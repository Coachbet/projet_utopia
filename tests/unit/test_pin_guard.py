import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from vocalguard.config import Settings
from vocalguard.interface.auth import PinGuard, PinLogFilter
from vocalguard.interface.event_hub import EventHub
from vocalguard.interface.http import control, phone_app
from vocalguard.interface.ws.dashboard import create_dashboard_router

PIN = "432198"


def test_twilio_signature_validation_is_secure_by_default(monkeypatch):
    monkeypatch.delenv("TWILIO_VALIDATE_SIGNATURE", raising=False)
    assert Settings(_env_file=None).twilio_validate_signature is True


def test_lockout_expires_and_even_correct_pin_is_blocked():
    now = [100.0]
    guard = PinGuard(clock=lambda: now[0])
    for _ in range(4):
        with pytest.raises(HTTPException) as error:
            guard.check(PIN, "000000")
        assert error.value.status_code == 403
    for received in ("000000", PIN):
        with pytest.raises(HTTPException) as error:
            guard.check(PIN, received)
        assert error.value.status_code == 429
        assert error.value.headers["Retry-After"] == "600"
    now[0] += 599
    with pytest.raises(HTTPException):
        guard.check(PIN, PIN)
    now[0] += 1
    guard.check(PIN, PIN)


@pytest.mark.parametrize(
    "pin", ["", "12345", "12345a", "\uff11\uff12\uff13\uff14\uff15\uff16", "123 456"]
)
def test_unsafe_server_configuration_refuses_every_client(pin):
    with pytest.raises(HTTPException) as error:
        PinGuard().check(pin, pin)
    assert error.value.status_code == 503


def test_a_success_resets_consecutive_failures():
    guard = PinGuard()
    for _ in range(4):
        with pytest.raises(HTTPException):
            guard.check(PIN, "bad")
    guard.check(PIN, PIN)
    with pytest.raises(HTTPException) as error:
        guard.check(PIN, "bad")
    assert error.value.status_code == 403


def test_controls_and_websocket_share_app_lockout():
    app = FastAPI()
    app.state.settings = Settings(_env_file=None, app_pin=PIN)
    app.state.registry = SimpleNamespace(active=lambda: [])
    app.include_router(phone_app.router)
    app.include_router(control.router)
    app.include_router(create_dashboard_router(EventHub()))
    with TestClient(app) as client:
        for route in ("/app/incoming", "/control/calls/missing/hold", "/app/incoming"):
            assert client.post(route, json={}).status_code == 403
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect("/ws/dashboard?pin=000000"):
                pass
        assert error.value.code == 1008
        assert client.post("/control/calls/missing/verify").status_code == 429
        assert client.post("/app/incoming", json={"pin": PIN}).status_code == 429
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/ws/dashboard?pin={PIN}"):
                pass


@pytest.mark.parametrize(
    "path", ["/ws/dashboard?pin=", "/ws/dashboard?p%69n=", "/ws/%64ashboard?pin="]
)
def test_pin_is_removed_from_uvicorn_log_arguments(path):
    record = logging.LogRecord(
        "uvicorn.error", logging.INFO, "", 0, "WebSocket %s", (f"{path}{PIN}",), None
    )
    assert PinLogFilter().filter(record)
    assert PIN not in record.getMessage()
    assert "[masqué]" in record.getMessage()
