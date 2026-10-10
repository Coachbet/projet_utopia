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
from vocalguard.infrastructure.telephony.client import TwilioCallController
from vocalguard.interface.app import create_app
from vocalguard.interface.lifecycle import release_call

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


# --- Libération de l'état d'appel ------------------------------------------
#
# L'état d'un appel vit à trois endroits qui ne se connaissent pas (session,
# pipeline de score, contrôleur Twilio) et deux déclencheurs indépendants
# annoncent la fin de l'appel. Les tests ci-dessous vérifient les trois purges et
# les deux chemins : c'est la fuite qui se verrait au deuxième appel de démo.


@pytest.fixture
def controller_twilio(harness):
    """Remplace la doublure de log par le vrai contrôleur, sans credentials.

    `LoggingCallController` ne tient aucun état par appel, donc il ne peut rien
    prouver sur la purge du cache de conférence. `TwilioCallController` sans
    client Twilio ne joint jamais le réseau mais porte bien ce cache, et partage
    la file d'annonces de l'app comme dans `create_app`.
    """
    _, app, _, _ = harness
    controller = TwilioCallController(app.state.settings, announcements=app.state.announcements)
    app.state.controller = controller
    return controller


def test_statut_terminal_purge_les_trois_etats(harness, controller_twilio):
    client, app, transcriber, _ = harness
    client.post(
        "/twilio/voice/inbound",
        data={"CallSid": CALL_ID, "From": "+33755555555", "To": "+33600000000"},
    )
    asyncio.run(_dire(transcriber))
    # Le SID de conférence est normalement mémorisé par le premier appel REST ;
    # ici on le pose à la main, aucun réseau n'est joignable dans ce test.
    controller_twilio._conference_sids[CALL_ID] = "CF_integration_01"
    app.state.announcements.queue(CALL_ID, "Annonce jamais prononcée")

    pipeline = app.state.pipeline
    assert CALL_ID in pipeline._reputation_done, "sans état avant, le test ne prouve rien"

    client.post("/twilio/voice/status", data={"CallSid": CALL_ID, "CallStatus": "completed"})

    assert app.state.registry.get(CALL_ID) is None
    # L'état interne du pipeline : c'est lui qui empêchait le second appel de
    # refaire sa vérification de réputation.
    assert CALL_ID not in pipeline._reputation_done
    assert CALL_ID not in pipeline._llm_in_flight
    assert CALL_ID not in pipeline._claimed_identity
    # Le contrôleur : un SID de conférence périmé ferait agir sur un appel mort,
    # et une annonce orpheline serait prononcée pendant l'appel suivant.
    assert controller_twilio._conference_sids == {}
    assert app.state.announcements.pending(CALL_ID) == ()


def test_fermeture_du_media_stream_libere_l_appel(harness):
    """Le chemin de nettoyage FIABLE : le `StatusCallback` de la jambe entrante
    est configuré sur le numéro dans la console Twilio et peut ne jamais arriver,
    alors que la fermeture du stream accompagne toujours la fin de l'appel."""
    client, app, _, _ = harness
    client.post("/twilio/voice/inbound", data={"CallSid": CALL_ID, "From": "+1555", "To": "+1555"})

    with client.websocket_connect("/ws/media") as ws:
        ws.send_text(_start_frame())
        ws.send_text(_media_frame("inbound", 0))
        assert app.state.registry.get(CALL_ID) is not None

    assert app.state.registry.active() == [], "le registre grossissait à chaque appel"
    assert client.get("/healthz").json()["active_calls"] == []


def test_le_second_appel_refait_sa_verification_de_reputation(harness):
    """La régression qui compterait devant le jury : rejouer la démo.

    Le pipeline ne vérifie la réputation qu'une fois par `call_id`. Sans purge,
    le deuxième appel n'émettait plus de `reputation` — donc plus de « numéro
    inconnu » ni de mismatch d'identité, le signal le plus parlant du pitch.
    """
    client, _, transcriber, sink = harness
    comptes = []

    for _ in range(2):
        sink.clear()
        client.post(
            "/twilio/voice/inbound",
            data={"CallSid": CALL_ID, "From": "+33755555555", "To": "+33600000000"},
        )
        asyncio.run(_dire(transcriber))
        client.post("/twilio/voice/status", data={"CallSid": CALL_ID, "CallStatus": "completed"})
        comptes.append(len(sink.by_type(EventType.REPUTATION)))

    # Pas de compte en dur : ce qui doit tenir est que le second appel produise
    # le même signal que le premier, quel que soit le nombre de vérifications que
    # le chemin LLM ajoute.
    assert comptes[1] >= 1, "le second appel n'émettait plus aucune réputation"
    assert comptes[1] == comptes[0]


def test_un_statut_non_terminal_ne_libere_rien(harness, controller_twilio):
    """`in-progress` arrive au décrochage : libérer là couperait l'appel vivant."""
    client, app, transcriber, _ = harness
    client.post(
        "/twilio/voice/inbound",
        data={"CallSid": CALL_ID, "From": "+33755555555", "To": "+33600000000"},
    )
    asyncio.run(_dire(transcriber))
    controller_twilio._conference_sids[CALL_ID] = "CF_integration_01"

    client.post("/twilio/voice/status", data={"CallSid": CALL_ID, "CallStatus": "in-progress"})

    assert app.state.registry.get(CALL_ID) is not None
    assert CALL_ID in app.state.pipeline._reputation_done
    assert controller_twilio._conference_sids == {CALL_ID: "CF_integration_01"}


def test_liberer_un_appel_inconnu_ne_leve_pas(harness):
    """Deux raisons d'arriver ici avec un `call_id` inconnu, les deux normales :
    les deux déclencheurs de libération se chevauchent (donc le second est un
    no-op), et la jambe sortante vers la victime poste sur `/twilio/voice/status`
    avec SON PROPRE CallSid, qui n'est pas un `call_id` de chez nous."""
    client, app, _, _ = harness
    client.post("/twilio/voice/inbound", data={"CallSid": CALL_ID, "From": "+1555", "To": "+1555"})

    # Le statut terminal de la jambe victime : l'appel du scammeur doit survivre.
    client.post(
        "/twilio/voice/status",
        data={"CallSid": "CA_jambe_victime", "CallStatus": "completed"},
    )
    assert app.state.registry.get(CALL_ID) is not None

    asyncio.run(release_call(app.state, "CA_jamais_vu"))
    asyncio.run(release_call(app.state, ""))

    # Et deux libérations du même appel : la seconde est silencieuse.
    asyncio.run(release_call(app.state, CALL_ID))
    asyncio.run(release_call(app.state, CALL_ID))
    assert app.state.registry.active() == []
