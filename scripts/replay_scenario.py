#!/usr/bin/env python3
"""Rejoue un scénario écrit dans le pipeline, sans téléphone ni AWS.

C'est l'outil qui débloque l'équipe : tant que la téléphonie n'est pas prête,
Dev 2 et Biz 1 travaillent contre de faux événements produits ici, à la cadence
réelle de l'appel. Aucun appel Twilio, aucun appel Claude, aucun crédit dépensé.

    make replay SCENARIO=scenarios/scam_01_petit_fils.json
    .venv/bin/python scripts/replay_scenario.py scenarios/_smoke_technique.json --no-wait

Format du fichier scénario (JSON) :

    {
      "call_id": "CA_demo_01",
      "caller_number": "+15551234567",
      "victim_number": "+33600000000",
      "turns": [
        {"t": 0.0, "speaker": "caller", "text": "Mamie c'est Lucas...", "is_final": true},
        {"t": 4.2, "speaker": "victim", "text": "Lucas ?"}
      ]
    }

  `t`        : secondes depuis le début de l'appel (sert à rejouer à la bonne cadence)
  `speaker`  : "caller" (l'appelant, scammeur présumé) ou "victim" (la personne appelée)
  `is_final` : optionnel, défaut true. `false` simule un résultat partiel de Transcribe.
  Les clés commençant par `_` sont ignorées (commentaires).

Ce script ne calcule PAS de score et n'appelle PAS Claude : c'est la part de Dev 2.
Voir `on_transcript_hook` plus bas, le point de branchement prévu pour son pipeline.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Le paquet est installé par `uv sync`, mais le script doit aussi marcher lancé
# à la main depuis un clone frais, sans installation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vocalguard.config import get_settings
from vocalguard.domain.events import Event
from vocalguard.domain.models import TranscriptSegment

# Ces modules sont écrits par d'autres membres de l'équipe. S'ils manquent, on le
# dit franchement plutôt que de recopier une version locale qui divergerait.
try:
    from vocalguard.application.intervention import InterventionRunner
    from vocalguard.application.sessions import SessionRegistry
    from vocalguard.infrastructure.fakes.call_controller import LoggingCallController
    from vocalguard.infrastructure.fakes.event_sink import (
        ConsoleEventSink,
        FanOutSink,
        InMemoryEventSink,
    )
    from vocalguard.infrastructure.fakes.transcriber import FakeTranscriber
except ImportError as exc:  # pragma: no cover - dépend de l'avancement de l'équipe
    print(f"\n  Brique manquante : {exc}", file=sys.stderr)
    print(
        "  Le replay a besoin de : application/sessions.py, application/intervention.py,\n"
        "  infrastructure/fakes/{event_sink,transcriber,call_controller}.py — la part de Dev 1.\n",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc

SPEAKERS = ("caller", "victim")


# ---------------------------------------------------------------------------
# Point de branchement pour Dev 2
# ---------------------------------------------------------------------------
# Dev 2 : remplace `on_transcript_hook` par ton pipeline d'analyse. Signature :
#
#     async def hook(session, segment: TranscriptSegment, sink) -> None: ...
#
# À toi d'émettre tes propres événements (`keyword`, `llm`, `reputation`, `score`)
# dans `sink`. Dès qu'un événement `score` y passe, InterventionRunner déclenche
# les actions (ici simulées par LoggingCallController, aucun appel Twilio réel).
# Appelé uniquement sur les segments finaux : c'est la règle du plan pour Claude.
on_transcript_hook: Any = None


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

    return Scenario(
        call_id=str(raw.get("call_id") or f"CA_replay_{path.stem}"),
        caller_number=str(raw.get("caller_number") or "+15555550100"),
        victim_number=str(raw.get("victim_number") or "+33600000000"),
        turns=tuple(turns),
    )


# --- Colle locale : affichage et branchement des actions --------------------


class JsonLinesSink:
    """Une ligne JSON par événement, pour piper vers jq ou un fichier."""

    async def emit(self, event: Event) -> None:
        print(json.dumps(event.to_wire(), ensure_ascii=False), flush=True)


class InterventionSink:
    """Relaie chaque événement au runner d'intervention.

    Le bus d'événements de Dev 2 fera ce routage proprement ; en attendant, c'est
    ce branchement à la main qui permet de voir les actions se déclencher en replay.

    Le runner est injecté après coup : il a besoin du registre, qui a besoin du
    sink, qui a besoin de ce relais. Cette liaison tardive casse le cycle.
    """

    def __init__(self) -> None:
        self.runner: Any = None

    async def emit(self, event: Event) -> None:
        if self.runner is not None:
            await self.runner.handle_event(event)


# --- Boucle de replay -------------------------------------------------------


async def replay(
    scenario: Scenario,
    *,
    speed: float,
    no_wait: bool,
    as_json: bool,
    quiet: bool,
) -> int:
    settings = get_settings()

    memory = InMemoryEventSink()
    intervention = InterventionSink()

    # L'ordre d'affichage compte : la console d'abord, pour que l'action
    # apparaisse juste sous le score qui l'a déclenchée.
    sinks: list[Any] = [memory, intervention]
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
    intervention.runner = InterventionRunner(
        controller=LoggingCallController(),
        sink=sink,
        settings=settings,
        registry=registry,
    )

    if not quiet and not as_json:
        cadence = "immédiat" if no_wait else f"vitesse {speed:g}x"
        print(
            f"\n  Replay {scenario.call_id} · {len(scenario.turns)} tours · "
            f"{scenario.duration:.0f} s d'appel · {cadence}\n"
        )

    session = await registry.open(
        scenario.call_id,
        caller_number=scenario.caller_number,
        victim_number=scenario.victim_number,
    )
    await session.start()

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

            if on_transcript_hook is not None and turn.is_final:
                segment = TranscriptSegment(
                    speaker=turn.speaker, text=turn.text, t=turn.t, is_final=True
                )
                await on_transcript_hook(session, segment, sink)
    finally:
        await registry.close(scenario.call_id)

    if not as_json:
        _recap(memory, quiet=quiet)
    return 0


def _recap(memory: Any, *, quiet: bool) -> None:
    counts = Counter(str(event.type) for event in memory.events)
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
        print(
            "  Aucun événement `score` : normal tant que Dev 2 n'a pas branché son\n"
            "  pipeline sur `on_transcript_hook` (voir en tête de ce script).\n"
        )
    else:
        print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rejoue un scénario d'appel dans le pipeline, sans téléphone ni AWS.",
    )
    parser.add_argument("scenario", type=Path, help="chemin du fichier JSON de scénario")
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
    args = parser.parse_args(argv)

    if args.speed <= 0:
        parser.error("--speed doit être strictement positif")

    try:
        scenario = load_scenario(args.scenario)
    except ScenarioError as exc:
        print(f"\n  Scénario inutilisable — {exc}\n", file=sys.stderr)
        return 1

    try:
        return asyncio.run(
            replay(
                scenario,
                speed=args.speed,
                no_wait=args.no_wait,
                as_json=args.as_json,
                quiet=args.quiet,
            )
        )
    except KeyboardInterrupt:
        print("\n  Replay interrompu.\n", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
