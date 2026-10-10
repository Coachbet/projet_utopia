"""Métriques évaluées, échecs honnêtes et absence de transcript dans les rapports."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from vocalguard.config import Settings
from vocalguard.domain.models import ContentSignal, TranscriptSegment

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "evaluate_scenarios.py"
spec = importlib.util.spec_from_file_location("evaluate_scenarios", SCRIPT)
evaluation = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = evaluation
spec.loader.exec_module(evaluation)


def result(id: str, label: str, predicted: str | None, status: str = "evaluated"):
    return evaluation.Result(id, id, label, "fr", status, predicted=predicted)


def test_confusion_matrix_and_rates_exclude_unavailable_model_calls() -> None:
    rows = [
        result("tp", "scam", "scam"),
        result("tn", "legitimate", "legitimate"),
        result("fp", "legitimate", "scam"),
        result("fn", "scam", "legitimate"),
        result("failed", "scam", None, "unavailable"),
    ]
    summary = evaluation.summarize(rows)
    assert summary["confusion_matrix"] == {
        "true_positive": 1,
        "true_negative": 1,
        "false_positive": 1,
        "false_negative": 1,
    }
    assert summary["sensitivity"] == summary["specificity"] == summary["precision"] == 0.5
    assert summary["evaluated_cases"] == 4
    assert summary["unavailable_cases"] == 1
    assert {error["id"] for error in summary["classification_errors"]} == {"fp", "fn"}


def test_missing_class_and_missing_evidence_do_not_claim_perfect_accuracy() -> None:
    summary = evaluation.summarize([result("failed", "scam", None, "unavailable")])
    assert summary["accuracy"] is None
    assert summary["sensitivity"] is None
    assert summary["specificity"] is None
    assert summary["content_latency_ms"]["p95"] is None


def test_latency_percentiles_use_observed_samples() -> None:
    row = result("valid", "scam", "scam")
    row.content_latency_ms = [10, 20, 30, 40]
    summary = evaluation.summarize([row])
    assert summary["content_latency_ms"] == {"samples": 4, "p50": 25, "p95": 38.5, "max": 40}


def test_real_mode_fails_without_exports_or_local_credentials(
    monkeypatch, capsys, tmp_path
) -> None:
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "absent-credentials"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "absent-config"))
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    # Ne jamais lire les clés réelles ajoutées par l'utilisateur dans .env.
    monkeypatch.setattr(evaluation, "Settings", lambda: Settings(_env_file=None))
    assert evaluation.main(["--mode", "bedrock"]) == 2
    assert "profil AWS local" in capsys.readouterr().err


def test_temporary_credentials_require_session_token(monkeypatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ASIA_SYNTHETIC_NEVER_VALID")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic-not-a-secret")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    with pytest.raises(evaluation.EvaluationError, match="AWS_SESSION_TOKEN"):
        evaluation.real_client(Settings(_env_file=None))


def test_real_mode_accepts_local_profile_and_removes_metadata_providers(
    monkeypatch, tmp_path
) -> None:
    import boto3

    path = tmp_path / "credentials"
    path.write_text(
        "[default]\naws_access_key_id = synthetic-access\n"
        "aws_secret_access_key = synthetic-secret\n"
    )
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(path))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "absent-config"))
    removed = []
    actual_session = boto3.Session

    def session(**kwargs):
        instance = actual_session(**kwargs)
        resolver = instance._session.get_component("credential_provider")
        remove = resolver.remove

        def tracked_remove(name):
            removed.append(name)
            remove(name)

        resolver.remove = tracked_remove
        instance.client = lambda service, **kwargs: (service, kwargs["config"])
        return instance

    monkeypatch.setattr(boto3, "Session", session)
    service, config = evaluation.real_client(Settings(_env_file=None))
    assert service == "bedrock-runtime"
    assert removed == ["iam-role", "container-role"]
    assert config.connect_timeout == 5
    assert config.read_timeout == 25


async def test_routing_projection_exposes_trusted_spoofing_blind_spot(tmp_path) -> None:
    (tmp_path / "scam_known.json").write_text(
        json.dumps(
            {
                "evaluation": {"label": "scam"},
                "caller_number": "+33612345678",
                "turns": [
                    {
                        "t": 1,
                        "speaker": "caller",
                        "text": "Mamie c'est Lucas, urgence, accident, virement, ne dis rien.",
                    }
                ],
            }
        )
    )
    args = argparse.Namespace(mode="offline", scenarios=tmp_path, region=None, model=None)
    report = await evaluation.run(args)
    assert report["results"][0]["production_route"] == "trusted_passthrough"
    assert report["analyzed_route_summary"]["total_cases"] == 0
    assert report["routing_projection"]["summary"]["confusion_matrix"]["false_negative"] == 1
    assert (
        report["routing_projection"]["trusted_cases"][0]["routed_decision"]
        == "no_analysis_no_alert"
    )


async def test_offline_report_contains_no_transcript_or_false_claude_evidence(
    tmp_path: Path,
) -> None:
    marker = "NEVER_EXPORT_TEST_CONVERSATION_4821"
    (tmp_path / "legit_report.json").write_text(
        json.dumps(
            {
                "title": "Test synthétique",
                "evaluation": {"label": "legitimate"},
                "caller_number": "+33700000001",
                "turns": [{"t": 1, "speaker": "caller", "text": marker}],
            }
        )
    )
    args = argparse.Namespace(mode="offline", scenarios=tmp_path, region=None, model=None)
    report = await evaluation.run(args)
    serialized = json.dumps(report)
    assert marker not in serialized
    assert marker not in evaluation.markdown(report)
    assert "doublure heuristique" in report["evidence"]
    assert report["model"] == "FakeContentAnalyzer"
    assert report["results"][0]["content_calls"] == 1
    assert report["results"][0]["content_successes"] == 1


async def test_invalid_bedrock_output_records_unavailable_evidence() -> None:
    class InvalidClient:
        def invoke_model(self, **kwargs):
            return {"body": b'{"content":[{"text":"invalid"}]}'}

    case = evaluation.Case(
        "synthetic",
        "Test",
        "scam",
        "fr",
        "+33700000001",
        (
            TranscriptSegment("caller", "Contenu synthétique", 1, True),
            TranscriptSegment("caller", "Deuxième tour", 2, True),
        ),
    )
    result = await evaluation.evaluate_case(
        case,
        mode="bedrock",
        settings=Settings(_env_file=None),
        client=InvalidClient(),
    )
    assert result.status == "unavailable"
    assert result.predicted is None
    assert result.content_calls == result.content_failures == 1
    assert result.content_successes == 0


def test_dataset_includes_unknown_legitimate_and_multilingual_contrasts() -> None:
    cases = evaluation.load_cases()
    assert len([case for case in cases if case.label == "scam"]) >= 8
    assert len([case for case in cases if case.label == "legitimate"]) >= 8
    assert {"fr", "en", "fr-en"} <= {case.language for case in cases}
    assert any(case.id == "legit_urgent_borrowed_phone" for case in cases)
    assert any(case.id == "scam_known_identity" for case in cases)


async def test_analyzer_none_is_an_error_and_latencies_remain_measured() -> None:
    class SometimesUnavailable:
        async def analyze(self, window):
            return ContentSignal(0.1) if len(window) == 1 else None

    timed = evaluation.TimedAnalyzer(SometimesUnavailable())
    await timed.analyze([TranscriptSegment("caller", "texte", 1, True)])
    await timed.analyze([])
    assert timed.calls == 2
    assert (timed.successes, timed.failures) == (1, 1)
    assert len(timed.latencies) == 2
