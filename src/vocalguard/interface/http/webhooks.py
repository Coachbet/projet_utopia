"""Webhooks TwiML appelés par Twilio pendant l'appel.

Toutes les réponses sont du XML TwiML. Deux contraintes dictent la forme de ce
module : Twilio attend une réponse en quelques centaines de millisecondes (donc
aucun appel réseau sortant dans le chemin de la réponse, l'appel de la victime
part en tâche de fond), et Twilio rappelle ces URLs lui-même pour l'annonce et
la musique d'attente (donc le texte à dire est lu dans `AnnouncementStore`,
jamais passé dans l'URL).

Les formulaires sont lus via `request.form()` : Twilio poste en
`application/x-www-form-urlencoded`, que Starlette décode sans `python-multipart`.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request, Response

from vocalguard.infrastructure.telephony.twiml import (
    announce_twiml,
    empty_twiml,
    hold_music_twiml,
    inbound_call_twiml,
    join_conference_twiml,
)
from vocalguard.interface.lifecycle import release_call

log = logging.getLogger(__name__)

router = APIRouter(prefix="/twilio", tags=["twilio"])

XML = "application/xml"

_background: set[asyncio.Task] = set()
"""Les tâches de fond sont retenues ici : une `Task` sans référence peut être
ramassée par le GC avant de s'être exécutée, et l'appel de la victime ne partirait
jamais."""


def _xml(body: str) -> Response:
    return Response(content=body, media_type=XML)


@router.post("/voice/inbound")
async def voice_inbound(request: Request) -> Response:
    """Entrée de l'appel du scammeur : on branche l'écoute, puis la conférence.

    `<Start><Stream>` est unidirectionnel et compatible avec `<Dial><Conference>`,
    ce qui nous permet d'écouter les deux pistes tout en gardant la main sur
    chaque participant.
    """
    form = await request.form()
    call_sid = str(form.get("CallSid") or "")
    from_number = str(form.get("From") or "")

    settings = request.app.state.settings
    registry = request.app.state.registry
    controller = request.app.state.controller

    if not call_sid:
        log.warning("Webhook inbound sans CallSid — appel ignoré")
        return _xml(empty_twiml())

    # `open()` est idempotent et démarre les transcriptions : Twilio rejoue ce
    # webhook en cas de timeout, et deux sessions pour un appel dédoubleraient
    # transcript et score.
    await registry.open(
        call_sid,
        caller_number=from_number,
        victim_number=settings.victim_phone_number,
    )

    # L'appel de la victime part en tâche de fond : Twilio attend ce TwiML pour
    # placer l'appelant dans la conférence, et un aller-retour REST ici coûterait
    # une seconde d'attente côté scammeur — assez pour qu'il raccroche.
    task = asyncio.create_task(_dial_victim(controller, call_sid))
    _background.add(task)
    task.add_done_callback(_background.discard)

    return _xml(
        inbound_call_twiml(
            stream_url=settings.media_stream_url,
            conference_name=settings.conference_name_for(call_sid),
            conference_status_callback=f"{settings.public_base_url}/twilio/conference/status",
        )
    )


async def _dial_victim(controller, call_id: str) -> None:
    # `dial_victim` n'est pas dans le port `CallController` : seul l'adapter
    # Twilio sait passer un appel. Sans lui (démo sans téléphone), on ne fait rien.
    dial = getattr(controller, "dial_victim", None)
    if dial is None:
        log.info("Contrôleur sans téléphonie : jambe victime non appelée (%s)", call_id)
        return
    try:
        victim_call_sid = await dial(call_id)
        log.info("Jambe victime créée pour %s : %s", call_id, victim_call_sid)
    except Exception:
        log.exception("Impossible d'appeler la victime pour %s", call_id)


@router.api_route("/voice/join/{call_id}", methods=["GET", "POST"])
async def voice_join(call_id: str, request: Request) -> Response:
    """TwiML de la jambe sortante : la victime rejoint la conférence de l'appel."""
    settings = request.app.state.settings
    return _xml(
        join_conference_twiml(
            conference_name=settings.conference_name_for(call_id),
            conference_status_callback=f"{settings.public_base_url}/twilio/conference/status",
        )
    )


@router.api_route("/voice/announce/{call_id}", methods=["GET", "POST"])
async def voice_announce(call_id: str, request: Request) -> Response:
    """Message à dire à la victime seule, consommé par `announce_url`.

    Twilio rappelle cette URL une fois par annonce demandée : on dépile, pour que
    deux alertes successives ne rejouent pas la même phrase.
    """
    message = request.app.state.announcements.pop(call_id)
    if not message:
        log.warning("Annonce demandée pour %s mais aucune en attente", call_id)
        return _xml(empty_twiml())
    return _xml(announce_twiml(message))


@router.api_route("/voice/hold-music", methods=["GET", "POST"])
async def voice_hold_music() -> Response:
    """Ce qu'entend le scammeur mis en attente. Il doit croire à un aléa réseau."""
    return _xml(hold_music_twiml())


@router.post("/voice/status")
async def voice_status(request: Request) -> Response:
    """Fin d'appel : on libère tout l'état, sinon il fuit d'un appel au suivant.

    Ce webhook est le filet, pas le chemin principal : le `StatusCallback` de la
    jambe entrante est configuré sur le NUMÉRO dans la console Twilio, pas par
    notre code, donc il peut parfaitement ne jamais arriver. Le nettoyage fiable
    est celui de `ws/media.py`. Les deux appellent `release_call`, qui est
    idempotent — c'est voulu, ils arrivent souvent tous les deux.

    Comportement connu et ACCEPTÉ, ne pas « corriger » : la jambe sortante vers
    la victime poste aussi ici, avec SON PROPRE CallSid (cf. `status_callback`
    dans `TwilioCallController.dial_victim`). Ce SID n'est pas un `call_id`
    connu, donc la libération est un no-op inoffensif. Conséquence : si la
    VICTIME raccroche la première, la session reste ouverte jusqu'à ce que le
    scammeur raccroche aussi — il reste seul dans la conférence, son propre
    statut terminal arrive alors et libère l'appel. Nettoyer sur le SID de la
    victime demanderait de mémoriser la correspondance jambe→appel, pour
    fermer... un appel dont le scammeur parle peut-être encore.
    """
    form = await request.form()
    call_sid = str(form.get("CallSid") or "")
    status = str(form.get("CallStatus") or "")
    log.info("Statut d'appel %s : %s", call_sid, status)

    if call_sid and status in {"completed", "canceled", "failed", "busy", "no-answer"}:
        await release_call(request.app.state, call_sid)

    return _xml(empty_twiml())


@router.post("/conference/status")
async def conference_status(request: Request) -> Response:
    """Événements de conférence. Utile en débogage de démo : savoir qui a rejoint
    quand explique la plupart des « l'annonce n'est pas partie »."""
    form = await request.form()
    log.info(
        "Conférence %s : %s (call_sid=%s)",
        form.get("FriendlyName"),
        form.get("StatusCallbackEvent"),
        form.get("CallSid"),
    )
    return _xml(empty_twiml())
