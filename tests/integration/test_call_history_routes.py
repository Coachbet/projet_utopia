"""API historique : authentification, pagination et suppression réelles."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vocalguard.config import Settings
from vocalguard.domain.events import Event, EventType
from vocalguard.domain.history import CallRecord, utc_iso
from vocalguard.infrastructure.fakes.event_sink import InMemoryEventSink
from vocalguard.infrastructure.fakes.transcriber import FakeTranscriber
from vocalguard.infrastructure.history.sqlite import SQLiteCallHistory
from vocalguard.interface.app import create_app
from vocalguard.interface.http.phone_app import router

PIN = "432198"


@pytest.fixture
def client(tmp_path: Path):
    app = FastAPI()
    app.state.settings = SimpleNamespace(app_pin=PIN)
    app.state.history = SQLiteCallHistory(tmp_path / "calls.sqlite")
    app.include_router(router)
    with TestClient(app) as test_client:
        test_client.portal.call(app.state.history.initialize)
        for i in range(53):
            test_client.portal.call(
                app.state.history.save,
                CallRecord(
                    f"CA_{i:03}",
                    utc_iso(datetime(2026, 10, 10, tzinfo=UTC)),
                    caller_number="+33612345678",
                    country="FR",
                ),
            )
        yield test_client, app


@pytest.mark.parametrize("route", ["/app/history", "/app/history/CA_001", "/app/history/clear"])
def test_all_history_routes_require_pin(client, route: str) -> None:
    http, _ = client
    denied = http.post(route, json={"pin": "000000"})
    assert denied.status_code == 403
    assert "+33612345678" not in denied.text
    assert "CA_001" not in denied.text


def test_list_detail_and_clear(client) -> None:
    http, _ = client
    first = http.post("/app/history", json={"pin": PIN}).json()
    assert len(first["items"]) == 50
    assert first["total"] == 53
    assert first["has_more"] is True
    assert first["enabled"] is True
    second = http.post("/app/history", json={"pin": PIN, "offset": 50}).json()
    assert len(second["items"]) == 3
    assert second["has_more"] is False
    detail = http.post("/app/history/CA_001", json={"pin": PIN})
    assert detail.status_code == 200
    assert detail.json()["country"] == "FR"
    assert detail.json()["max_score"] is None
    assert http.post("/app/history/absent", json={"pin": PIN}).status_code == 404
    cleared = http.post("/app/history/clear", json={"pin": PIN})
    assert cleared.json() == {"ok": True, "deleted": 53}
    assert http.post("/app/history", json={"pin": PIN}).json()["total"] == 0


@pytest.mark.parametrize("body", [{"limit": 51}, {"limit": 0}, {"offset": -1}])
def test_invalid_pagination_rejected(client, body: dict[str, int]) -> None:
    http, _ = client
    assert http.post("/app/history", json={"pin": PIN, **body}).status_code == 422


def test_shared_pin_lockout_applies_to_history_and_phone_routes(client) -> None:
    http, _ = client
    routes = ["/app/history", "/app/history/CA_001", "/app/history/clear", "/app/incoming"]
    for i in range(5):
        response = http.post(routes[i % 4], json={"pin": "000000"})
    assert response.status_code == 429
    assert http.post("/app/history", json={"pin": PIN}).status_code == 429


def test_disabled_history_returns_explicit_empty_state(client) -> None:
    http, app = client
    app.state.history.enabled = False
    result = http.post("/app/history", json={"pin": PIN}).json()
    assert result["enabled"] is False
    assert result["items"] == []
    assert result["total"] == 0


@pytest.mark.parametrize("trusted", [False, True])
def test_full_application_records_routing_and_finalizes_on_caller_status(
    tmp_path: Path,
    trusted: bool,
) -> None:
    contacts = tmp_path / "contacts.json"
    contacts.write_text(
        '{"contacts":[{"name":"Lucas","phone_number":"+33612345678","relation":"petit-fils"}]}'
        if trusted
        else '{"contacts":[]}',
        encoding="utf-8",
    )
    settings = Settings(
        _env_file=None,
        transcribe_backend="fake",
        contacts_path=contacts,
        public_hostname="test.invalid",
        twilio_auth_token="",
        app_pin=PIN,
        history_enabled=True,
        history_path=tmp_path / "full.sqlite",
    )
    app = create_app(settings, sink=InMemoryEventSink(), transcriber_factory=FakeTranscriber)
    with TestClient(app) as http:
        opened = http.post(
            "/twilio/voice/inbound",
            data={"CallSid": "CA_full_history", "From": "+33612345678", "To": "+15555551234"},
        )
        assert opened.status_code == 200
        http.portal.call(app.state.history_recorder.mark_answered, "CA_full_history")
        http.portal.call(
            app.state.sink.emit,
            Event(call_id="CA_full_history", type=EventType.SCORE, t=4, data={"score": 90}),
        )
        closed = http.post(
            "/twilio/voice/status",
            data={"CallSid": "CA_full_history", "CallStatus": "completed"},
        )
        assert closed.status_code == 200
        record = http.post("/app/history/CA_full_history", json={"pin": PIN}).json()
        assert record["ended_at"] is not None
        assert record["duration_s"] >= 0
        assert record["answered"] is True
        assert record["origin"] == ("contact" if trusted else "inconnu")
        assert record["route"] == ("trusted_passthrough" if trusted else "analyzed")
        assert record["max_score"] == (None if trusted else 90)
        assert record["intervene_at_s"] == (None if trusted else 4)
        assert app.state.registry.get("CA_full_history") is None
