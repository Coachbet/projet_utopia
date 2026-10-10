"""Sortants PWA sans réseau : PIN, nonce, signature et historique complet."""

from __future__ import annotations

import json
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from vocalguard.config import Settings
from vocalguard.infrastructure.fakes.event_sink import InMemoryEventSink
from vocalguard.infrastructure.fakes.transcriber import FakeTranscriber
from vocalguard.interface.app import create_app

HOST = "outgoing.ngrok.example"
ACCOUNT = "AC" + "a" * 32
TOKEN = "outgoing-test-auth-private"
PIN = "654321"
NUMBER = "+33612345678"
CALL = "CA" + "1" * 32
CHILD = "CA" + "2" * 32
PATH = "/twilio/voice/outbound"


@pytest.fixture
def app_client(tmp_path):
    contacts = tmp_path / "contacts.json"
    contacts.write_text(json.dumps({"contacts": [{"name": "Lucas", "phone_number": NUMBER}]}))
    settings = Settings(
        _env_file=None,
        transcribe_backend="fake",
        twilio_validate_signature=False,
        public_hostname=HOST,
        twilio_account_sid=ACCOUNT,
        twilio_auth_token=TOKEN,
        twilio_api_key_sid="SK" + "b" * 32,
        twilio_api_key_secret="api-key-test-private",
        twilio_twiml_app_sid="AP" + "c" * 32,
        twilio_phone_number="+15550001111",
        voice_client_identity="protected",
        victim_phone_number="+33600000000",
        protected_phone_number="+33600000001",
        app_pin=PIN,
        contacts_path=contacts,
        history_path=tmp_path / "history.sqlite",
        history_enabled=True,
        voicemail_enabled=False,
        log_level="CRITICAL",
    )
    sink = InMemoryEventSink()
    factory_calls = []

    def transcriber():
        instance = FakeTranscriber()
        factory_calls.append(instance)
        return instance

    app = create_app(settings, sink=sink, transcriber_factory=transcriber)
    with TestClient(app) as client:
        yield client, app, factory_calls


def post_signed(client, path, body, *, token=TOKEN):
    signature = RequestValidator(token).compute_signature(f"https://{HOST}{path}", body)
    return client.post(path, data=body, headers={"X-Twilio-Signature": signature})


def authorize(client, *, number=NUMBER):
    response = client.post("/app/outgoing", json={"pin": PIN, "number": number})
    assert response.status_code == 200, response.text
    return response.json()


def outbound_body(authorization, **overrides):
    return {
        "AccountSid": ACCOUNT,
        "CallSid": CALL,
        "From": "client:protected",
        "Authorization": authorization,
        **overrides,
    }


def test_preparation_pin_n_appelle_personne_et_ne_divulgue_aucun_secret(app_client, caplog):
    client, app, factory = app_client
    assert client.post("/app/outgoing", json={"pin": "000000", "number": NUMBER}).status_code == 403
    prepared = authorize(client, number="06 12 34 56 78")
    assert prepared["number"] == NUMBER
    assert prepared["caller_name"] == "Lucas"
    assert prepared["direction"] == "outgoing" and prepared["analyzed"] is False
    assert prepared["expires_in"] == 60
    assert len(prepared["authorization"]) >= 40
    assert app.state.registry.active() == []
    assert all(not instance.streams for instance in factory)
    assert TOKEN not in caplog.text and prepared["authorization"] not in caplog.text


@pytest.mark.parametrize(
    "number",
    [
        "112",
        "client:intruder",
        "https://example.com",
        "+33612345678#",
        "06AB12345678",
        "+33+612345678",
        "+15550001111",
        "+33600000000",
        "+33600000001",
    ],
)
def test_destinations_invalides_et_numeros_service_refuses(app_client, number):
    client, app, factory = app_client
    response = client.post("/app/outgoing", json={"pin": PIN, "number": number})
    assert response.status_code == 422
    assert app.state.registry.active() == [] and factory == []


def test_webhook_exige_signature_meme_quand_demo_false_et_nonce_pin(app_client):
    client, app, factory = app_client
    prepared = authorize(client)
    body = outbound_body(prepared["authorization"])
    assert client.post(PATH, data=body).status_code == 403
    assert post_signed(client, PATH, body, token="wrong-auth").status_code == 403
    assert post_signed(client, PATH, outbound_body("invented-token")).status_code == 403
    assert app.state.registry.active() == [] and factory == []
    # Les refus n'ont pas consommé le nonce de la personne autorisée.
    assert post_signed(client, PATH, body).status_code == 200


def test_twilio_dial_uniquement_destination_reservee_sans_audio_ni_transcription(app_client):
    client, app, factory = app_client
    prepared = authorize(client)
    body = outbound_body(prepared["authorization"], To="+44999999999")
    response = post_signed(client, PATH, body)
    assert response.status_code == 200
    xml = ET.fromstring(response.text)
    dial = xml.find("Dial")
    assert dial.get("callerId") == "+15550001111"
    assert dial.get("answerOnBridge") == "true" and dial.get("record") == "do-not-record"
    assert dial.find("Number").text == NUMBER
    assert xml.find(".//Stream") is None and xml.find(".//Record") is None
    assert app.state.registry.get(CALL).transcribe is False
    assert all(not instance.streams for instance in factory)
    retry = post_signed(client, PATH, body)
    assert retry.status_code == 200 and retry.text == response.text
    assert post_signed(client, PATH, {**body, "CallSid": "CA_other"}).status_code == 403
    assert (
        post_signed(client, PATH, {**body, "Authorization": "nonce-different"}).status_code == 403
    )
    assert client.post("/app/outgoing", json={"pin": PIN, "number": NUMBER}).status_code == 409
    history = client.post("/app/history", json={"pin": PIN}).json()["items"]
    assert len(history) == 1
    assert history[0]["direction"] == "outgoing"
    assert history[0]["max_score"] is None


def test_mauvaise_identite_et_compte_ne_peuvent_utiliser_nonce(app_client):
    client, app, _ = app_client
    prepared = authorize(client)
    assert (
        post_signed(
            client, PATH, outbound_body(prepared["authorization"], From="client:intruder")
        ).status_code
        == 403
    )
    assert (
        post_signed(
            client, PATH, outbound_body(prepared["authorization"], AccountSid="AC_wrong")
        ).status_code
        == 403
    )
    assert app.state.registry.active() == []
    assert post_signed(client, PATH, outbound_body(prepared["authorization"])).status_code == 200


def test_ancienne_configuration_inbound_de_la_twiml_app_reste_compatible(app_client):
    client, app, factory = app_client
    prepared = authorize(client)
    response = post_signed(
        client, "/twilio/voice/inbound", outbound_body(prepared["authorization"])
    )
    assert response.status_code == 200
    assert ET.fromstring(response.text).find("Dial/Number").text == NUMBER
    assert app.state.registry.get(CALL).transcribe is False
    assert all(not instance.streams for instance in factory)


def test_callbacks_lies_parent_et_jambe_finalisent_historique_sans_session_residuelle(app_client):
    client, app, _ = app_client
    prepared = authorize(client)
    post_signed(client, PATH, outbound_body(prepared["authorization"]))
    callback = f"{PATH}/status/{CALL}"
    body = {"CallSid": CHILD, "ParentCallSid": CALL, "CallStatus": "in-progress"}
    assert client.post(callback, data=body).status_code == 403
    wrong = {**body, "ParentCallSid": "CA_intruder"}
    assert post_signed(client, callback, wrong).status_code == 200
    assert app.state.registry.get(CALL) is not None
    assert post_signed(client, callback, body).status_code == 200
    assert (
        post_signed(
            client, callback, {**body, "CallSid": "CA_wrong", "CallStatus": "completed"}
        ).status_code
        == 200
    )
    assert app.state.registry.get(CALL) is not None
    assert post_signed(client, callback, {**body, "CallStatus": "completed"}).status_code == 200
    assert app.state.registry.get(CALL) is None and not app.state.outgoing.busy
    history_response = client.post(f"/app/history/{CALL}", json={"pin": PIN})
    assert history_response.status_code == 200, history_response.text
    record = history_response.json()
    assert record["answered"] is True and record["ended_at"] is not None
    assert record["direction"] == "outgoing"
    assert record["max_score"] is None
    assert post_signed(client, callback, {**body, "CallStatus": "completed"}).status_code == 200


def test_no_answer_marque_sortant_manque_et_done_prononce_francais(app_client):
    client, app, _ = app_client
    prepared = authorize(client)
    post_signed(client, PATH, outbound_body(prepared["authorization"]))
    done = f"{PATH}/done/{CALL}"
    response = post_signed(
        client, done, {"CallSid": CALL, "DialCallSid": CHILD, "DialCallStatus": "no-answer"}
    )
    assert response.status_code == 200
    xml = ET.fromstring(response.text)
    assert xml.find("Say").get("language") == "en-US"
    assert xml.find("Hangup") is not None
    assert app.state.registry.get(CALL) is None
    history_response = client.post(f"/app/history/{CALL}", json={"pin": PIN})
    assert history_response.status_code == 200, history_response.text
    record = history_response.json()
    assert record["answered"] is False


def test_echec_avant_creation_de_la_jambe_libere_reservation_sans_dialcallsid(app_client):
    client, app, _ = app_client
    prepared = authorize(client)
    post_signed(client, PATH, outbound_body(prepared["authorization"]))
    done = f"{PATH}/done/{CALL}"
    response = post_signed(client, done, {"CallSid": CALL, "DialCallStatus": "failed"})
    assert response.status_code == 200
    assert app.state.registry.get(CALL) is None and not app.state.outgoing.busy
    record = client.post(f"/app/history/{CALL}", json={"pin": PIN}).json()
    assert record["answered"] is False and record["ended_at"] is not None
    assert authorize(client)["authorization"] != prepared["authorization"]
