"""Dépôt vocal explicite après échec, jamais d'enregistrement de conversation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from xml.etree import ElementTree as ET

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from vocalguard.config import Settings
from vocalguard.infrastructure.telephony.client import TwilioCallController
from vocalguard.infrastructure.telephony.twiml import voicemail_done_twiml, voicemail_twiml
from vocalguard.interface.http.webhooks import router

ACCOUNT = "AC" + "a" * 32
HOST = "voicemail.ngrok.example"
TOKEN = "voicemail-test-private-auth"
CALL = "CA" + "b" * 32
APP = "CA" + "c" * 32
RECORDING = "RE" + "d" * 32


def settings():
    return Settings(
        _env_file=None,
        public_hostname=HOST,
        twilio_account_sid=ACCOUNT,
        twilio_auth_token=TOKEN,
        twilio_phone_number="+15550001111",
        voice_client_identity="protected",
        victim_phone_number="",
        twilio_validate_signature=False,
    )


def mailbox(*, expected=True):
    return SimpleNamespace(
        enabled=True,
        expect=AsyncMock(return_value=expected),
        capture=AsyncMock(return_value=True),
    )


async def test_voicemail_persist_expectation_avant_transition_et_arrete_analyse():
    sdk = MagicMock()
    sdk.calls.create.return_value.sid = APP
    ctrl = TwilioCallController(settings(), client=sdk)
    service = mailbox()
    ctrl.configure_voicemail(service)
    ctrl.remember_caller(CALL, "+33612345678", has_stream=True)
    await ctrl.dial_victim(CALL)

    def verify_transition(**kwargs):
        service.expect.assert_awaited_once_with(CALL, caller_number="+33612345678")
        xml = ET.fromstring(kwargs["twiml"])
        assert [child.tag for child in xml] == ["Stop", "Say", "Record", "Hangup"]
        assert xml.find("Stop/Stream").get("name") == "vocalguard-analysis"
        assert xml.find("Say").get("language") == "en-US"
        assert "will be recorded" in xml.find("Say").text
        assert xml.find("Record").get("maxLength") == "120"
        assert xml.find("Record").get("transcribe") == "false"
        assert xml.find("Record").get("recordingStatusCallbackEvent") == "completed"
        assert xml.find("Start") is None

    sdk.calls.return_value.update.side_effect = verify_transition
    assert await ctrl.victim_status(CALL, APP, leg="app", status="no-answer") == "unavailable"
    sdk.calls.return_value.update.assert_called_once()


@pytest.mark.parametrize("expected", [False, RuntimeError("database unavailable")])
async def test_stockage_indisponible_ne_declenche_pas_record(expected):
    sdk = MagicMock()
    sdk.calls.create.return_value.sid = APP
    ctrl = TwilioCallController(settings(), client=sdk)
    service = mailbox(expected=expected if isinstance(expected, bool) else True)
    if isinstance(expected, Exception):
        service.expect.side_effect = expected
    ctrl.configure_voicemail(service)
    await ctrl.dial_victim(CALL)
    await ctrl.victim_status(CALL, APP, leg="app", status="failed")
    xml = ET.fromstring(sdk.calls.return_value.update.call_args.kwargs["twiml"])
    assert xml.find("Record") is None
    assert xml.find("Hangup") is not None


def test_tous_parametres_voicemail_limitent_enregistrement_au_message():
    xml = ET.fromstring(
        voicemail_twiml(done_url="https://public/done", status_url="https://public/status")
    )
    record = xml.find("Record")
    assert record.get("playBeep") == "true"
    assert record.get("trim") == "trim-silence"
    assert record.get("maxLength") == "120" and record.get("timeout") == "8"
    assert record.get("finishOnKey") == "#"
    assert record.get("transcribeCallback") is None
    assert record.get("action") == "https://public/done"
    done = ET.fromstring(voicemail_done_twiml())
    assert [child.tag for child in done] == ["Say", "Hangup"]
    assert done.find("Record") is None
    # Un appel de confiance ne dispose d'aucun Stream à arrêter.
    trusted = ET.fromstring(
        voicemail_twiml(
            done_url="https://public/done", status_url="https://public/status", stop_analysis=False
        )
    )
    assert trusted.find("Stop") is None


def app_with_mailbox():
    app = FastAPI()
    app.include_router(router)
    app.state.settings = settings()
    app.state.voicemail = mailbox()
    # Aucun registre : un callback peut arriver après fermeture de la session
    # ou redémarrage. Seule l'attente persistée autorise le service à capturer.
    return app


def signature(path, body, *, token=TOKEN):
    return {
        "X-Twilio-Signature": RequestValidator(token).compute_signature(
            f"https://{HOST}{path}", body
        )
    }


def recording_body(**overrides):
    return {
        "AccountSid": ACCOUNT,
        "CallSid": CALL,
        "RecordingSid": RECORDING,
        "RecordingStatus": "completed",
        "RecordingDuration": "42",
        "RecordingUrl": "http://malicious.example/internal-secret",
        **overrides,
    }


def test_callback_signe_durable_capture_uniquement_sid_et_duree_sans_url_arbitraire():
    app = app_with_mailbox()
    path = "/twilio/voicemail/status"
    body = recording_body()
    with TestClient(app) as client:
        assert client.post(path, data=body, headers=signature(path, body)).status_code == 200
    app.state.voicemail.capture.assert_awaited_once_with(
        call_id=CALL, recording_sid=RECORDING, duration_s=42
    )


@pytest.mark.parametrize("headers", [None, {"X-Twilio-Signature": "forged-private-value"}])
def test_callbacks_non_authentifies_refuses_meme_en_mode_demo(headers, caplog):
    app = app_with_mailbox()
    with TestClient(app) as client:
        assert (
            client.post(
                "/twilio/voicemail/status", data=recording_body(), headers=headers
            ).status_code
            == 403
        )
        assert client.post("/twilio/voicemail/done", headers=headers).status_code == 403
    app.state.voicemail.capture.assert_not_awaited()
    assert TOKEN not in caplog.text and "forged-private-value" not in caplog.text


@pytest.mark.parametrize(
    "overrides",
    [
        {"RecordingStatus": "in-progress"},
        {"RecordingDuration": "0"},
        {"RecordingDuration": "121"},
        {"RecordingDuration": "malformed"},
    ],
)
def test_aucune_capture_pour_callback_partiel_ou_duree_invalide(overrides):
    app = app_with_mailbox()
    body = recording_body(**overrides)
    path = "/twilio/voicemail/status"
    with TestClient(app) as client:
        assert client.post(path, data=body, headers=signature(path, body)).status_code == 200
    app.state.voicemail.capture.assert_not_awaited()


def test_compte_twilio_mismatch_refuse_et_done_signe_termine_le_depot():
    app = app_with_mailbox()
    path = "/twilio/voicemail/status"
    body = recording_body(AccountSid="AC_wrong")
    with TestClient(app) as client:
        assert client.post(path, data=body, headers=signature(path, body)).status_code == 403
        done = "/twilio/voicemail/done"
        response = client.post(done, data={}, headers=signature(done, {}))
        assert response.status_code == 200
        assert ET.fromstring(response.text).find("Hangup") is not None
    app.state.voicemail.capture.assert_not_awaited()
