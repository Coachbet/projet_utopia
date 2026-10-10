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
from vocalguard.interface.lifecycle import release_call

log = logging.getLogger(__name__)

router = APIRouter()

_CALLER_NUMBER_PARAMETERS = ("caller_number", "from")
"""Noms de `<Parameter>` acceptés pour le numéro de l'appelant, par ordre de
préférence. Deux et pas un : `caller_number` est le nom de chez nous (celui du
champ de `CallSession`), `from` celui du champ Twilio qui le porte côté webhook.
Aucun des deux n'est envoyé aujourd'hui par `inbound_call_twiml` — la liste est
là pour que l'ajout du `<Parameter>` côté TwiML suffise, sans toucher ici."""


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
            # C'est LE chemin de nettoyage fiable : la fermeture du stream arrive
            # toujours quand l'appel se termine, alors que le `StatusCallback` de
            # la jambe entrante est configuré sur le numéro dans la console
            # Twilio et peut ne jamais nous être posté.
            # `release_call` passe par `registry.close()`, qui appelle déjà
            # `session.aclose()` : le faire aussi ici serait redondant (inoffensif,
            # `aclose` est idempotent, mais trompeur à relire).
            await release_call(websocket.app.state, session.call_id)


async def _attach(registry, frame: StreamStart):
    """Relie le stream à la session d'appel ouverte par le webhook.

    Le `call_id` passé en `<Parameter>` fait foi ; on retombe sur le `callSid` de
    la trame `start` si le paramètre manque. Si le webhook n'est pas passé (ordre
    inversé, ou test manuel), on ouvre la session ici : mieux vaut un transcript
    sans contexte d'appel que pas de transcript.

    Une session déjà ouverte n'est JAMAIS retouchée : c'est le webhook entrant qui
    détient le `From`, et l'écraser avec ce que porte (ou ne porte pas) la trame
    `start` casserait le chemin normal pour réparer le chemin dégradé.
    """
    call_id = frame.custom_parameters.get("call_id") or frame.call_sid
    if not call_id:
        log.warning("Trame start sans call_sid ni paramètre call_id")
        return None

    session = registry.get(call_id)
    if session is None:
        log.warning("Media stream pour un appel inconnu (%s) — ouverture tardive", call_id)
        caller_number = _caller_number(frame)
        if not caller_number:
            # Pas un détail de log : sans numéro, `ScoringPipeline._check_reputation`
            # sort immédiatement, donc le coupe-circuit
            # `payment_request_with_identity_mismatch` ne peut structurellement pas
            # s'armer et la démo plafonne à l'alerte. Le dire ici, à l'endroit où la
            # cause est connue, plutôt que de laisser le pipeline s'en étonner plus tard.
            log.warning(
                "Appel %s ouvert sans numéro appelant (le webhook entrant, qui porte "
                "le `From`, n'est pas passé et la trame start ne porte aucun des "
                "paramètres %s) : le signal réputation restera inerte et le "
                "coupe-circuit paiement + identité ne pourra pas s'armer",
                call_id,
                "/".join(_CALLER_NUMBER_PARAMETERS),
            )
        session = await registry.open(call_id, caller_number=caller_number)

    log.info("Media stream %s rattaché à l'appel %s", frame.stream_sid, call_id)
    return session


def _caller_number(frame: StreamStart) -> str:
    """Numéro de l'appelant tel que la trame `start` le porte, `""` si absent.

    Seule source possible sur ce chemin : la trame `start` ne contient aucun
    champ de numéro, uniquement les `<Parameter>` déclarés dans le `<Stream>`.
    """
    for name in _CALLER_NUMBER_PARAMETERS:
        number = frame.custom_parameters.get(name, "").strip()
        if number:
            return number
    return ""
