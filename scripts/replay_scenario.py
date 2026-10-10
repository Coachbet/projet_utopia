#!/usr/bin/env python3
"""Rejoue un scénario écrit dans le pipeline complet, sans téléphone.

Deux usages, le même script :

  - **débloquer l'équipe** — tant que la téléphonie n'est pas prête, on rejoue un
    appel écrit à la cadence réelle, sans Twilio et sans crédit dépensé ;
  - **calibrer** — chaque scénario porte un bloc `expect`, et le script sort en
    erreur si le score ne tombe pas dans la fourchette attendue. Les scénarios de
    Biz 1 forment ainsi une suite de non-régression, en particulier les appels
    légitimes qui doivent rester sous 40.

    make replay SCENARIO=scenarios/scam_01_petit_fils.json
    uv run python scripts/replay_scenario.py scenarios/*.json --no-wait
    uv run python scripts/replay_scenario.py scenarios/scam_01_petit_fils.json --real-llm

Format du fichier scénario (JSON) :

    {
      "call_id": "CA_demo_01",
      "caller_number": "+15551234567",
      "victim_number": "+33600000000",
      "expect": {"min_score": 85, "level": "intervene"},
      "turns": [
        {"t": 0.0, "speaker": "caller", "text": "Mamie c'est Lucas...", "is_final": true},
        {"t": 4.2, "speaker": "victim", "text": "Lucas ?"}
      ]
    }

  `t`        : secondes depuis le début de l'appel (sert à rejouer à la bonne cadence)
  `speaker`  : "caller" (l'appelant, scammeur présumé) ou "victim" (la personne appelée)
  `is_final` : optionnel, défaut true. `false` simule un résultat partiel de Transcribe.
  `expect`   : optionnel. `min_score` / `max_score` portent sur le PIC atteint,
               `level` sur le niveau final. Absent = aucune attente, rien à vérifier.
  Les clés commençant par `_` sont ignorées (commentaires).

Par défaut Claude n'est pas appelé : une doublure déterministe tient sa place, pour
que le rejeu soit gratuit et reproductible. `--real-llm` branche le vrai Bedrock.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Le paquet est installé par `uv sync`, mais le script doit aussi marcher lancé
# à la main depuis un clone frais, sans installation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vocalguard.config import get_settings
from vocalguard.domain.events import Event, EventType

# Ces modules sont écrits par les deux moitiés de l'équipe. S'ils manquent, on le
# dit franchement plutôt que de recopier une version locale qui divergerait.
try:
    from vocalguard.application.intervention import InterventionRunner
    from vocalguard.application.scoring import ScoringPipeline
    from vocalguard.application.sessions import SessionRegistry
    from vocalguard.infrastructure.contacts.book import JsonContactBook
    from vocalguard.infrastructure.fakes.call_controller import LoggingCallController
    from vocalguard.infrastructure.fakes.doubles import FakeContentAnalyzer
    from vocalguard.infrastructure.fakes.event_sink import (
        ConsoleEventSink,
        FanOutSink,
        InMemoryEventSink,
    )
    from vocalguard.infrastructure.fakes.transcriber import FakeTranscriber
    from vocalguard.infrastructure.keywords.detector import KeywordDetector
    from vocalguard.infrastructure.telephony.reputation import TwilioReputationChecker
except ImportError as exc:  # pragma: no cover - dépend de l'avancement de l'équipe
    print(f"\n  Brique manquante : {exc}", file=sys.stderr)
    print(
        "  Le replay a besoin des deux moitiés : application/{sessions,intervention,scoring}.py,\n"
        "  infrastructure/fakes/*, infrastructure/{keywords,contacts,telephony}/*.\n",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc

SPEAKERS = ("caller", "victim")

GREEN = "\033[32m"
RED = "\033[31m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


@dataclass(frozen=True, slots=True)
class Turn:
    t: float
    speaker: str
    text: str
    is_final: bool = True


@dataclass(frozen=True, slots=True)
class Scenario:
    call_id: str
    caller_number: str
    victim_number: str
    turns: tuple[Turn, ...]
    title: str = ""
    notes: str = ""
    expect: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.turns[-1].t if self.turns else 0.0


class ScenarioError(Exception):
    """Scénario illisible : on veut un message utile, pas une KeyError."""


def load_scenario(path: Path) -> Scenario:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ScenarioError(f"fichier introuvable : {path}") from exc
    except json.JSONDecodeError as exc:
        raise ScenarioError(f"JSON invalide dans {path} ligne {exc.lineno} : {exc.msg}") from exc

    if not isinstance(raw, dict):
        raise ScenarioError(f"{path} : l'objet racine doit être un dictionnaire JSON")

    raw_turns = raw.get("turns")
    if not isinstance(raw_turns, list) or not raw_turns:
        raise ScenarioError(f'{path} : clé "turns" absente ou vide')

    turns: list[Turn] = []
    for index, item in enumerate(raw_turns):
        where = f"{path} : turns[{index}]"
        if not isinstance(item, dict):
            raise ScenarioError(f"{where} doit être un objet")
        speaker = item.get("speaker")
        if speaker not in SPEAKERS:
            raise ScenarioError(f"{where} : speaker={speaker!r}, attendu caller ou victim")
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ScenarioError(f'{where} : "text" manquant ou vide')
        try:
            t = float(item.get("t", 0.0))
        except (TypeError, ValueError) as exc:
            raise ScenarioError(f"{where} : t={item.get('t')!r} n'est pas un nombre") from exc
        if t < 0:
            raise ScenarioError(f"{where} : t doit être positif")
        is_final = bool(item.get("is_final", True))
        turns.append(Turn(t=t, speaker=speaker, text=text, is_final=is_final))

    # Un scénario écrit à la main n'est pas forcément dans l'ordre chronologique.
    turns.sort(key=lambda turn: turn.t)

    expect = raw.get("expect") or {}
    if not isinstance(expect, dict):
        raise ScenarioError(f'{path} : "expect" doit être un objet')

    return Scenario(
        call_id=str(raw.get("call_id") or f"CA_replay_{path.stem}"),
        caller_number=str(raw.get("caller_number") or "+15555550100"),
        victim_number=str(raw.get("victim_number") or "+33600000000"),
        turns=tuple(turns),
        title=str(raw.get("title") or path.stem),
        notes=str(raw.get("notes") or ""),
        expect=expect,
    )


# --- Colle locale : affichage et branchement ---------------------------------


class JsonLinesSink:
    """Une ligne JSON par événement, pour piper vers jq ou un fichier."""

    async def emit(self, event: Event) -> None:
        print(json.dumps(event.to_wire(), ensure_ascii=False), flush=True)


class LateBoundSink:
    """Relais à liaison tardive.

    Le sink a besoin du pipeline, qui a besoin du registre, qui a besoin du sink :
    ce relais casse le cycle. On le met dans le `FanOutSink`, puis on lui donne
    sa cible une fois tout le monde construit.
    """

    def __init__(self, label: str) -> None:
        self.label = label
        self.handler: Any = None

    async def emit(self, event: Event) -> None:
        if self.handler is not None:
            await self.handler(event)


def _contact_book(settings: Any) -> JsonContactBook:
    """Le carnet réel s'il existe, sinon celui d'exemple.

    `data/contacts.json` est dans `.gitignore` (numéros réels) : sur un clone
    frais il est absent, et un carnet vide ferait silencieusement tomber le
    signal de réputation — donc la calibration.
    """
    path = Path(settings.contacts_path)
    if not path.exists():
        example = path.with_name("contacts.example.json")
        if example.exists():
            return JsonContactBook.from_path(example)
    return JsonContactBook.from_path(path)


def _build_analyzer(*, real_llm: bool) -> Any:
    if not real_llm:
        return FakeContentAnalyzer()
    from vocalguard.infrastructure.aws.bedrock_analyzer import BedrockContentAnalyzer

    # Le scénario est rejoué plus vite que le temps réel : la cadence anti-coût
    # sauterait tous les tours sauf le premier. Ici on veut une analyse par tour.
    return BedrockContentAnalyzer(min_interval_seconds=0.0)


# --- Boucle de replay -------------------------------------------------------


async def replay(
    scenario: Scenario,
    *,
    speed: float,
    no_wait: bool,
    as_json: bool,
    quiet: bool,
    real_llm: bool,
) -> bool:
    settings = get_settings()

    memory = InMemoryEventSink()
    intervention_relay = LateBoundSink("intervention")
    scoring_relay = LateBoundSink("scoring")

    # L'ordre compte. La console d'abord, pour que l'action apparaisse juste sous
    # le score qui l'a déclenchée. Le scoring en dernier : ses propres événements
    # réentrent dans le fan-out, donc ils s'affichent après le transcript qui les
    # a provoqués et la frise reste lisible.
    sinks: list[Any] = [memory, intervention_relay, scoring_relay]
    if as_json:
        sinks.insert(0, JsonLinesSink())
    elif not quiet:
        sinks.insert(0, ConsoleEventSink())
    sink = FanOutSink(*sinks)

    transcriber = FakeTranscriber()
    registry = SessionRegistry(
        transcriber_factory=lambda: transcriber,
        sink=sink,
        settings=settings,
    )

    # Le runner écrit ses événements `action` dans le même sink : ils s'affichent
    # donc comme le reste. Pas de boucle, `handle_event` ne lit que les `score`.
    intervention_relay.handler = InterventionRunner(
        controller=LoggingCallController(),
        sink=sink,
        settings=settings,
        registry=registry,
    ).handle_event

    pipeline = ScoringPipeline(
        registry=registry,
        keyword_engine=KeywordDetector(),
        content_analyzer=_build_analyzer(real_llm=real_llm),
        reputation_checker=TwilioReputationChecker(
            contacts=_contact_book(settings), settings=settings
        ),
        settings=settings,
    )
    scoring_relay.handler = pipeline.emit

    if not quiet and not as_json:
        cadence = "immédiat" if no_wait else f"vitesse {speed:g}x"
        claude = "Claude réel" if real_llm else "doublure de contenu"
        print(f"\n{BOLD}▶ {scenario.title}{RESET}  {DIM}{scenario.call_id}{RESET}")
        if scenario.notes:
            print(f"{DIM}  {scenario.notes}{RESET}")
        print(
            f"{DIM}  {len(scenario.turns)} tours · {scenario.duration:.0f} s d'appel · "
            f"{cadence} · {claude}{RESET}\n"
        )

    # `registry.open` démarre déjà la session : un `session.start()` de plus
    # ouvrirait un second jeu de streams de transcription.
    await registry.open(
        scenario.call_id,
        caller_number=scenario.caller_number,
        victim_number=scenario.victim_number,
    )

    try:
        started = time.monotonic()
        for turn in scenario.turns:
            if not no_wait:
                # On se recale sur l'horloge absolue du scénario : accumuler des
                # sleeps relatifs ferait dériver la cadence sur un appel long.
                delay = turn.t / speed - (time.monotonic() - started)
                if delay > 0:
                    await asyncio.sleep(delay)

            await transcriber.say(turn.speaker, turn.text, t=turn.t, is_final=turn.is_final)

            # L'analyse de contenu tourne en tâche de fond. On l'attend à chaque
            # tour : sans ça, `--no-wait` ne lui laisse jamais la main et le
            # verdict dépendrait de l'ordonnanceur.
            await pipeline.aclose()
    finally:
        await pipeline.aclose()
        await registry.close(scenario.call_id)
        pipeline.forget(scenario.call_id)

    if not as_json:
        _recap(memory, quiet=quiet)
    return _verdict(scenario, memory, quiet=quiet or as_json)


def _scores(memory: InMemoryEventSink) -> list[int]:
    return [int(e.data.get("score", 0)) for e in memory.by_type(EventType.SCORE)]


def _recap(memory: InMemoryEventSink, *, quiet: bool) -> None:
    counts = Counter(event.type.value for event in memory.events)
    if quiet:
        print(json.dumps(counts, ensure_ascii=False))
        return

    print("\n  Événements émis")
    print("  " + "─" * 40)
    for name in ("transcript", "keyword", "llm", "voice", "reputation", "score", "action"):
        n = counts.get(name, 0)
        marker = " " if n else "·"
        print(f"  {marker} {name.ljust(12)} {n:>4}")
    print("  " + "─" * 40)
    if not counts.get("score"):
        print("  Aucun événement `score` — le pipeline de scoring n'a rien produit.")
    print()


def _verdict(scenario: Scenario, memory: InMemoryEventSink, *, quiet: bool) -> bool:
    scores = _scores(memory)
    events = memory.by_type(EventType.SCORE)
    peak = max(scores, default=0)
    final = scores[-1] if scores else 0
    level = str(events[-1].data.get("level", "none")) if events else "none"

    if not quiet:
        print(f"  score final {final}  ({level}) — pic à {peak}")

    if not scenario.expect:
        if not quiet:
            print(f"{DIM}  aucun bloc `expect` : rien à vérifier{RESET}\n")
        return True

    problems: list[str] = []
    expect = scenario.expect
    if "min_score" in expect and peak < int(expect["min_score"]):
        problems.append(f"pic {peak} < min_score {expect['min_score']}")
    if "max_score" in expect and peak > int(expect["max_score"]):
        problems.append(f"pic {peak} > max_score {expect['max_score']} (faux positif)")
    if "level" in expect and level != expect["level"]:
        problems.append(f"niveau {level} != {expect['level']}")

    if problems:
        # Sur stderr : `--json` doit rester un flux JSON pipeable vers jq, et un
        # verdict d'échec au milieu le rendrait illisible.
        print(f"  {RED}✗ {scenario.title} — {'; '.join(problems)}{RESET}\n", file=sys.stderr)
        return False
    if not quiet:
        print(f"  {GREEN}✓ conforme aux attentes du scénario{RESET}\n")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rejoue un ou plusieurs scénarios d'appel dans le pipeline complet.",
    )
    parser.add_argument(
        "scenarios", nargs="+", type=Path, help="fichiers JSON de scénario"
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="facteur de cadence (2 = deux fois plus vite). Défaut : 1.0",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="rejoue tous les tours immédiatement, sans respecter les timings",
    )
    parser.add_argument(
        "--json", dest="as_json", action="store_true", help="une ligne JSON par événement"
    )
    parser.add_argument("--quiet", action="store_true", help="n'affiche que le récapitulatif")
    parser.add_argument(
        "--real-llm",
        action="store_true",
        help="appelle vraiment Claude sur Bedrock (facturé) au lieu de la doublure",
    )
    args = parser.parse_args(argv)

    if args.speed <= 0:
        parser.error("--speed doit être strictement positif")

    scenarios: list[Scenario] = []
    for path in args.scenarios:
        try:
            scenarios.append(load_scenario(path))
        except ScenarioError as exc:
            print(f"\n  Scénario inutilisable — {exc}\n", file=sys.stderr)
            return 1

    failures = 0
    try:
        for scenario in scenarios:
            ok = asyncio.run(
                replay(
                    scenario,
                    speed=args.speed,
                    no_wait=args.no_wait,
                    as_json=args.as_json,
                    quiet=args.quiet,
                    real_llm=args.real_llm,
                )
            )
            if not ok:
                failures += 1
    except KeyboardInterrupt:
        print("\n  Replay interrompu.\n", file=sys.stderr)
        return 130

    if len(scenarios) > 1 and not args.as_json:
        total = len(scenarios)
        if failures:
            print(f"  {RED}{failures}/{total} scénario(s) hors attentes{RESET}\n")
        else:
            print(f"  {GREEN}{total}/{total} scénarios conformes{RESET}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
