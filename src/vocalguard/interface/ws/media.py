"""WebSocket des Media Streams Twilio : l'audio de l'appel arrive ici.

Twilio ouvre cette connexion depuis Internet (wss:// via ngrok) et pousse ~50
trames de 20 ms par seconde et par piste. Deux conséquences sur ce code : on ne
fait jamais d'appel bloquant dans la boucle, et une trame inattendue est ignorée
plutôt que fatale — perdre 20 ms d'audio est sans conséquence, perdre la
connexion coûte le transcript de tout l'appel.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from vocalguard.infrastructure.telephony.media_stream import StreamStart, parse_frame

log = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws/media")
async def media_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    registry = websocket.app.state.registry
    session = None

    try:
        while True:
            message = await websocket.receive()
            raw = message.get("text") or message.get("bytes")
            if raw is None:
                if message.get("type") == "websocket.disconnect":
                    break
                continue

            frame = parse_frame(raw)
            if frame is None:
                continue

            if isinstance(frame, StreamStart):
                session = await _attach(registry, frame)
                if session is None:
                    continue

            if session is not None:
                await session.on_frame(frame)

    except WebSocketDisconnect:
        log.info("Media stream fermé par Twilio (%s)", getattr(session, "call_id", "?"))
    except Exception:
        log.exception("Erreur dans le media stream")
    finally:
        if session is not None:
            await session.aclose()


async def _attach(registry, frame: StreamStart):
    """Relie le stream à la session d'appel ouverte par le webhook.

    Le `call_id` passé en `<Parameter>` fait foi ; on retombe sur le `callSid` de
    la trame `start` si le paramètre manque. Si le webhook n'est pas passé (ordre
    inversé, ou test manuel), on ouvre la session ici : mieux vaut un transcript
    sans contexte d'appel que pas de transcript.
    """
    call_id = frame.custom_parameters.get("call_id") or frame.call_sid
    if not call_id:
        log.warning("Trame start sans call_sid ni paramètre call_id")
        return None

    session = registry.get(call_id)
    if session is None:
        log.warning("Media stream pour un appel inconnu (%s) — ouverture tardive", call_id)
        session = await registry.open(call_id)

    log.info("Media stream %s rattaché à l'appel %s", frame.stream_sid, call_id)
    return session
