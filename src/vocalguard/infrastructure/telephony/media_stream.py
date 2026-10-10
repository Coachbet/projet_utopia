"""Lecture du protocole Twilio Media Streams (WebSocket).

Twilio pousse des messages JSON typés par un champ `event`. Deux pièges dictent
la forme de ce module :

- Twilio ajoute des types d'événements au fil du temps, et une trame inattendue
  ne doit jamais tuer la WebSocket au milieu d'un appel : `parse_frame` renvoie
  `None` plutôt que de lever.
- Les nombres arrivent en chaînes de caractères (`"chunk": "1"`), il faut les
  convertir ici pour que le reste du code manipule des entiers.

Les pistes `inbound` / `outbound` sont ce qui nous dit qui parle : l'appelant
arrive sur `inbound`, ce que l'appelant entend (donc la victime) sur `outbound`.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any

from vocalguard.domain.events import Speaker

TRACK_TO_SPEAKER: dict[str, Speaker] = {"inbound": "caller", "outbound": "victim"}


def speaker_for_track(track: str) -> Speaker | None:
    """`None` pour une piste inconnue : on préfère un locuteur absent à un faux."""
    return TRACK_TO_SPEAKER.get(track)


@dataclass(frozen=True, slots=True)
class MediaFormat:
    encoding: str
    sample_rate: int
    channels: int


@dataclass(frozen=True, slots=True)
class Connected:
    protocol: str
    version: str


@dataclass(frozen=True, slots=True)
class StreamStart:
    stream_sid: str
    call_sid: str
    tracks: tuple[str, ...]
    media_format: MediaFormat
    custom_parameters: dict[str, str]


@dataclass(frozen=True, slots=True)
class MediaChunk:
    stream_sid: str
    track: str
    speaker: Speaker | None
    chunk: int
    timestamp_ms: int
    ulaw: bytes
    """Charge utile base64 déjà décodée : octets μ-law bruts, prêts pour `mulaw`."""


@dataclass(frozen=True, slots=True)
class StreamStop:
    stream_sid: str
    call_sid: str


@dataclass(frozen=True, slots=True)
class Mark:
    stream_sid: str
    name: str


@dataclass(frozen=True, slots=True)
class Dtmf:
    stream_sid: str
    digit: str


Frame = Connected | StreamStart | MediaChunk | StreamStop | Mark | Dtmf


def _start(message: dict[str, Any]) -> StreamStart:
    start = message["start"]
    media_format = start["mediaFormat"]
    return StreamStart(
        # Le streamSid est répété dans `start` et à la racine ; on prend celui
        # du bloc `start`, qui est la source de vérité du message.
        stream_sid=str(start.get("streamSid") or message["streamSid"]),
        call_sid=str(start["callSid"]),
        tracks=tuple(str(t) for t in start["tracks"]),
        media_format=MediaFormat(
            encoding=str(media_format["encoding"]),
            sample_rate=int(media_format["sampleRate"]),
            channels=int(media_format["channels"]),
        ),
        custom_parameters={
            str(k): str(v) for k, v in (start.get("customParameters") or {}).items()
        },
    )


def _media(message: dict[str, Any]) -> MediaChunk:
    media = message["media"]
    track = str(media["track"])
    return MediaChunk(
        stream_sid=str(message["streamSid"]),
        track=track,
        speaker=speaker_for_track(track),
        chunk=int(media["chunk"]),
        timestamp_ms=int(media["timestamp"]),
        ulaw=base64.b64decode(media["payload"]),
    )


def parse_frame(raw: str | bytes) -> Frame | None:
    """Trame Twilio → objet métier, ou `None` si on ne sait pas la lire.

    Tout est attrapé volontairement : un `KeyError` sur un champ absent ou un
    base64 tronqué ne justifie pas de couper l'appel en cours.
    """
    try:
        message = json.loads(raw)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return None
    if not isinstance(message, dict):
        return None

    event = message.get("event")
    try:
        if event == "connected":
            return Connected(protocol=str(message["protocol"]), version=str(message["version"]))
        if event == "start":
            return _start(message)
        if event == "media":
            return _media(message)
        if event == "stop":
            return StreamStop(
                stream_sid=str(message["streamSid"]),
                call_sid=str(message["stop"]["callSid"]),
            )
        if event == "mark":
            return Mark(stream_sid=str(message["streamSid"]), name=str(message["mark"]["name"]))
        if event == "dtmf":
            return Dtmf(stream_sid=str(message["streamSid"]), digit=str(message["dtmf"]["digit"]))
    except (KeyError, TypeError, ValueError, AttributeError, binascii.Error):
        return None

    return None
