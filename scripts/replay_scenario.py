#!/usr/bin/env python3
"""Rejoue un scénario écrit dans le pipeline, sans téléphone.

C'est l'outil de calibration : chaque scénario porte un bloc `expect`, et le
script sort en erreur si le score ne tombe pas dans la fourchette attendue. Les
cinq scénarios de Biz 1 deviennent donc une suite de non-régression — en
particulier les deux appels légitimes, qui doivent rester sous 40.

    uv run python scripts/replay_scenario.py scenarios/scam_01_petit_fils.json
    uv run python scripts/replay_scenario.py scenarios/*.json --real-llm
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vocalguard.application.pipeline import ScoringPipeline
from vocalguard.application.session import SessionRegistry
from vocalguard.config import get_settings
from vocalguard.domain.events import Event, EventType
from vocalguard.domain.models import TranscriptSegment
from vocalguard.infrastructure.contacts.book import JsonContactBook
from vocalguard.infrastructure.fakes.doubles import (
    FakeContentAnalyzer,
    RecordingCallController,
)
from vocalguard.infrastructure.keywords.detector import KeywordDetector
from vocalguard.infrastructure.telephony.reputation import (
    TwilioReputationChecker,
)

RESET = "\033[0m"
DIM = "\033[2m"
LEVEL_COLOR = {
    "none": "\033[32m",
    "watch": "\033[33m",
    "alert": "\033[35m",
    "intervene": "\033[31m",
}
TYPE_COLOR = {
    EventType.TRANSCRIPT: DIM,
    EventType.KEYWORD: "\033[36m",
    EventType.LLM: "\033[34m",
    EventType.REPUTATION: "\033[36m",
    EventType.VOICE: "\033[36m",
    EventType.ACTION: "\033[1;31m",
}


class ManualClock:
    """Horloge pilotée par le scénario : la trace est reproductible à l'identique."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def _fmt(event: Event) -> str:
    d = event.data
    head = f"{DIM}{event.t:6.1f}s{RESET} "
    color = TYPE_COLOR.get(event.type, "")
    label = f"{color}{event.type.value:<10}{RESET}"

    match event.type:
        case EventType.TRANSCRIPT:
            who = "appelant" if event.speaker == "caller" else "victime"
            return f"{head}{label}{DIM}[{who}]{RESET} {d.get('text', '')}"
        case EventType.KEYWORD:
            terms = ", ".join(d.get("terms", [])) or "—"
            return f"{head}{label}risk={d.get('risk', 0):.2f}  {DIM}{terms}{RESET}"
        case EventType.LLM:
            tactics = ", ".join(d.get("tactics", [])) or "—"
            who = d.get("claimed_identity") or "—"
            return (
                f"{head}{label}risk={d.get('risk', 0):.2f}  {tactics}  {DIM}identité: {who}{RESET}"
            )
        case EventType.REPUTATION:
            reasons = ", ".join(d.get("reasons", [])) or "—"
            flag = "  MISMATCH" if d.get("identity_mismatch") else ""
            return (
                f"{head}{label}risk={d.get('risk', 0):.2f}  "
                f"{d.get('line_type', '?')}  {DIM}{reasons}{RESET}{flag}"
            )
        case EventType.VOICE:
            return (
                f"{head}{label}risk={d.get('risk', 0):.2f}  "
                f"{DIM}{d.get('windows_analyzed', 0)} fenêtres{RESET}"
            )
        case EventType.SCORE:
            level = d.get("level", "none")
            col = LEVEL_COLOR.get(level, "")
            breakers = d.get("breakers_fired") or []
            suffix = f"  {DIM}coupe-circuits: {', '.join(breakers)}{RESET}" if breakers else ""
            return f"{head}{label}{col}{d.get('score', 0):3d}  {level:<10}{RESET}{suffix}"
        case EventType.ACTION:
            ok = "ok" if d.get("ok") else "ÉCHEC"
            line = f"{head}{label}{d.get('action')} -> {d.get('target')}  [{ok}]"
            spoken = str(d.get("message", "")).strip()
            if spoken:
                line += f"\n{' ' * 19}{DIM}« {spoken} »{RESET}"
            return line
    return f"{head}{label}{d}"


class TracingSink:
    """Affiche chaque événement et retient le dernier score."""

    def __init__(self, *, quiet: bool = False) -> None:
        self.quiet = quiet
        self.events: list[Event] = []

    async def publish(self, event: Event) -> None:
        self.events.append(event)
        if not self.quiet:
            print(_fmt(event))

    def last_score(self) -> tuple[int, str]:
        scores = [e for e in self.events if e.type is EventType.SCORE]
        if not scores:
            return 0, "none"
        d = scores[-1].data
        return int(d.get("score", 0)), str(d.get("level", "none"))

    def peak_score(self) -> int:
        return max(
            (int(e.data.get("score", 0)) for e in self.events if e.type is EventType.SCORE),
            default=0,
        )


def build_pipeline(
    sink: TracingSink, clock: ManualClock, *, real_llm: bool
) -> tuple[ScoringPipeline, RecordingCallController]:
    settings = get_settings()
    contacts = JsonContactBook.from_path(settings.contacts_path)
    controller = RecordingCallController()

    if real_llm:
        from vocalguard.infrastructure.aws.bedrock_analyzer import BedrockContentAnalyzer

        # Le scénario est rejoué en accéléré : la cadence anti-coût n'a pas de
        # sens ici, on veut une analyse à chaque tour.
        analyzer = BedrockContentAnalyzer(min_interval_seconds=0.0)
    else:
        analyzer = FakeContentAnalyzer()

    pipeline = ScoringPipeline(
        event_sink=sink,
        keyword_engine=KeywordDetector(),
        content_analyzer=analyzer,
        reputation_checker=TwilioReputationChecker(contacts=contacts),
        contact_book=contacts,
        call_controller=controller,
        trusted_contact_number=settings.trusted_contact_phone_number or "+33600000000",
        weights=settings.weights(),
        thresholds=settings.thresholds(),
        registry=SessionRegistry(clock=clock),
    )
    return pipeline, controller


async def replay(scenario: dict[str, Any], *, speed: float, real_llm: bool, quiet: bool) -> bool:
    clock = ManualClock()
    sink = TracingSink(quiet=quiet)
    pipeline, _ = build_pipeline(sink, clock, real_llm=real_llm)

    call_id = scenario.get("call_id", "CA_REPLAY")
    title = scenario.get("title", Path("scénario").name)
    print(f"\n\033[1m▶ {title}\033[0m  {DIM}{call_id}{RESET}")
    if scenario.get("notes"):
        print(f"{DIM}  {scenario['notes']}{RESET}")
    print()

    await pipeline.start_call(call_id, caller_number=scenario.get("caller_number", ""))

    previous_t = 0.0
    for turn in scenario.get("turns", []):
        t = float(turn.get("t", previous_t))
        if speed > 0:
            await asyncio.sleep(max(0.0, (t - previous_t) / speed))
        previous_t = t
        clock.value = t

        segment = TranscriptSegment(
            speaker=turn.get("speaker", "caller"),
            text=turn.get("text", ""),
            t=t,
            is_final=True,
        )
        await sink.publish(
            Event(
                call_id=call_id,
                t=t,
                type=EventType.TRANSCRIPT,
                speaker=segment.speaker,
                data={"text": segment.text, "is_final": True},
            )
        )
        await pipeline.on_transcript(call_id, segment)
        # Laisse l'analyse de contenu, qui tourne en tâche de fond, se poser
        # avant le tour suivant — sinon la trace est illisible.
        await pipeline.aclose()

    await pipeline.aclose()
    await pipeline.end_call(call_id)
    return _verdict(scenario, sink)


def _verdict(scenario: dict[str, Any], sink: TracingSink) -> bool:
    expect = scenario.get("expect") or {}
    score, level = sink.last_score()
    peak = sink.peak_score()
    print(f"\n  score final {score}  ({level}) — pic à {peak}")

    if not expect:
        return True

    problems: list[str] = []
    if "min_score" in expect and peak < int(expect["min_score"]):
        problems.append(f"pic {peak} < min_score {expect['min_score']}")
    if "max_score" in expect and peak > int(expect["max_score"]):
        problems.append(f"pic {peak} > max_score {expect['max_score']}")
    if "level" in expect and level != expect["level"]:
        problems.append(f"niveau {level} != {expect['level']}")

    if problems:
        print(f"  \033[31m✗ {'; '.join(problems)}{RESET}")
        return False
    print(f"  \033[32m✓ conforme aux attentes du scénario{RESET}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenarios", nargs="+", type=Path)
    parser.add_argument(
        "--speed",
        type=float,
        default=0.0,
        help="0 = instantané (défaut), 1 = temps réel, 4 = 4x plus vite",
    )
    parser.add_argument(
        "--real-llm", action="store_true", help="Appelle vraiment Claude sur Bedrock"
    )
    parser.add_argument("--quiet", action="store_true", help="Ne montre que le verdict")
    args = parser.parse_args()

    ok = True
    for path in args.scenarios:
        scenario = json.loads(path.read_text(encoding="utf-8"))
        ok &= asyncio.run(
            replay(scenario, speed=args.speed, real_llm=args.real_llm, quiet=args.quiet)
        )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
