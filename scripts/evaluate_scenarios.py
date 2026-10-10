#!/usr/bin/env python3
"""Évalue des conversations synthétiques sans téléphone et sans en exporter le texte."""

# ruff: noqa: E402 -- CLI bootstraps src/ before importing the package.
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vocalguard.application.scoring import ScoringPipeline
from vocalguard.application.sessions import SessionRegistry
from vocalguard.config import Settings
from vocalguard.domain.events import Event, EventType
from vocalguard.domain.models import ContentSignal, TranscriptSegment
from vocalguard.domain.scoring.circuit_breakers import PAYMENT_IDENTITY_MIN_RISK
from vocalguard.infrastructure.aws.credentials import AWSCredentialsError, aws_session
from vocalguard.infrastructure.contacts.book import JsonContactBook
from vocalguard.infrastructure.fakes.doubles import FakeContentAnalyzer
from vocalguard.infrastructure.fakes.event_sink import FanOutSink
from vocalguard.infrastructure.fakes.transcriber import FakeTranscriber
from vocalguard.infrastructure.keywords.detector import KeywordDetector
from vocalguard.infrastructure.telephony.reputation import TwilioReputationChecker


class EvaluationError(Exception):
    """Erreur explicite dont le message ne contient ni secret ni conversation."""


@dataclass(frozen=True)
class Case:
    id: str
    title: str
    label: str
    language: str
    number: str
    turns: tuple[TranscriptSegment, ...]


def load_cases(directory: Path = ROOT / "scenarios") -> list[Case]:
    cases = []
    for path in sorted(directory.rglob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        metadata = raw.get("evaluation", {})
        label = metadata.get("label")
        if label is None:
            if path.stem.startswith("scam_"):
                label = "scam"
            elif path.stem.startswith("legit_"):
                label = "legitimate"
            else:
                continue  # Le smoke technique ne constitue pas une vérité terrain.
        if label not in {"scam", "legitimate"}:
            raise EvaluationError(f"Étiquette invalide : {path.name}")
        turns = []
        previous = -1.0
        for turn in raw.get("turns", []):
            t = float(turn["t"])
            if not math.isfinite(t) or t < previous or turn["speaker"] not in {"caller", "victim"}:
                raise EvaluationError(f"Tour invalide : {path.name}")
            if not isinstance(turn["text"], str) or not turn["text"].strip():
                raise EvaluationError(f"Texte absent : {path.name}")
            turns.append(TranscriptSegment(turn["speaker"], turn["text"], t, True))
            previous = t
        if not turns:
            raise EvaluationError(f"Conversation vide : {path.name}")
        cases.append(
            Case(
                path.stem,
                raw.get("title", path.stem),
                label,
                metadata.get("language", "fr"),
                raw.get("caller_number", ""),
                tuple(turns),
            )
        )
    if not cases:
        raise EvaluationError("Aucun scénario étiqueté trouvé.")
    return cases


@dataclass
class Result:
    id: str
    title: str
    label: str
    language: str
    status: str
    production_route: str = "unknown"
    peak_score: int | None = None
    peak_level: str | None = None
    predicted: str | None = None
    first_alert_s: float | None = None
    breakers: list[str] = field(default_factory=list)
    content_calls: int = 0
    content_successes: int = 0
    content_failures: int = 0
    content_latency_ms: list[float] = field(default_factory=list)


class MetricSink:
    """Liste blanche : jamais de transcript, raison libre ou explication de Claude."""

    def __init__(self) -> None:
        self.scores: list[tuple[float, int, str]] = []
        self.breakers: set[str] = set()

    async def emit(self, event: Event) -> None:
        if event.type is EventType.SCORE:
            self.scores.append((event.t, int(event.data["score"]), str(event.data["level"])))
            self.breakers.update(event.data.get("breakers_fired", []))


class TimedAnalyzer:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.calls = 0
        self.successes = 0
        self.failures = 0
        self.latencies: list[float] = []

    async def analyze(self, window) -> ContentSignal | None:
        started = time.perf_counter()
        self.calls += 1
        try:
            signal = await self.inner.analyze(window)
        except Exception:
            signal = None
        finally:
            self.latencies.append(round((time.perf_counter() - started) * 1000, 3))
        if signal is None:
            self.failures += 1
        else:
            self.successes += 1
        return signal


def real_client(settings: Settings, *, region: str | None = None):
    """.env, exports ou profil local ; aucun repli EC2/ECS ni export global."""
    from botocore.config import Config

    try:
        session = aws_session(settings, allow_metadata=False)
    except AWSCredentialsError as exc:
        raise EvaluationError(str(exc)) from None
    credentials = session.get_credentials()
    if credentials is None:
        raise EvaluationError(
            "Mode Bedrock : configurer un profil AWS local (~/.aws/credentials) "
            "ou définir AWS_ACCESS_KEY_ID et AWS_SECRET_ACCESS_KEY dans .env ou les exports."
        )
    frozen = credentials.get_frozen_credentials()
    return session.client(
        "bedrock-runtime",
        region_name=region or settings.bedrock_region,
        aws_access_key_id=frozen.access_key,
        aws_secret_access_key=frozen.secret_key,
        aws_session_token=frozen.token,
        config=Config(connect_timeout=5, read_timeout=25, retries={"max_attempts": 0}),
    )


async def evaluate_case(
    case: Case,
    *,
    mode: str,
    settings: Settings,
    client=None,
    model_id: str | None = None,
) -> Result:
    contacts = JsonContactBook.from_path(ROOT / "data" / "contacts.example.json")
    metrics = MetricSink()
    fanout = FanOutSink(metrics)
    clock = [0.0]
    registry = SessionRegistry(
        transcriber_factory=FakeTranscriber,
        sink=fanout,
        settings=settings,
        clock=lambda: clock[0],
    )
    analyzer = None
    if mode == "offline":
        analyzer = TimedAnalyzer(FakeContentAnalyzer())
    elif mode == "bedrock":
        from vocalguard.infrastructure.aws.bedrock_analyzer import BedrockContentAnalyzer

        analyzer = TimedAnalyzer(
            BedrockContentAnalyzer(
                client=client,
                model_id=model_id or settings.bedrock_model_id,
                min_interval_seconds=0,
            )
        )
    pipeline = ScoringPipeline(
        registry=registry,
        keyword_engine=KeywordDetector(),
        content_analyzer=analyzer,
        reputation_checker=TwilioReputationChecker(contacts=contacts, settings=settings),
        settings=settings,
    )
    fanout.add(pipeline)
    session = await registry.open(case.id, caller_number=case.number)
    try:
        for turn in case.turns:
            clock[0] = turn.t
            await session.on_segment(turn)
            await pipeline.aclose()
            if analyzer is not None and analyzer.failures:
                break  # Autorisation AWS expirée : un échec, aucun retry coûteux par scénario.
    finally:
        pipeline.forget(case.id)
        await registry.close(case.id)
        await pipeline.aclose()
    status = "evaluated"
    if not metrics.scores:
        status = "unavailable"
    elif analyzer is not None and analyzer.failures:
        status = "partial" if analyzer.successes else "unavailable"
    result = Result(case.id, case.title, case.label, case.language, status)
    contact = contacts.by_number(case.number)
    result.production_route = (
        "trusted_passthrough" if contact and contact.is_trusted else "analyzed"
    )
    if metrics.scores:
        peak = max(metrics.scores, key=lambda sample: sample[1])
        result.peak_score, result.peak_level = peak[1:]
        result.first_alert_s = next(
            (t for t, score, _ in metrics.scores if score >= settings.thresholds().alert),
            None,
        )
        if status == "evaluated":
            result.predicted = "scam" if peak[1] >= settings.thresholds().alert else "legitimate"
        result.breakers = sorted(metrics.breakers)
    if analyzer:
        result.content_calls = analyzer.calls
        result.content_successes = analyzer.successes
        result.content_failures = analyzer.failures
        result.content_latency_ms = analyzer.latencies
    return result


def ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    samples = sorted(values)
    position = (len(samples) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    return round(samples[low] + (samples[high] - samples[low]) * (position - low), 3)


def summarize(results: list[Result]) -> dict[str, object]:
    matrix = {"true_positive": 0, "true_negative": 0, "false_positive": 0, "false_negative": 0}
    errors = []
    evaluated = [result for result in results if result.status == "evaluated"]
    for result in evaluated:
        positive = result.label == "scam"
        predicted_positive = result.predicted == "scam"
        category = ("true_" if positive == predicted_positive else "false_") + (
            "positive" if predicted_positive else "negative"
        )
        matrix[category] += 1
        if category.startswith("false_"):
            errors.append({"id": result.id, "category": category, "peak_score": result.peak_score})
    tp, tn, fp, fn = (
        matrix[key]
        for key in (
            "true_positive",
            "true_negative",
            "false_positive",
            "false_negative",
        )
    )
    latencies = [latency for result in results for latency in result.content_latency_ms]
    return {
        "total_cases": len(results),
        "evaluated_cases": len(evaluated),
        "unavailable_cases": len(results) - len(evaluated),
        "confusion_matrix": matrix,
        "sensitivity": ratio(tp, tp + fn),
        "specificity": ratio(tn, tn + fp),
        "precision": ratio(tp, tp + fp),
        "accuracy": ratio(tp + tn, tp + tn + fp + fn),
        "classification_errors": errors,
        "content_calls": sum(result.content_calls for result in results),
        "content_successes": sum(result.content_successes for result in results),
        "content_failures": sum(result.content_failures for result in results),
        "content_latency_ms": {
            "samples": len(latencies),
            "p50": percentile(latencies, 0.5),
            "p95": percentile(latencies, 0.95),
            "max": max(latencies) if latencies else None,
        },
    }


def markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    matrix = summary["confusion_matrix"]
    lines = [
        "# Évaluation de scénarios VocalGuard",
        "",
        report["evidence"],
        "",
        f"Mode : `{report['mode']}` · Modèle : `{report['model']}`",
        f"Scénarios exploitables : {summary['evaluated_cases']}/{summary['total_cases']}",
        "",
        "Les résultats portent sur des conversations synthétiques, sans téléphone, AWS Transcribe,",
        "signal voix ni Twilio Lookup. Tous les transcripts sont analysés ; le passage direct des",
        "contacts de confiance n'est pas testé. Un petit jeu écrit à la main n'est pas une mesure",
        "de performance sur la population. Les échecs du modèle sont exclus des taux.",
        "Chaque segment synthétique est soumis au modèle avec un intervalle minimal de 0 s ;",
        "cette cadence diffère de celle des appels en production. Le moment de première alerte",
        "suit l'horloge du scénario et ne mesure pas la latence réelle de détection.",
        f"Le gate paiement + identité exige un risque de contenu ≥{PAYMENT_IDENTITY_MIN_RISK},"
        " une tactique de paiement",
        "inhabituel et une identité discordante. Ce seuil de MVP reste heuristique, non calibré.",
        "",
        "| Vérité écrite | Alerte déclenchée | Pas d'alerte |",
        "|---|---:|---:|",
        f"| Arnaque | {matrix['true_positive']} | {matrix['false_negative']} |",
        f"| Légitime | {matrix['false_positive']} | {matrix['true_negative']} |",
        "",
        f"Sensibilité : {summary['sensitivity']} · Spécificité : {summary['specificity']}",
        f"Précision : {summary['precision']} · Exactitude : {summary['accuracy']}",
        "",
        "Latences mesurées du signal de contenu (ms), incluant les appels échoués :",
        json.dumps(summary["content_latency_ms"], ensure_ascii=False),
        "",
        "| Scénario | Langue | Vérité | Statut | Route réelle | Pic | Décision |",
        "|---|---|---|---|---|---:|---|",
    ]
    for result in report["results"]:
        lines.append(
            f"| {result['id']} | {result['language']} | {result['label']} | "
            f"{result['status']} | {result['production_route']} | "
            f"{result['peak_score']} | {result['predicted']} |"
        )
    for title, subset in (
        ("Appels dont la route active l'analyse", report["analyzed_route_summary"]),
        (
            "Projection du routage réel (aucune téléphonie testée)",
            report["routing_projection"]["summary"],
        ),
    ):
        routing_matrix = subset["confusion_matrix"]
        lines.extend(
            [
                "",
                f"## {title}",
                "",
                "| Vérité écrite | Alerte déclenchée | Pas d'alerte |",
                "|---|---:|---:|",
                f"| Arnaque | {routing_matrix['true_positive']} | "
                f"{routing_matrix['false_negative']} |",
                f"| Légitime | {routing_matrix['false_positive']} | "
                f"{routing_matrix['true_negative']} |",
            ]
        )
    lines.extend(
        [
            "",
            report["routing_projection"]["evidence"],
            "",
            "Le cas d'usurpation du numéro connu est donc un angle mort du passage direct,",
            "même si son texte aurait été reconnu par un analyseur dans ce banc synthétique.",
        ]
    )
    return "\n".join(lines) + "\n"


async def run(args) -> dict[str, Any]:
    settings = Settings() if args.mode == "bedrock" else Settings(_env_file=None)
    settings = settings.model_copy(
        update={
            "transcribe_backend": "fake",
            "history_enabled": False,
            "twilio_lookup_enabled": False,
            "voice_detector_enabled": False,
        }
    )
    client = real_client(settings, region=args.region) if args.mode == "bedrock" else None
    cases = load_cases(args.scenarios)
    results = []
    unavailable = False
    for case in cases:
        if unavailable:
            results.append(Result(case.id, case.title, case.label, case.language, "skipped"))
            continue
        result = await evaluate_case(
            case, mode=args.mode, settings=settings, client=client, model_id=args.model
        )
        results.append(result)
        if args.mode == "bedrock" and result.status != "evaluated":
            unavailable = True
    evidence = {
        "offline": "HORS LIGNE : doublure heuristique, aucune requête Claude/Bedrock.",
        "keywords": "MOTS-CLÉS SEULS : aucun modèle de langage sollicité.",
        "bedrock": "BEDROCK RÉEL : seules les réponses valides du modèle alimentent les taux.",
    }[args.mode]
    return {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "mode": args.mode,
        "model": (args.model or settings.bedrock_model_id)
        if args.mode == "bedrock"
        else ("FakeContentAnalyzer" if args.mode == "offline" else "none"),
        "evidence": evidence,
        "configuration": {
            "thresholds": asdict(settings.thresholds()),
            "weights": asdict(settings.weights()),
            "payment_identity_min_risk": PAYMENT_IDENTITY_MIN_RISK,
            "voice_signal": "not_measured",
            "twilio_lookup": "disabled",
            "routing_exercised": "all_synthetic_transcripts_analyzed",
            "content_min_interval_seconds": 0,
            "scenario_clock": "simulated; first_alert_s is not wall-clock latency",
            "bedrock_region": (args.region or settings.bedrock_region)
            if args.mode == "bedrock"
            else None,
        },
        "alert_threshold": settings.thresholds().alert,
        "summary": summarize(results),
        "analyzed_route_summary": summarize(
            [result for result in results if result.production_route == "analyzed"]
        ),
        "routing_projection": {
            "evidence": "Projection du routage, sans appel réel : un contact de confiance "
            "passe sans alerte ni analyse. La vérification manuelle est hors périmètre.",
            "summary": summarize(
                [
                    replace(
                        result,
                        predicted="legitimate",
                        peak_score=None,
                        peak_level=None,
                        first_alert_s=None,
                        content_calls=0,
                        content_successes=0,
                        content_failures=0,
                        content_latency_ms=[],
                    )
                    if result.production_route == "trusted_passthrough"
                    and result.status == "evaluated"
                    else result
                    for result in results
                ]
            ),
            "trusted_cases": [
                {"id": result.id, "label": result.label, "routed_decision": "no_analysis_no_alert"}
                for result in results
                if result.production_route == "trusted_passthrough"
            ],
        },
        "results": [asdict(result) for result in results],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["offline", "keywords", "bedrock"], default="offline")
    parser.add_argument("--scenarios", type=Path, default=ROOT / "scenarios")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts" / "evaluation")
    parser.add_argument("--model")
    parser.add_argument("--region")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.ERROR)
    try:
        report = asyncio.run(run(args))
    except Exception as error:
        # Ne pas sérialiser l'exception de fournisseur ou un corps JSON invalide.
        message = str(error) if isinstance(error, EvaluationError) else type(error).__name__
        print(f"Évaluation impossible : {message}", file=sys.stderr)
        return 2
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.output_dir / "report.md").write_text(markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {"mode": report["mode"], "evidence": report["evidence"], "summary": report["summary"]},
            ensure_ascii=False,
        )
    )
    return 2 if report["summary"]["unavailable_cases"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
