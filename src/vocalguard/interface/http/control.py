"""Panneau de contrôle humain : les actions que l'opérateur déclenche à la main.

Le système sait déjà mettre le scammeur en attente tout seul (`domain.alerts.
plan_actions` au niveau `intervene`, exécuté par `application.intervention`).
Ce module ajoute les deux boutons que l'automatisme ne produira jamais :

  - `resume` : l'opérateur regarde le dashboard, conclut au faux positif, et veut
    rendre la parole à l'appelant ;
  - `hold` : le filet de sécurité du précédent. `AlertLevel.INTERVENE` est le
    niveau maximum et `InterventionRunner.handle_score` sort dès que
    `assessment.level <= session.acted_level` : après une reprise, plus aucun
    score — pas même 100 — ne peut remettre le scammeur en attente. Sans cette
    route, un clic de trop sur `resume` rendait la parole au scammeur
    DÉFINITIVEMENT.

Les deux sont des **surcharges humaines** : elles agissent directement sur
l'appel via le contrôleur Twilio et ne touchent pas `acted_level`, donc pas la
machine à états du score. Voir `_surcharge_humaine` pour le pourquoi détaillé.

Ce module n'est donc **pas** un webhook Twilio, contrairement à son voisin
`webhooks.py` : il répond en JSON à notre propre dashboard, pas en TwiML à
Twilio. D'où l'absence de `Response(media_type="application/xml")` ici, et des
codes HTTP exploitables par un `fetch()`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request

from vocalguard.domain.alerts import ActionKind
from vocalguard.domain.events import EventType

if TYPE_CHECKING:
    from vocalguard.application.sessions import CallSession

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
    return await _surcharge_humaine(request, call_id, held=False)


@router.post("/calls/{call_id}/hold")
async def hold_call(call_id: str, request: Request) -> dict[str, object]:
    """Remet l'appelant en attente : le retour arrière après un `resume` de trop.

    Symétrique exact de `resume_call`, même contrat de réponse et mêmes codes
    HTTP, parce que le dashboard câble les deux boutons sur la même fonction.

    C'est aussi la seule façon de re-protéger la victime après une reprise :
    `acted_level` reste à INTERVENE (volontairement, voir plus bas), donc aucun
    score ne reprendra jamais la main sur cet appel.
    """
    return await _surcharge_humaine(request, call_id, held=True)


async def _surcharge_humaine(request: Request, call_id: str, *, held: bool) -> dict[str, object]:
    """Applique l'état d'attente voulu par l'opérateur. `held=True` → mise en attente.

    Un seul corps pour les deux routes : leur contrat de réponse est consommé
    par le même code de dashboard, et deux implémentations parallèles auraient
    divergé au premier correctif (codes HTTP, forme de l'événement, ordre entre
    l'appel Twilio et la mise à jour de l'état).
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

    if session.caller_held == held:
        # L'état demandé est déjà atteint : c'est un succès, simplement sans
        # effet. On sort AVANT Twilio et AVANT le bus, parce qu'un `resume` sur
        # un appel jamais mis en attente émettait un `action: resume_caller,
        # ok: true` — la frise du dashboard affichait une reprise qui n'avait
        # rien repris, et l'opérateur qui double-clique voyait deux points pour
        # une seule décision. La forme de la réponse, elle, ne change pas : le
        # dashboard n'a pas à distinguer « fait » de « déjà fait ».
        return _etat(call_id, session)

    controller = request.app.state.controller
    action = ActionKind.HOLD_CALLER if held else ActionKind.RESUME_CALLER

    try:
        if held:
            ok = bool(await controller.hold_caller(call_id))
        else:
            ok = bool(await controller.resume_caller(call_id))
    except Exception:
        # `TwilioCallController.hold_caller`/`resume_caller` renvoient déjà
        # `False` au lieu de lever, mais une doublure de test ou une panne
        # réseau inattendue ne doit pas transformer un clic d'opérateur en 500.
        log.exception("action %s impossible sur %s", action.value, call_id)
        ok = False

    if ok:
        # Exactement ce que fait `InterventionRunner._execute` : l'état ne suit
        # l'action que si l'action a réussi, sinon le dashboard afficherait
        # « rendu à l'appelant » alors que le scammeur écoute encore la musique
        # d'attente.
        session.caller_held = held

    # `acted_level` n'est touché par AUCUNE des deux routes. Pour `resume` :
    # `handle_score` n'agit que si `assessment.level > session.acted_level`, donc
    # le prochain score à 90 ne remettra pas le scammeur en attente. C'est le
    # comportement voulu, la décision humaine l'emporte sur le score ; le
    # réinitialiser reprendrait l'appel pour le remettre en attente deux
    # secondes plus tard — une boucle absurde, et devant un jury ça
    # ressemblerait à un bug plutôt qu'à une garde. Le filet de sécurité est
    # `POST /control/calls/{call_id}/hold`, l'action explicite de l'opérateur.
    # Pour `hold` : symétriquement, ces deux routes sont des surcharges
    # humaines, elles agissent sur l'appel sans prétendre réécrire ce que le
    # score a déjà décidé.

    await session.emit(
        EventType.ACTION,
        {"action": action.value, "target": "caller", "ok": ok},
    )

    return _etat(call_id, session, ok=ok)


def _etat(call_id: str, session: CallSession, *, ok: bool = True) -> dict[str, object]:
    """La réponse des deux routes, au même endroit : Dev 2 code son bouton contre
    cette forme exacte, elle ne doit pas pouvoir diverger entre `hold` et
    `resume`."""
    return {"ok": ok, "call_id": call_id, "caller_held": session.caller_held}
