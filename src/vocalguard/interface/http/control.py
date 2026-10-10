"""Panneau de contrôle humain : les actions que l'opérateur déclenche à la main.

Le système sait déjà mettre le scammeur en attente tout seul (`domain.alerts.
plan_actions` au niveau `intervene`, exécuté par `application.intervention`).
Il manquait le chemin inverse, qu'aucun score ne produira jamais : l'opérateur
regarde le dashboard, conclut au faux positif, et veut rendre la parole à
l'appelant.

Ce module n'est donc **pas** un webhook Twilio, contrairement à son voisin
`webhooks.py` : il répond en JSON à notre propre dashboard, pas en TwiML à
Twilio. D'où l'absence de `Response(media_type="application/xml")` ici, et des
codes HTTP exploitables par un `fetch()`.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from vocalguard.domain.alerts import ActionKind
from vocalguard.domain.events import EventType

log = logging.getLogger(__name__)

router = APIRouter(prefix="/control", tags=["control"])


@router.post("/calls/{call_id}/resume")
async def resume_call(call_id: str, request: Request) -> dict[str, object]:
    """Rend la parole à l'appelant mis en attente : décision humaine, pas de score.

    Renvoie toujours du JSON, et ne distingue que deux situations par le code
    HTTP — l'appel existe (200) ou non (404). Un échec Twilio reste un 200 avec
    `ok: false` : le dashboard doit pouvoir afficher « Twilio a refusé » sans
    confondre ça avec « l'appel est terminé », et une 500 dans un panneau de
    démo ne dirait rien d'actionnable à l'opérateur.
    """
    session = request.app.state.registry.get(call_id)
    if session is None:
        # 404 et pas 200/ok:false : le bouton doit disparaître, pas clignoter en
        # rouge. Un appel absent du registre est un appel raccroché ou jamais
        # ouvert, jamais une panne de téléphonie.
        raise HTTPException(
            status_code=404,
            detail=f"Appel {call_id} inconnu : déjà terminé ou jamais ouvert.",
        )

    try:
        ok = bool(await request.app.state.controller.resume_caller(call_id))
    except Exception:
        # `TwilioCallController.resume_caller` renvoie déjà `False` au lieu de
        # lever, mais une doublure de test ou une panne réseau inattendue ne
        # doit pas transformer un clic d'opérateur en 500.
        log.exception("reprise d'appel impossible sur %s", call_id)
        ok = False

    if ok:
        # Exactement ce que fait `InterventionRunner._execute` pour
        # RESUME_CALLER : l'état ne suit l'action que si l'action a réussi,
        # sinon le dashboard afficherait « rendu à l'appelant » alors que le
        # scammeur écoute encore la musique d'attente.
        session.caller_held = False

    # `acted_level` reste volontairement à INTERVENE. `handle_score` n'agit que
    # si `assessment.level > session.acted_level` : le prochain score à 90 ne
    # remettra donc PAS le scammeur en attente. C'est le comportement voulu, la
    # décision humaine l'emporte sur le score. Le réinitialiser reprendrait
    # l'appel pour le remettre en attente deux secondes plus tard — une boucle
    # absurde, et devant un jury ça ressemblerait à un bug plutôt qu'à une
    # garde. Pour re-intervenir, il faudrait une action explicite de
    # l'opérateur, pas un rebond du score.

    await session.emit(
        EventType.ACTION,
        {"action": ActionKind.RESUME_CALLER.value, "target": "caller", "ok": ok},
    )

    return {"ok": ok, "call_id": call_id, "caller_held": session.caller_held}
