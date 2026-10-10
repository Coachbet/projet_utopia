"""Persistance de métriques : confidentialité, seuils, concurrence et rétention."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from vocalguard.application.history import HistoryRecorder
from vocalguard.domain.alerts import Thresholds
from vocalguard.domain.events import Event, EventType
from vocalguard.domain.history import CallRecord, caller_origin, utc_iso
from vocalguard.domain.models import Contact
from vocalguard.domain.ports import CallHistory
from vocalguard.infrastructure.fakes.doubles import StaticContactBook
from vocalguard.infrastructure.history.sqlite import SQLiteCallHistory

NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)


@pytest.fixture
async def history(tmp_path: Path) -> SQLiteCallHistory:
    store = SQLiteCallHistory(tmp_path / "calls.sqlite")
    await store.initialize(now=NOW)
    return store


def recorder(store: SQLiteCallHistory, *, trusted: bool = False) -> HistoryRecorder:
    number = "+33612345678"
    contacts = StaticContactBook([Contact("Lucas", number, "petit-fils")] if trusted else [])
    session = SimpleNamespace(caller_number=number)
    registry = SimpleNamespace(get=lambda call_id: session if call_id == "CA_history" else None)
    return HistoryRecorder(store=store, registry=registry, contacts=contacts)


async def event(
    sink: HistoryRecorder,
    type: EventType,
    t: float = 0,
    **data: object,
) -> None:
    await sink.emit(Event(call_id="CA_history", type=type, t=t, data=data))


async def test_adapter_conforms_and_has_only_metadata_columns(history: SQLiteCallHistory) -> None:
    assert isinstance(history, CallHistory)
    with sqlite3.connect(history.path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(calls)")}
    assert columns == set(CallRecord("id", utc_iso()).to_dict())
    assert not columns & {"transcript", "text", "audio", "pcm16", "code", "claimed_identity"}
    assert history.path.stat().st_mode & 0o777 == 0o600


async def test_one_row_start_retry_finalize_is_idempotent(history: SQLiteCallHistory) -> None:
    sink = recorder(history)
    await sink.start_call("CA_history", caller_number="+33612345678", started_at=NOW)
    await sink.start_call("CA_history", caller_number="+442012345678", started_at=NOW)
    await sink.mark_answered("CA_history")
    await sink.finalize("CA_history", duration_s=20, ended_at=NOW + timedelta(seconds=20))
    await sink.finalize("CA_history", duration_s=999)
    await event(sink, EventType.SCORE, 30, score=99)
    await sink.start_call("CA_history", caller_number="+33612345678")
    page = await history.list_calls()
    assert page.total == 1
    row = page.items[0]
    assert row.caller_number == "+33612345678"
    assert row.country == "FR"
    assert row.origin == "inconnu"
    assert row.duration_s == 20
    assert row.answered is True
    assert row.ended_at == utc_iso(NOW + timedelta(seconds=20))
    assert row.max_score is None


async def test_all_threshold_first_times_and_signal_peaks(history: SQLiteCallHistory) -> None:
    sink = recorder(history)
    await sink.start_call("CA_history", caller_number="+33612345678")
    for type, first, lower in (
        (EventType.LLM, 0.95, 0.7),
        (EventType.KEYWORD, 0.8, 0.2),
        (EventType.VOICE, 0.6, 0.3),
        (EventType.REPUTATION, 0.7, 0.1),
    ):
        await event(sink, type, 2, risk=first)
        await event(sink, type, 3, risk=lower)
    for t, score in ((2, 20), (4, 40), (8, 66), (12, 90), (14, 20), (16, 99)):
        await event(sink, EventType.SCORE, t, score=score)
    await event(sink, EventType.LLM, 18, risk=0.2, tactics=["urgence", "paiement"])
    await event(
        sink, EventType.LLM, 19, risk=0.1, tactics=["urgence"], explanation="Un virement urgent."
    )
    row = await history.get_call("CA_history")
    assert row is not None
    assert (row.watch_at_s, row.alert_at_s, row.intervene_at_s) == (4, 8, 12)
    assert row.max_score == 99
    assert row.max_level == "intervene"
    assert (row.peak_llm, row.peak_keywords, row.peak_voice, row.peak_reputation) == (
        0.95,
        0.8,
        0.6,
        0.7,
    )
    assert row.tactics == ["urgence", "paiement"]
    assert row.explanation == "Un virement urgent."


async def test_score_leap_marks_all_three_thresholds_at_same_time(
    history: SQLiteCallHistory,
) -> None:
    sink = recorder(history)
    await event(sink, EventType.SCORE, 6.25, score=89, level="intervene")
    row = await history.get_call("CA_history")
    assert row is not None
    assert (row.watch_at_s, row.alert_at_s, row.intervene_at_s) == (6.25, 6.25, 6.25)


async def test_custom_thresholds_used_for_crossings(history: SQLiteCallHistory) -> None:
    sink = recorder(history)
    sink._thresholds = Thresholds(watch=25, alert=55, intervene=75)
    await event(sink, EventType.SCORE, 3, score=55)
    row = await history.get_call("CA_history")
    assert row is not None
    assert (row.watch_at_s, row.alert_at_s, row.intervene_at_s) == (3, 3, None)


async def test_raw_transcripts_audio_codes_and_event_fields_never_reach_sqlite(
    history: SQLiteCallHistory,
) -> None:
    sink = recorder(history)
    marker = "NEVER_PERSIST_SECRET_TRANSCRIPT_938402"
    await sink.start_call("CA_history", caller_number="+33612345678")
    await event(sink, EventType.TRANSCRIPT, 5, text=marker, is_final=True)
    await event(sink, EventType.LLM, 5, risk=0.8, text=marker, claimed_identity=marker)
    await event(sink, EventType.KEYWORD, 6, risk=0.3, terms=[marker], audio=marker)
    await event(sink, EventType.SCORE, 6, score=60, explanation=marker, reasons=[marker])
    await event(sink, EventType.ACTION, 7, action="verify_requested", ok=True, code=marker)
    await sink.finalize("CA_history", duration_s=10)
    for file in history.path.parent.glob("calls.sqlite*"):
        assert marker.encode() not in file.read_bytes()
    row = await history.get_call("CA_history")
    assert row is not None
    assert row.explanation == ""
    assert row.actions == ["verify_requested"]


async def test_trusted_victim_only_events_never_store_analysis(history: SQLiteCallHistory) -> None:
    sink = recorder(history, trusted=True)
    await sink.start_call(
        "CA_history",
        caller_number="+33612345678",
        route="trusted_passthrough",
    )
    await event(sink, EventType.LLM, 2, risk=0.9, tactics=["urgence"], explanation="Sensible")
    await event(sink, EventType.SCORE, 4, score=99, level="intervene")
    await event(sink, EventType.REPUTATION, 4, risk=1, contact_denied=True)
    row = await history.get_call("CA_history")
    assert row is not None
    assert row.caller_name == "Lucas"
    assert row.relation == "petit-fils"
    assert row.origin == "contact"
    assert row.max_score is row.max_level is row.peak_llm is row.peak_reputation is None
    assert row.watch_at_s is row.alert_at_s is row.intervene_at_s is None
    assert row.tactics == []
    assert row.explanation == ""


async def test_late_trusted_routing_clears_preceding_metrics(history: SQLiteCallHistory) -> None:
    sink = recorder(history)
    await event(sink, EventType.SCORE, 0, score=90)
    await event(sink, EventType.ACTION, 0, action="trusted_passthrough", ok=True)
    row = await history.get_call("CA_history")
    assert row is not None
    assert row.route == "trusted_passthrough"
    assert row.max_score is None
    assert row.intervene_at_s is None


@pytest.mark.parametrize(
    ("action", "ok", "verification", "actions"),
    [
        ("verify_requested", True, "none", ["verify_requested"]),
        ("identity_verified", True, "verified", ["identity_verified"]),
        ("identity_verified", False, "failed", []),
        ("verify_expired", False, "expired", ["verify_expired"]),
        ("hold_caller", False, "none", []),
        ("hold_caller", True, "none", ["hold_caller"]),
        ("arbitrary-secret", True, "none", []),
    ],
)
async def test_action_whitelist_and_verification_outcomes(
    history: SQLiteCallHistory,
    action: str,
    ok: bool,
    verification: str,
    actions: list[str],
) -> None:
    sink = recorder(history)
    await event(sink, EventType.ACTION, action=action, ok=ok)
    await event(sink, EventType.ACTION, action=action, ok=ok)
    row = await history.get_call("CA_history")
    assert row is not None
    assert row.verification == verification
    assert row.actions == actions


async def test_reputation_line_type_and_contact_denial(history: SQLiteCallHistory) -> None:
    sink = recorder(history)
    await event(sink, EventType.REPUTATION, risk=1, line_type="nonFixedVoip", contact_denied=True)
    row = await history.get_call("CA_history")
    assert row is not None
    assert row.line_type == "nonFixedVoip"
    assert row.verification == "failed"
    assert row.actions == ["contact_denied"]


async def test_retention_boundary_and_restart_purges(history: SQLiteCallHistory) -> None:
    for id, age in (("old", 31), ("boundary", 30), ("recent", 1)):
        await history.save(CallRecord(id, utc_iso(NOW - timedelta(days=age))))
    reopened = SQLiteCallHistory(history.path, retention_days=30)
    await reopened.initialize(now=NOW)
    assert [item.call_id for item in (await reopened.list_calls()).items] == ["recent", "boundary"]


async def test_pagination_max_50_and_deterministic_order(history: SQLiteCallHistory) -> None:
    await asyncio.gather(*(history.save(CallRecord(f"CA_{i:03}", utc_iso(NOW))) for i in range(56)))
    first = await history.list_calls()
    second = await history.list_calls(offset=50)
    assert first.total == second.total == 56
    assert len(first.items) == 50
    assert len(second.items) == 6
    assert first.items[0].call_id == "CA_055"
    assert first.to_dict()["has_more"] is True
    assert second.to_dict()["has_more"] is False
    with pytest.raises(ValueError):
        await history.list_calls(limit=51)


async def test_clear_does_not_resurrect_an_active_call(history: SQLiteCallHistory) -> None:
    sink = recorder(history)
    await event(sink, EventType.SCORE, score=80)
    assert await sink.clear() == 1
    await event(sink, EventType.SCORE, score=99)
    await sink.finalize("CA_history", duration_s=12)
    assert (await history.list_calls()).total == 0


async def test_disabled_is_noop_and_never_creates_file(tmp_path: Path) -> None:
    store = SQLiteCallHistory(tmp_path / "absent" / "calls.sqlite", enabled=False)
    sink = recorder(store)
    await store.initialize()
    await sink.start_call("CA_history")
    await event(sink, EventType.SCORE, score=90)
    await sink.finalize("CA_history")
    await store.save(CallRecord("id", utc_iso()))
    assert (await store.list_calls()).total == 0
    assert await store.get_call("id") is None
    assert await store.clear() == 0
    assert not store.path.parent.exists()


async def test_database_write_failure_never_interrupts_event_pipeline(
    history: SQLiteCallHistory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = recorder(history)
    save = history.save

    async def unavailable(record: CallRecord) -> None:
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(history, "save", unavailable)
    await sink.start_call("CA_history")
    await event(sink, EventType.SCORE, 4, score=90)
    monkeypatch.setattr(history, "save", save)
    await event(sink, EventType.SCORE, 8, score=20)
    row = await history.get_call("CA_history")
    assert row is not None
    assert row.max_score == 90
    assert row.intervene_at_s == 4


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 101, "90", True])
async def test_bad_numeric_metrics_ignored(history: SQLiteCallHistory, value: object) -> None:
    sink = recorder(history)
    await event(sink, EventType.SCORE, score=value)
    await event(sink, EventType.LLM, risk=value)
    row = await history.get_call("CA_history")
    assert row is not None
    assert row.max_score is None
    assert row.peak_llm is None


@pytest.mark.parametrize(
    ("number", "contact", "origin", "country"),
    [
        ("", False, "masqué", None),
        ("anonymous", False, "masqué", None),
        ("+33612345678", False, "inconnu", "FR"),
        ("+33612345678", True, "contact", "FR"),
        ("+442012345678", False, "étranger", "GB"),
        ("+441481123456", False, "étranger", "GG"),
        ("+14155551234", False, "étranger", "US"),
        ("+14165551234", False, "étranger", "CA"),
        ("+18005551234", False, "étranger", None),
        ("+262123456789", False, "étranger", None),
    ],
)
def test_origin_does_not_guess_shared_country_codes(
    number: str,
    contact: bool,
    origin: str,
    country: str | None,
) -> None:
    assert caller_origin(number, is_contact=contact) == (origin, country)
