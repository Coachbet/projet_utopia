"""Signature du handshake vérifiée avant tout audio, session ou accès AWS."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from twilio.request_validator import RequestValidator

from vocalguard.config import Settings
from vocalguard.interface.ws.media import router

HOST = "public.ngrok.example"
TOKEN = "twilio-private-test-secret"
PUBLIC_URL = f"wss://{HOST}/ws/media"


def harness(**overrides):
    app = FastAPI()
    app.include_router(router)
    app.state.settings = Settings(
        _env_file=None,
        **{
            "public_hostname": HOST,
            "twilio_auth_token": TOKEN,
            "twilio_validate_signature": True,
            **overrides,
        },
    )
    # Un émetteur non authentifié ne doit même pas lire le registre. Toute
    # ouverture (et donc toute fabrique Transcribe/AWS) serait une régression.
    registry = SimpleNamespace(
        get=MagicMock(return_value=None),
        open=AsyncMock(side_effect=AssertionError("session opened unexpectedly")),
        close=AsyncMock(),
    )
    app.state.registry = registry
    return app, registry


def signed(url: str = PUBLIC_URL, *, token: str = TOKEN) -> dict[str, str]:
    return {"X-Twilio-Signature": RequestValidator(token).compute_signature(url, {})}


@pytest.mark.parametrize("headers", [{}, {"X-Twilio-Signature": "forged-signature"}])
def test_signature_absente_ou_fausse_refuse_avant_accept_et_tout_etat(headers):
    app, registry = harness()
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect("/ws/media", headers=headers):
                pytest.fail("unauthenticated handshake accepted")
        assert error.value.code == 1008
    registry.get.assert_not_called()
    registry.open.assert_not_awaited()
    registry.close.assert_not_awaited()


@pytest.mark.parametrize("url", [PUBLIC_URL, PUBLIC_URL + "/"])
def test_signature_wss_publique_valide_et_variante_slash_documentee_acceptent(url):
    app, registry = harness()
    with TestClient(app) as client, client.websocket_connect("/ws/media", headers=signed(url)):
        pass
    registry.open.assert_not_awaited()
    registry.close.assert_not_awaited()


def test_mode_demo_false_accepte_sans_signature():
    app, registry = harness(twilio_validate_signature=False)
    with TestClient(app) as client, client.websocket_connect("/ws/media"):
        pass
    registry.open.assert_not_awaited()


@pytest.mark.parametrize("settings", [{"twilio_auth_token": ""}, {"public_hostname": ""}])
def test_configuration_incomplete_refuse(settings):
    app, registry = harness(**settings)
    with TestClient(app) as client, pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/media", headers=signed()):
            pytest.fail("incomplete credentials accepted")
    registry.get.assert_not_called()
    registry.open.assert_not_awaited()
    registry.close.assert_not_awaited()


@pytest.mark.parametrize(
    "url", ["https://public.ngrok.example/ws/media", "wss://testserver/ws/media"]
)
def test_autre_protocole_ou_hote_reste_refuse_malgre_headers_proxy(url):
    app, registry = harness()
    headers = {
        **signed(url),
        "Host": "testserver",
        "X-Forwarded-Host": "testserver",
        "X-Forwarded-Proto": "wss",
    }
    with TestClient(app) as client, pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/media", headers=headers):
            pytest.fail("client controlled URL accepted")
    registry.get.assert_not_called()
    registry.close.assert_not_awaited()


def test_query_string_exacte_participe_a_la_signature():
    app, registry = harness()
    query = "?marker=test%20value"
    with TestClient(app) as client:
        with client.websocket_connect("/ws/media" + query, headers=signed(PUBLIC_URL + query)):
            pass
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/ws/media?marker=changed", headers=signed(PUBLIC_URL + query)
            ):
                pytest.fail("changed query accepted")
    registry.get.assert_not_called()


def test_refus_ne_journalise_ni_token_signature_ni_query(caplog):
    app, _ = harness()
    signature = "a-client-private-value"
    with TestClient(app) as client, pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/ws/media?private=private-query-value",
            headers={"X-Twilio-Signature": signature},
        ):
            pytest.fail("forged signature accepted")
    assert "signature Twilio invalide" in caplog.text
    assert signature not in caplog.text
    assert TOKEN not in caplog.text
    assert "private-query-value" not in caplog.text


def test_signature_valide_permet_audio_et_nettoyage_de_la_seule_session_attachee():
    app, registry = harness()
    session = SimpleNamespace(call_id="CA_signed", on_frame=AsyncMock(), t=lambda: 0.0)
    registry.get.return_value = session
    start = {
        "event": "start",
        "streamSid": "MZ_signed",
        "start": {
            "streamSid": "MZ_signed",
            "callSid": session.call_id,
            "tracks": ["inbound", "outbound"],
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
            "customParameters": {},
        },
    }
    with TestClient(app) as client, client.websocket_connect("/ws/media", headers=signed()) as ws:
        ws.send_text(json.dumps(start))
    session.on_frame.assert_awaited_once()
    registry.open.assert_not_awaited()
    registry.close.assert_awaited_once_with(session.call_id)
