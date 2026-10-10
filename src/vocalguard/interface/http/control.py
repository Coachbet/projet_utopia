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

Ces deux-là sont des **surcharges humaines** : elles agissent directement sur
l'appel via le contrôleur Twilio et ne touchent pas `acted_level`, donc pas la
machine à états du score. Voir `_surcharge_humaine` pour le pourquoi détaillé.

La troisième route est d'une autre nature et il ne faut pas les confondre :

  - `contact-denied` : l'opérateur rapporte un FAIT (« le vrai proche dit que ce
    n'est pas lui »), il ne commande pas une action. Le signal est injecté dans
    le pipeline, exactement comme l'aurait fait un SMS entrant, et c'est le score
    qui décide ensuite. Elle existe parce que le coupe-circuit
    `trusted_contact_denied` n'avait qu'un seul déclencheur — un SMS d'un mobile
    français vers un long code américain, dont la délivrabilité n'est pas
    garantie et dont l'échec est muet.

Ce module n'est donc **pas** un webhook Twilio, contrairement à son voisin
`webhooks.py` : il répond en JSON à notre propre dashboard, pas en TwiML à
Twilio. D'où l'absence de `Response(media_type="application/xml")` ici, et des
codes HTTP exploitables par un `fetch()`.
"""

from __future__ import annotations

import inspect
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


@router.post("/calls/{call_id}/contact-denied")
async def contact_denied(call_id: str, request: Request) -> dict[str, object]:
    """Le vrai proche dément être l'appelant, saisi depuis le dashboard.

    Pourquoi cette route existe alors que `/twilio/sms/inbound` fait déjà le
    travail : le coupe-circuit `trusted_contact_denied` n'a qu'UN déclencheur, et
    c'est un SMS d'un mobile français vers un long code américain. Si ce SMS
    n'arrive pas, le signal le plus décisif du système devient indémontrable —
    pas dégradé, indémontrable — et le symptôme est muet : le webhook ne part
    jamais, aucune erreur nulle part. Cette route rend le chemin indépendant du
    réseau, et un bouton reste déterministe devant un jury là où attendre un SMS
    sur scène est un pari.

    **Ce n'est pas une surcharge humaine** comme `hold` et `resume`, et c'est la
    différence qui compte : ces deux-là agissent sur l'appel et court-circuitent
    le score, alors que celle-ci injecte exactement le signal qu'un SMS aurait
    injecté, puis laisse l'automatisme décider. L'opérateur dit ce qu'il sait,
    pas ce qu'il veut qu'il arrive.

    D'où la forme de réponse — `ok` et `call_id`, sans `caller_held` : la mise en
    attente découle du score republié par le pipeline, donc elle n'a pas encore
    eu lieu quand on répond. Annoncer `caller_held` ici serait une valeur périmée
    à l'instant où le dashboard la lit.
    """
    session = request.app.state.registry.get(call_id)
    if session is None:
        # Même raisonnement que les deux autres routes : un appel absent du
        # registre est raccroché, pas en panne. Le bouton doit disparaître.
        raise HTTPException(
            status_code=404,
            detail=f"Appel {call_id} inconnu : déjà terminé ou jamais ouvert.",
        )

    pipeline = getattr(request.app.state, "pipeline", None)
    hook = getattr(pipeline, "contact_replied", None)
    if hook is None:
        # Même garde défensive que `/twilio/sms/inbound` : on nomme la
        # conséquence plutôt que de renvoyer un succès silencieux.
        log.warning(
            "Pipeline sans `contact_replied` : le démenti du proche est reçu "
            "mais ne pèse pas sur le score de %s",
            call_id,
        )
        return {"ok": False, "call_id": call_id}

    try:
        result = hook(call_id, denied=True)
        if inspect.isawaitable(result):
            await result
    except Exception:
        # Un clic d'opérateur ne devient jamais une 500. `contact_replied` est
        # idempotente côté pipeline, donc un second clic est sans danger.
        log.exception("démenti du proche impossible à appliquer sur %s", call_id)
        return {"ok": False, "call_id": call_id}

    return {"ok": True, "call_id": call_id}


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
