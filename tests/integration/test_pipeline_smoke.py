"""Bout en bout de la moitié Dev 1, sans téléphone, sans AWS, sans réseau.

Ce qu'on prouve ici est exactement le premier livrable de Dev 1 : un appel
produit un transcript, et un score produit les bonnes actions Twilio dans le bon
ordre. Les adapters externes sont remplacés par les doublures de
`infrastructure/fakes/`, donc ce test tourne sur n'importe quelle machine — c'est
le filet de sécurité avant de brancher un vrai numéro.
"""

from __future__ import annotations

import asyncio
import base64
import json
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient

from vocalguard.config import Settings
from vocalguard.domain.events import Event, EventType
from vocalguard.infrastructure.fakes.event_sink import InMemoryEventSink
from vocalguard.infrastructure.fakes.transcriber import FakeTranscriber
from vocalguard.interface.app import create_app

CALL_ID = "CA_integration_01"
STREAM_SID = "MZ_integration_01"
SILENCE_ULAW = b"\xff" * 160  # une trame Twilio = 20 ms de μ-law à 8 kHz


@pytest.fixture
def harness():
    """Une app complète câblée sur des doublures, et de quoi l'inspecter."""
    settings = Settings(
        _env_file=None,
        transcribe_backend="fake",
        public_hostname="demo.ngrok-free.app",
        victim_phone_number="+33600000000",
        trusted_contact_phone_number="+33611111111",
        log_level="WARNING",
    )
    transcriber = FakeTranscriber()
    sink = InMemoryEventSink()
    app = create_app(settings, sink=sink, transcriber_factory=lambda: transcriber)
    with TestClient(app) as client:
        yield client, app, transcriber, sink


def _start_frame() -> str:
    return json.dumps(
        {
            "event": "start",
            "sequenceNumber": "1",
            "streamSid": STREAM_SID,
            "start": {
                "streamSid": STREAM_SID,
                "accountSid": "ACxxx",
                "callSid": CALL_ID,
                "tracks": ["inbound", "outbound"],
                "customParameters": {},
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
            },
        }
    )


def _media_frame(track: str, chunk: int) -> str:
    return json.dumps(
        {
            "event": "media",
            "sequenceNumber": str(chunk + 1),
            "streamSid": STREAM_SID,
            "media": {
                "track": track,
                "chunk": str(chunk),
                "timestamp": str(chunk * 20),
                "payload": base64.b64encode(SILENCE_ULAW).decode(),
            },
        }
    )


def test_appel_entrant_branche_ecoute_et_conference(harness):
    client, app, _, _ = harness

    response = client.post(
        "/twilio/voice/inbound",
        data={"CallSid": CALL_ID, "From": "+15550001111", "To": "+15550002222"},
    )

    assert response.status_code == 200
    root = ElementTree.fromstring(response.text)

    stream = root.find("./Start/Stream")
    assert stream is not None, "sans <Stream>, aucun audio ne nous parvient"
    assert stream.get("track") == "both_tracks"
    assert stream.get("url", "").startswith("wss://"), "Twilio refuse le ws:// en clair"

    conference = root.find("./Dial/Conference")
    assert conference is not None, "la conférence est ce qui permet d'agir par participant"
    assert CALL_ID in (conference.text or ""), "une conférence par appel"

    assert app.state.registry.get(CALL_ID) is not None


def test_audio_mulaw_arrive_au_bon_locuteur(harness):
    client, _, transcriber, _ = harness
    client.post("/twilio/voice/inbound", data={"CallSid": CALL_ID, "From": "+1555", "To": "+1555"})

    with client.websocket_connect("/ws/media") as ws:
        ws.send_text(_start_frame())
        ws.send_text(_media_frame("inbound", 0))
        ws.send_text(_media_frame("inbound", 1))
        ws.send_text(_media_frame("outbound", 2))
        ws.send_text(json.dumps({"event": "mark", "streamSid": STREAM_SID, "mark": {"name": "x"}}))

    # 160 octets de μ-law donnent 320 octets de PCM16 : c'est ce que Transcribe attend.
    assert transcriber.bytes_received["caller"] == 2 * 2 * len(SILENCE_ULAW)
    assert transcriber.bytes_received["victim"] == 2 * len(SILENCE_ULAW)


def test_transcript_produit_des_evenements_conformes_au_contrat(harness):
    client, _, transcriber, sink = harness
    client.post("/twilio/voice/inbound", data={"CallSid": CALL_ID, "From": "+1555", "To": "+1555"})

    asyncio.run(_dire(transcriber))

    events = sink.by_type(EventType.TRANSCRIPT)
    assert [e.data["is_final"] for e in events] == [False, True]
    final = events[-1]
    assert final.call_id == CALL_ID
    assert final.speaker == "caller"
    assert final.data["text"].startswith("Mamie")
    assert final.t == pytest.approx(3.2)


async def _dire(transcriber: FakeTranscriber) -> None:
    await transcriber.say("caller", "Mamie c'est", t=2.1, is_final=False)
    await transcriber.say("caller", "Mamie c'est Lucas, j'ai besoin d'argent", t=3.2)


def test_un_score_d_intervention_met_en_attente_puis_annonce(harness):
    client, app, _, sink = harness
    client.post("/twilio/voice/inbound", data={"CallSid": CALL_ID, "From": "+1555", "To": "+1555"})

    asyncio.run(_scorer(app, score=90, level="intervene"))

    controller = app.state.controller
    methodes = [appel[0] for appel in controller.calls]
    # L'ordre est la garantie produit : le scammeur est en attente AVANT l'annonce,
    # pour qu'il ne puisse pas l'entendre même si announce_url fuite.
    assert methodes.index("hold_caller") < methodes.index("announce_to_victim")
    assert "send_sms" in methodes
    assert all(m != "hangup" for m in methodes), "jamais de raccrochage automatique"

    actions = sink.by_type(EventType.ACTION)
    assert {e.data["action"] for e in actions} >= {"hold_caller", "announce_victim", "sms_contact"}
    assert all(e.data["ok"] for e in actions)
    assert app.state.registry.get(CALL_ID).caller_held is True

    # Un second score d'intervention ne doit rien rejouer dans l'oreille de la victime.
    avant = len(controller.calls)
    asyncio.run(_scorer(app, score=92, level="intervene"))
    assert len(controller.calls) == avant


def test_un_score_de_vigilance_ne_declenche_rien_d_audible(harness):
    client, app, _, sink = harness
    client.post("/twilio/voice/inbound", data={"CallSid": CALL_ID, "From": "+1555", "To": "+1555"})

    asyncio.run(_scorer(app, score=45, level="watch"))

    assert app.state.controller.calls == []
    assert sink.by_type(EventType.ACTION) == []


async def _scorer(app, *, score: int, level: str) -> None:
    await app.state.intervention.handle_event(
        Event(
            call_id=CALL_ID,
            t=12.4,
            type=EventType.SCORE,
            data={
                "score": score,
                "level": level,
                "reasons": ["urgence", "paiement inhabituel"],
                "breakers_fired": [],
            },
        )
    )


def test_annonce_servie_une_seule_fois(harness):
    client, app, _, _ = harness
    app.state.announcements.queue(CALL_ID, "Attention, cet appel présente des signes d'arnaque.")

    premiere = client.post(f"/twilio/voice/announce/{CALL_ID}")
    say = ElementTree.fromstring(premiere.text).find("./Say")
    assert say is not None and say.get("language") == "fr-FR"
    assert "arnaque" in (say.text or "")

    # Twilio peut rappeler l'URL : sans file vidée, la victime réentendrait la phrase.
    seconde = client.post(f"/twilio/voice/announce/{CALL_ID}")
    assert seconde.status_code == 200
    assert ElementTree.fromstring(seconde.text).find("./Say") is None


def test_musique_d_attente_en_boucle(harness):
    client, _, _, _ = harness
    play = ElementTree.fromstring(client.post("/twilio/voice/hold-music").text).find("./Play")
    assert play is not None
    assert play.get("loop") == "0", "une boucle finie rendrait l'attente suspecte"


def test_fin_d_appel_libere_la_session(harness):
    client, app, _, _ = harness
    client.post("/twilio/voice/inbound", data={"CallSid": CALL_ID, "From": "+1555", "To": "+1555"})
    assert client.get("/healthz").json()["active_calls"] == [CALL_ID]

    client.post("/twilio/voice/status", data={"CallSid": CALL_ID, "CallStatus": "completed"})

    assert app.state.registry.get(CALL_ID) is None
    assert client.get("/healthz").json()["active_calls"] == []
