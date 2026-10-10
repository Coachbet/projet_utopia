"""Un scénario rejoué produit les mêmes seuils en base que sur le bus."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from vocalguard.application.history import HistoryRecorder
from vocalguard.application.scoring import ScoringPipeline
from vocalguard.application.sessions import SessionRegistry
from vocalguard.config import Settings
from vocalguard.domain.models import TranscriptSegment
from vocalguard.infrastructure.contacts.book import JsonContactBook
from vocalguard.infrastructure.fakes.doubles import FakeContentAnalyzer
from vocalguard.infrastructure.fakes.event_sink import FanOutSink, InMemoryEventSink
from vocalguard.infrastructure.fakes.transcriber import FakeTranscriber
from vocalguard.infrastructure.history.sqlite import SQLiteCallHistory
from vocalguard.infrastructure.keywords.detector import KeywordDetector
from vocalguard.infrastructure.telephony.reputation import TwilioReputationChecker
from vocalguard.interface.lifecycle import release_call

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "scenario_name", ["scam_01_petit_fils", "scam_02_faux_conseiller_bancaire"]
)
async def test_replayed_scenario_history_matches_real_pipeline_bus(
    tmp_path: Path,
    scenario_name: str,
) -> None:
    scenario = json.loads((ROOT / "scenarios" / f"{scenario_name}.json").read_text())
    settings = Settings(_env_file=None, twilio_lookup_enabled=False)
    contacts = JsonContactBook.from_path(ROOT / "data" / "contacts.example.json")
    sink = InMemoryEventSink()
    fanout = FanOutSink(sink)
    clock = [0.0]
    registry = SessionRegistry(
        transcriber_factory=FakeTranscriber,
        sink=fanout,
        settings=settings,
        clock=lambda: clock[0],
    )
    store = SQLiteCallHistory(tmp_path / "scenario.sqlite")
    await store.initialize()
    recorder = HistoryRecorder(store=store, registry=registry, contacts=contacts)
    pipeline = ScoringPipeline(
        registry=registry,
        keyword_engine=KeywordDetector(),
        content_analyzer=FakeContentAnalyzer(),
        reputation_checker=TwilioReputationChecker(contacts=contacts, settings=settings),
        settings=settings,
    )
    fanout.add(recorder)
    fanout.add(pipeline)
    call_id = scenario["call_id"]
    session = await registry.open(call_id, caller_number=scenario["caller_number"])
    await recorder.start_call(call_id, caller_number=scenario["caller_number"])
    await recorder.mark_answered(call_id)
    for turn in scenario["turns"]:
        clock[0] = float(turn["t"])
        await session.on_segment(
            TranscriptSegment(
                speaker=turn["speaker"],
                text=turn["text"],
                t=clock[0],
                is_final=True,
            )
        )
        await pipeline.aclose()
    clock[0] += 5
    await release_call(
        SimpleNamespace(
            registry=registry,
            pipeline=pipeline,
            history_recorder=recorder,
        ),
        call_id,
    )
    record = await store.get_call(call_id)
    assert record is not None
    assert record.answered is True
    assert record.ended_at is not None
    assert record.duration_s == clock[0]
    assert record.max_score == max(event.data["score"] for event in sink.by_type("score"))
    assert record.max_score >= scenario["expect"]["min_score"]
    for threshold, field in ((40, "watch_at_s"), (65, "alert_at_s"), (85, "intervene_at_s")):
        expected = min(
            event.t for event in sink.by_type("score") if event.data["score"] >= threshold
        )
        assert getattr(record, field) == expected
    raw = store.path.read_bytes()
    # Phrase complète issue du transcript : les explications et tactiques sont
    # autorisées, mais aucun tour de parole ne peut être retrouvé en base.
    for turn in scenario["turns"]:
        assert turn["text"].encode() not in raw
