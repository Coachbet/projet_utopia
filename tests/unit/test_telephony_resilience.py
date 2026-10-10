"""Régressions de routage : app fermée, repli, callbacks rejoués et retardés."""

from __future__ import annotations

import asyncio
import io
import threading
import wave
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from vocalguard.config import Settings
from vocalguard.infrastructure.fakes.event_sink import InMemoryEventSink
from vocalguard.infrastructure.fakes.transcriber import FakeTranscriber
from vocalguard.infrastructure.telephony.client import TwilioCallController
from vocalguard.interface.app import create_app
from vocalguard.interface.http import webhooks

CALL = "CA_parent"
APP = "CA_app"
PHONE = "CA_phone"


def controller(*, fallback: str = "+33600000000") -> tuple[TwilioCallController, MagicMock]:
    settings = Settings(
        _env_file=None,
        public_hostname="demo.example",
        twilio_account_sid="AC_test",
        twilio_auth_token="test",
        twilio_phone_number="+15550000000",
        voice_client_identity="protected",
        victim_phone_number=fallback,
    )
    client = MagicMock()
    client.calls.create.side_effect = [SimpleNamespace(sid=APP), SimpleNamespace(sid=PHONE)]
    return TwilioCallController(settings, client=client), client


async def test_app_sonne_25_secondes_et_callbacks_sont_correles() -> None:
    ctrl, client = controller()
    assert await ctrl.dial_victim(CALL) == APP
    kwargs = client.calls.create.call_args.kwargs
    assert kwargs["to"] == "client:protected"
    assert kwargs["timeout"] == 25
    assert kwargs["status_callback_event"] == ["initiated", "ringing", "answered", "completed"]
    assert parse_qs(urlparse(kwargs["status_callback"]).query) == {
        "call_id": [CALL],
        "leg": ["app"],
    }
    assert ctrl.call_id_for_leg(APP) == CALL
    assert ctrl.call_id_for_leg("CA_unknown") is None


async def test_un_webhook_entrant_rejoue_ne_cree_pas_deux_jambes() -> None:
    ctrl, client = controller()
    await asyncio.gather(ctrl.dial_victim(CALL), ctrl.dial_victim(CALL))
    assert client.calls.create.call_count == 1
    assert await ctrl.dial_victim(CALL) == APP


@pytest.mark.parametrize("failure", ["busy", "failed", "no-answer"])
async def test_echec_app_declenche_un_seul_repli_et_ignore_ses_callbacks_tardifs(failure) -> None:
    ctrl, client = controller()
    await ctrl.dial_victim(CALL)
    outcomes = await asyncio.gather(
        ctrl.victim_status(CALL, APP, leg="app", status=failure),
        ctrl.victim_status(CALL, APP, leg="app", status=failure),
    )
    assert "fallback" in outcomes
    assert client.calls.create.call_count == 2
    assert client.calls.create.call_args.kwargs["to"] == "+33600000000"
    assert parse_qs(urlparse(client.calls.create.call_args.kwargs["status_callback"]).query)[
        "leg"
    ] == ["phone"]
    assert await ctrl.victim_status(CALL, APP, leg="app", status="completed") == "ignored"
    assert await ctrl.victim_status(CALL, PHONE, leg="phone", status="in-progress") == "answered"
    assert await ctrl.victim_status(CALL, PHONE, leg="phone", status="in-progress") == "ignored"
    assert ctrl.call_id_for_leg(PHONE) == CALL


async def test_sans_repli_informer_l_appelant_et_finir_sa_jambe() -> None:
    ctrl, client = controller(fallback="")
    await ctrl.dial_victim(CALL)
    assert await ctrl.victim_status(CALL, APP, leg="app", status="no-answer") == "unavailable"
    client.calls.assert_called_once_with(CALL)
    response = ET.fromstring(client.calls.return_value.update.call_args.kwargs["twiml"])
    assert [child.tag for child in response] == ["Say", "Hangup"]
    assert response.find("Say").text == "The person is not available."
    assert response.find("Say").get("language") == "en-US"


async def test_repli_injoignable_ne_redial_jamais_en_boucle() -> None:
    ctrl, client = controller()
    await ctrl.dial_victim(CALL)
    await ctrl.victim_status(CALL, APP, leg="app", status="failed")
    assert await ctrl.victim_status(CALL, PHONE, leg="phone", status="busy") == "unavailable"
    assert client.calls.create.call_count == 2
    assert await ctrl.victim_status(CALL, PHONE, leg="phone", status="busy") == "ignored"
    assert client.calls.return_value.update.call_count == 1


async def test_erreur_rest_app_replie_aussi_et_retourne_le_sid_du_repli() -> None:
    ctrl, client = controller()
    client.calls.create.side_effect = [ConnectionError("unreachable"), SimpleNamespace(sid=PHONE)]
    assert await ctrl.dial_victim(CALL) == PHONE
    assert client.calls.create.call_count == 2
    assert ctrl.call_id_for_leg(PHONE) == CALL


async def test_callback_mauvaise_jambe_ou_mauvais_sid_ne_declenche_rien() -> None:
    ctrl, client = controller()
    await ctrl.dial_victim(CALL)
    assert await ctrl.victim_status(CALL, "CA_intruder", leg="app", status="failed") == "ignored"
    assert await ctrl.victim_status(CALL, APP, leg="phone", status="failed") == "ignored"
    assert client.calls.create.call_count == 1
    client.calls.return_value.update.assert_not_called()


async def test_callback_precoce_attend_le_vrai_sid_recu_par_rest() -> None:
    ctrl, client = controller()
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    allow_create = threading.Event()

    def delayed_create(**kwargs):
        loop.call_soon_threadsafe(started.set)
        assert allow_create.wait(timeout=2)
        return SimpleNamespace(sid=APP)

    client.calls.create.side_effect = delayed_create
    dialing = asyncio.create_task(ctrl.dial_victim(CALL))
    await started.wait()
    forged_status = asyncio.create_task(
        ctrl.victim_status(CALL, "CA_wrong", leg="app", status="no-answer")
    )
    actual_status = asyncio.create_task(
        ctrl.victim_status(CALL, APP, leg="app", status="in-progress")
    )
    await asyncio.sleep(0)
    assert not actual_status.done() and not forged_status.done()
    allow_create.set()
    assert await dialing == APP
    assert await forged_status == "ignored"
    assert await actual_status == "answered"
    assert client.calls.create.call_count == 1


async def test_create_en_vol_est_annule_si_l_appelant_a_raccroche() -> None:
    ctrl, client = controller()
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    allow_create = threading.Event()

    def delayed_create(**kwargs):
        loop.call_soon_threadsafe(started.set)
        assert allow_create.wait(timeout=2)
        return SimpleNamespace(sid=APP)

    client.calls.create.side_effect = delayed_create
    dialing = asyncio.create_task(ctrl.dial_victim(CALL))
    await started.wait()
    ctrl.forget(CALL)
    allow_create.set()
    assert await dialing is None
    assert ctrl.call_id_for_leg(APP) is None
    client.calls.assert_called_once_with(APP)
    client.calls.return_value.update.assert_called_once_with(status="canceled")


async def test_fin_appel_annule_la_sonnerie_deja_creee_une_seule_fois() -> None:
    ctrl, client = controller()
    await ctrl.dial_victim(CALL)
    await ctrl.cancel_pending_victim(CALL)
    await ctrl.cancel_pending_victim(CALL)
    client.calls.assert_called_once_with(APP)
    client.calls.return_value.update.assert_called_once_with(status="canceled")
    assert ctrl.call_id_for_leg(APP) is None
    assert await ctrl.victim_status(CALL, APP, leg="app", status="no-answer") == "ignored"


async def test_fin_appel_n_annule_jamais_une_jambe_deja_decrochee() -> None:
    ctrl, client = controller()
    await ctrl.dial_victim(CALL)
    await ctrl.victim_status(CALL, APP, leg="app", status="in-progress")
    await ctrl.cancel_pending_victim(CALL)
    client.calls.return_value.update.assert_not_called()


async def test_oubli_appel_efface_toutes_les_jambes_et_ignore_callback_tardif() -> None:
    ctrl, client = controller()
    await ctrl.dial_victim(CALL)
    ctrl.forget(CALL)
    assert ctrl.call_id_for_leg(APP) is None
    assert await ctrl.victim_status(CALL, APP, leg="app", status="no-answer") == "ignored"
    assert client.calls.create.call_count == 1


async def test_app_deja_decrochee_ne_replie_pas_sur_un_statut_desordonne() -> None:
    ctrl, client = controller()
    await ctrl.dial_victim(CALL)
    assert await ctrl.victim_status(CALL, APP, leg="app", status="in-progress") == "answered"
    assert await ctrl.victim_status(CALL, APP, leg="app", status="completed") == "completed"
    assert client.calls.create.call_count == 1


async def _drain_callbacks() -> None:
    while webhooks._background:
        await asyncio.gather(*tuple(webhooks._background))


def test_webhook_terminal_victime_libere_le_parent_et_pas_une_autre_session(tmp_path) -> None:
    ctrl, _ = controller(fallback="")
    settings = Settings(
        _env_file=None,
        transcribe_backend="fake",
        public_hostname="demo.example",
        contacts_path=tmp_path / "no_contacts.json",
        log_level="CRITICAL",
    )
    app = create_app(settings, sink=InMemoryEventSink(), transcriber_factory=FakeTranscriber)
    app.state.controller = ctrl
    with TestClient(app) as http:
        http.portal.call(app.state.registry.open, CALL)
        http.portal.call(app.state.registry.open, "CA_other")
        http.portal.call(ctrl.dial_victim, CALL)
        response = http.post(
            f"/twilio/voice/status?call_id={CALL}&leg=app",
            data={"CallSid": APP, "CallStatus": "no-answer"},
        )
        assert response.status_code == 200
        http.portal.call(_drain_callbacks)
        assert app.state.registry.get(CALL) is None
        assert app.state.registry.get("CA_other") is not None
        assert ctrl.call_id_for_leg(APP) is None


def test_retour_appel_sert_uniquement_tonalite_et_aucun_audio_personnel(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        transcribe_backend="fake",
        contacts_path=tmp_path / "contacts.json",
        public_hostname="demo.example",
        twilio_validate_signature=True,
        twilio_auth_token="test",
        log_level="CRITICAL",
    )
    app = create_app(settings, sink=InMemoryEventSink(), transcriber_factory=FakeTranscriber)
    with TestClient(app) as http:
        response = http.get("/twilio/voice/ringback.wav")
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/wav"
        with wave.open(io.BytesIO(response.content), "rb") as audio:
            assert audio.getframerate() == 8000
            assert audio.getnchannels() == 1
            assert audio.getnframes() == 40000
            frames = audio.readframes(40000)
            assert any(frames[:24000])
            assert not any(frames[24000:])
        # Le seul média public ne désactive pas la signature des contrôles.
        assert http.post("/twilio/voice/status", data={"CallSid": CALL}).status_code == 403
