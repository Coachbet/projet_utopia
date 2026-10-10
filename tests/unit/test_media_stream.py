"""Parsing des trames Media Streams.

Deux choses à garantir : les nombres livrés en chaînes par Twilio deviennent des
entiers, et aucune trame, même absurde, ne fait lever `parse_frame` — une
exception ici couperait la WebSocket au milieu de l'appel.
"""

from __future__ import annotations

import base64
import json

import pytest

from vocalguard.infrastructure.telephony.media_stream import (
    Connected,
    Dtmf,
    Mark,
    MediaChunk,
    StreamStart,
    StreamStop,
    parse_frame,
    speaker_for_track,
)

ULAW = b"\xff\x7f\x00\x80" * 40  # 160 octets, une trame de 20 ms
PAYLOAD = base64.b64encode(ULAW).decode()


def test_connected():
    frame = parse_frame(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
    assert frame == Connected(protocol="Call", version="1.0.0")


def test_start():
    frame = parse_frame(
        json.dumps(
            {
                "event": "start",
                "sequenceNumber": "1",
                "streamSid": "MZ123",
                "start": {
                    "streamSid": "MZ123",
                    "accountSid": "AC123",
                    "callSid": "CA123",
                    "tracks": ["inbound", "outbound"],
                    "customParameters": {"call_id": "CA123"},
                    "mediaFormat": {
                        "encoding": "audio/x-mulaw",
                        "sampleRate": 8000,
                        "channels": 1,
                    },
                },
            }
        )
    )
    assert isinstance(frame, StreamStart)
    assert (frame.stream_sid, frame.call_sid) == ("MZ123", "CA123")
    assert frame.tracks == ("inbound", "outbound")
    assert frame.custom_parameters == {"call_id": "CA123"}
    assert frame.media_format.sample_rate == 8000
    assert frame.media_format.channels == 1


def test_start_sans_custom_parameters():
    # Twilio omet le champ quand aucun <Parameter> n'est déclaré dans le TwiML.
    frame = parse_frame(
        json.dumps(
            {
                "event": "start",
                "streamSid": "MZ1",
                "start": {
                    "callSid": "CA1",
                    "tracks": ["inbound"],
                    "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
                },
            }
        )
    )
    assert isinstance(frame, StreamStart)
    assert frame.custom_parameters == {}
    assert frame.stream_sid == "MZ1"  # repli sur le streamSid racine


@pytest.mark.parametrize(
    ("track", "speaker"),
    [("inbound", "caller"), ("outbound", "victim")],
)
def test_media_associe_la_piste_au_locuteur(track, speaker):
    frame = parse_frame(
        json.dumps(
            {
                "event": "media",
                "sequenceNumber": "2",
                "streamSid": "MZ123",
                "media": {
                    "track": track,
                    "chunk": "7",
                    "timestamp": "140",
                    "payload": PAYLOAD,
                },
            }
        )
    )
    assert isinstance(frame, MediaChunk)
    assert frame.speaker == speaker
    assert frame.ulaw == ULAW
    # Twilio envoie ces deux champs en chaînes : ils doivent sortir en entiers.
    assert (frame.chunk, frame.timestamp_ms) == (7, 140)


def test_media_piste_inconnue_na_pas_de_locuteur():
    frame = parse_frame(
        json.dumps(
            {
                "event": "media",
                "streamSid": "MZ1",
                "media": {"track": "mixed", "chunk": "1", "timestamp": "0", "payload": PAYLOAD},
            }
        )
    )
    assert isinstance(frame, MediaChunk)
    assert frame.speaker is None


def test_stop():
    frame = parse_frame(
        json.dumps(
            {
                "event": "stop",
                "sequenceNumber": "3",
                "streamSid": "MZ123",
                "stop": {"accountSid": "AC123", "callSid": "CA123"},
            }
        )
    )
    assert frame == StreamStop(stream_sid="MZ123", call_sid="CA123")


def test_mark_et_dtmf():
    mark = parse_frame(
        json.dumps({"event": "mark", "streamSid": "MZ1", "mark": {"name": "alerte"}})
    )
    assert mark == Mark(stream_sid="MZ1", name="alerte")

    dtmf = parse_frame(
        json.dumps(
            {"event": "dtmf", "streamSid": "MZ1", "dtmf": {"track": "inbound", "digit": "1"}}
        )
    )
    assert dtmf == Dtmf(stream_sid="MZ1", digit="1")


def test_accepte_les_octets_autant_que_le_texte():
    brut = json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}).encode()
    assert parse_frame(brut) == Connected(protocol="Call", version="1.0.0")


@pytest.mark.parametrize(
    "brut",
    [
        "pas du json",
        "",
        "[]",  # JSON valide mais pas un objet
        "null",
        json.dumps({"event": "futur_event_twilio", "data": {}}),  # type ajouté plus tard
        json.dumps({"event": "media"}),  # bloc media absent
        json.dumps({"event": "media", "streamSid": "MZ1", "media": {"track": "inbound"}}),
        json.dumps(
            {
                "event": "media",
                "streamSid": "MZ1",
                "media": {"track": "inbound", "chunk": "x", "timestamp": "0", "payload": PAYLOAD},
            }
        ),  # chunk non numérique
        json.dumps({"event": "start", "streamSid": "MZ1", "start": {"callSid": "CA1"}}),
        json.dumps({"event": "stop", "streamSid": "MZ1"}),
        json.dumps({"event": "mark", "streamSid": "MZ1", "mark": {}}),
    ],
)
def test_trame_illisible_renvoie_none_sans_lever(brut):
    assert parse_frame(brut) is None


def test_speaker_for_track():
    assert speaker_for_track("inbound") == "caller"
    assert speaker_for_track("outbound") == "victim"
    assert speaker_for_track("both_tracks") is None
