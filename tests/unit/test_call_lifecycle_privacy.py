"""Résultats AWS livrés pendant la fermeture : aucune conversation ressuscitée."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from vocalguard.application.sessions import SessionRegistry
from vocalguard.config import Settings
from vocalguard.domain.models import TranscriptSegment
from vocalguard.infrastructure.fakes.event_sink import FanOutSink, InMemoryEventSink
from vocalguard.infrastructure.fakes.transcriber import FakeTranscriber
from vocalguard.interface.event_hub import EventHub
from vocalguard.interface.lifecycle import release_call


async def test_stream_final_results_cannot_reintroduce_finished_conversation() -> None:
    class FinalizingTranscriber(FakeTranscriber):
        async def open(self, *, speaker, on_segment):
            stream = await super().open(speaker=speaker, on_segment=on_segment)
            original_close = stream.close

            async def close_with_final_segment() -> None:
                await on_segment(TranscriptSegment(speaker, "SECRET_LAST_TRANSCRIPT_4821", 3, True))
                await original_close()

            stream.close = close_with_final_segment
            return stream

    hub = EventHub()
    sink = InMemoryEventSink()
    transcriber = FinalizingTranscriber()
    registry = SessionRegistry(
        transcriber_factory=lambda: transcriber,
        sink=FanOutSink(hub, sink),
        settings=Settings(_env_file=None),
    )
    session = await registry.open("CA_closing")
    await transcriber.say("caller", "Conversation en cours", t=1)
    assert len(session.history) == 1
    assert len(tuple(hub.replay())) == 1
    await release_call(SimpleNamespace(registry=registry, dashboard_hub=hub), "CA_closing")
    await transcriber.say("caller", "SECRET_AFTER_RELEASE_4821", t=4)
    assert tuple(hub.replay()) == ()
    assert registry.get("CA_closing") is None
    assert session.history == []
    assert len(sink.events) == 1
    assert all(stream.closed for stream in transcriber.streams.values())


async def test_cleanup_cancels_consumers_before_await_and_captures_end_duration() -> None:
    order = []
    clock = [7.0]
    session = SimpleNamespace(t=lambda: clock[0])

    async def close(call_id: str) -> None:
        order.append("close")
        clock[0] = 99.0

    async def cancel(call_id: str) -> None:
        order.append("cancel_ringing")

    async def finalize(call_id: str, *, duration_s: float) -> None:
        order.append("finalize")
        assert duration_s == 7

    state = SimpleNamespace(
        registry=SimpleNamespace(get=lambda call_id: session, close=close),
        pipeline=SimpleNamespace(forget=lambda call_id: order.append("pipeline")),
        verifier=SimpleNamespace(forget=lambda call_id: order.append("verifier")),
        controller=SimpleNamespace(
            forget=lambda call_id: order.append("controller"),
            cancel_pending_victim=cancel,
        ),
        history_recorder=SimpleNamespace(finalize=finalize),
        dashboard_hub=SimpleNamespace(forget=lambda call_id: order.append("dashboard")),
    )
    await release_call(state, "CA_closing")
    assert order == [
        "pipeline",
        "verifier",
        "close",
        "cancel_ringing",
        "finalize",
        "controller",
        "dashboard",
    ]


@pytest.mark.parametrize("failure", ["lookup", "duration"])
async def test_cleanup_continues_when_session_metadata_is_unavailable(failure: str) -> None:
    session = SimpleNamespace(t=Mock(side_effect=RuntimeError("clock unavailable")))
    registry = SimpleNamespace(get=Mock(return_value=session), close=AsyncMock())
    if failure == "lookup":
        registry.get.side_effect = RuntimeError("registry unavailable")
    recorder = SimpleNamespace(finalize=AsyncMock())
    controller = SimpleNamespace(cancel_pending_victim=AsyncMock(), forget=Mock())
    hub = SimpleNamespace(forget=Mock())

    await release_call(
        SimpleNamespace(
            registry=registry, history_recorder=recorder, controller=controller, dashboard_hub=hub
        ),
        "CA_unavailable",
    )

    registry.close.assert_awaited_once_with("CA_unavailable")
    controller.cancel_pending_victim.assert_awaited_once_with("CA_unavailable")
    recorder.finalize.assert_awaited_once_with("CA_unavailable", duration_s=None)
    controller.forget.assert_called_once_with("CA_unavailable")
    hub.forget.assert_called_once_with("CA_unavailable")
