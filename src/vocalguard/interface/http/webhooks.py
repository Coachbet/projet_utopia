"""Webhooks TwiML appelés par Twilio pendant l'appel.

Toutes les réponses sont du XML TwiML. Deux contraintes dictent la forme de ce
module : Twilio attend une réponse en quelques centaines de millisecondes (donc
aucun appel réseau sortant dans le chemin de la réponse, l'appel de la victime
part en tâche de fond), et Twilio rappelle ces URLs lui-même pour l'annonce et
la musique d'attente (donc le texte à dire est lu dans `AnnouncementStore`,
jamais passé dans l'URL).

Les formulaires sont lus via `request.form()` : Twilio poste en
`application/x-www-form-urlencoded`, que Starlette décode sans `python-multipart`.

Une route de ce module ne sert pas de la voix : `/twilio/sms/inbound` reçoit la
RÉPONSE du proche au SMS de vérification (`domain.alerts.SMS_VERIFY_CONTACT`).
Elle vit ici parce que c'est le même émetteur, le même format de formulaire et la
même exigence de latence — pas parce que c'est du TwiML vocal.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from twilio.request_validator import RequestValidator

from vocalguard.infrastructure.contacts.book import normalize_number
from vocalguard.infrastructure.keywords.detector import normalize as fold_text
from vocalguard.infrastructure.telephony.twiml import (
    announce_twiml,
    empty_twiml,
    hold_music_twiml,
    inbound_call_twiml,
    join_conference_twiml,
)
from vocalguard.interface.lifecycle import release_call

log = logging.getLogger(__name__)

XML = "application/xml"

SIGNATURE_HEADER = "X-Twilio-Signature"


async def verify_twilio_signature(request: Request) -> None:
    """Authentifie l'appelant comme étant bien Twilio, si le réglage l'exige.

    Pourquoi c'est nécessaire : derrière un tunnel public, quiconque connaît
    l'URL peut poster sur `/twilio/voice/status` (ce qui libère une session en
    cours) ou forger un `From` sur `/twilio/sms/inbound` (la garde expéditeur de
    cette route est sa SEULE authentification). Twilio signe chaque requête avec
    le compte-token ; la signature est la seule preuve d'origine qu'on puisse
    vérifier hors ligne.

    Pourquoi c'est éteint par défaut : cf. `Settings.twilio_validate_signature`.
    Réglage faux = retour immédiat, zéro changement de comportement.

    L'URL validée est reconstruite depuis `settings.public_base_url` + le chemin,
    JAMAIS `request.url` : la signature de Twilio porte sur l'URL PUBLIQUE qu'il
    a appelée, alors que derrière ngrok le serveur ne voit qu'un
    `http://localhost:8000/...` (hôte réécrit, schéma perdu). C'est exactement à
    cet endroit que la validation échoue en pratique — d'où le log explicite.
    """
    settings = request.app.state.settings
    if not getattr(settings, "twilio_validate_signature", False):
        return

    token = settings.twilio_auth_token
    if not token:
        # Fermé plutôt que silencieux : avec un token vide, `RequestValidator`
        # signerait avec une clé vide, donc n'importe qui pourrait produire une
        # signature « valide ». Refuser est la seule lecture honnête d'un
        # « valide les signatures » demandé sans de quoi les valider. On ne lève
        # pas au démarrage pour autant : la démo doit pouvoir booter.
        log.error(
            "TWILIO_VALIDATE_SIGNATURE=true mais TWILIO_AUTH_TOKEN est vide : "
            "webhook %s refusé. Renseigner le token, ou remettre le réglage à false.",
            request.url.path,
        )
        raise HTTPException(status_code=403, detail="Signature Twilio non vérifiable")

    signature = request.headers.get(SIGNATURE_HEADER, "")

    # Le corps du formulaire participe à la signature : Twilio concatène l'URL
    # puis les champs triés par nom. `request.form()` est mis en cache par
    # Starlette, la route le relira sans relire le corps du socket.
    params: dict[str, str] = {}
    if request.method == "POST":
        form = await request.form()
        params = {key: str(value) for key, value in form.items()}

    url = f"{settings.public_base_url}{request.url.path}"
    if request.url.query:
        # Twilio signe l'URL complète, query string incluse.
        url = f"{url}?{request.url.query}"

    if RequestValidator(token).validate(url, params, signature):
        return

    log.warning(
        "Signature Twilio invalide sur %s : requête refusée (403). À vérifier, "
        "dans cet ordre : l'URL collée dans la console Twilio doit être "
        "exactement « %s » ; PUBLIC_HOSTNAME doit valoir l'hôte public (ngrok) ; "
        "sinon repasser TWILIO_VALIDATE_SIGNATURE à false — derrière un tunnel, "
        "l'échec vient presque toujours de l'URL, pas d'un attaquant. "
        "En-tête %s %s.",
        request.url.path,
        url,
        SIGNATURE_HEADER,
        "absent" if not signature else "présent mais non concordant",
    )
    raise HTTPException(status_code=403, detail="Signature Twilio invalide")


# Garde posée sur le router : elle couvre toutes les routes `/twilio/**` de ce
# module, présentes et futures, sans avoir à y penser route par route — oublier
# `/voice/status` suffirait à laisser un inconnu libérer un appel en cours.
router = APIRouter(
    prefix="/twilio",
    tags=["twilio"],
    dependencies=[Depends(verify_twilio_signature)],
)

_background: set[asyncio.Task] = set()
"""Les tâches de fond sont retenues ici : une `Task` sans référence peut être
ramassée par le GC avant de s'être exécutée, et l'appel de la victime ne partirait
jamais."""


# --- Lexique des réponses au SMS de vérification ----------------------------
#
# Le proche répond d'un pouce, sous stress, depuis un clavier de téléphone :
# « NON !!! », « c est pas moi », « Nope ». Les deux listes sont donc comparées
# sur la forme repliée de `keywords.detector.normalize` (minuscules, accents
# retirés, apostrophes unifiées), ponctuation remplacée par des espaces.
#
# Deux listes fermées plutôt qu'une heuristique, et rien entre les deux : en cas
# de doute on NE FAIT RIEN. Rater un « non » coûte un signal — le score continue
# de monter tout seul et l'annonce part quand même. Inventer un « non » met en
# attente un vrai petit-fils qui appelait sa grand-mère, c'est-à-dire exactement
# le faux positif que tout le reste du système s'applique à éviter. L'asymétrie
# est assumée : on préfère être sourd que menteur.
#
# Le démenti est donc contraint deux fois (cf. `_reply_is_denial`) : il lui faut
# un marqueur négatif ET aucun marqueur affirmatif dans le même message. Chercher
# « non » n'importe où suffisait à lire un démenti dans « Non mais oui c'est
# moi », « mais non c'est bien moi mamie » ou « il n'y a aucun problème, non ? ».

_DENIAL_ANCHORED = (
    "non",
    "nan",
    "no",
    "jamais",
)
"""Négations ambiguës : ces mots sont aussi des PARTICULES DE DISCOURS en
français (« non mais oui c'est moi », « je n'ai jamais eu de souci », « ... non
? » en fin de question rassurante). Elles ne comptent donc qu'en TÊTE de
message, la seule position où elles répondent vraiment à la question posée."""

_DENIAL_ANYWHERE = (
    "nope",
    "negatif",
    "pas moi",
    "c est pas moi",
    "ce n est pas moi",
    "pas du tout",
    "surtout pas",
)
"""Démentis non ambigus : aucun ne s'emploie en remplissage de phrase, donc leur
position dans le message ne change pas leur sens. Ce sont eux qui rattrapent
« Non, c'est pas moi » quand la négation de tête est noyée."""

_AFFIRMATION_TERMS = (
    "oui",
    "ouais",
    "yes",
    "yep",
    "yeah",
    "affirmatif",
    "c est moi",
    "c est bien moi",
)
"""Reconnues pour deux raisons : les logguer comme telles — un « oui » n'est pas
un corps incompris, et savoir lequel des deux est arrivé change tout au débogage
d'une démo (« le proche a-t-il répondu ? » vs « a-t-on su le lire ? ») — et
surtout VETO sur le démenti : leur présence interdit de mettre l'appelant en
attente."""

_AFFIRMATION_ALONE = ("si",)
"""« Si » n'est un acquiescement que SEUL (« Si. », réponse à une question
négative). Ailleurs c'est une conjonction, et « Si tu veux je te rappelle »
n'est pas une confirmation d'identité : hors message d'un seul mot, ce corps
reste incompris plutôt que lu comme un « oui »."""

_NON_WORD = re.compile(r"[^a-z0-9]+")


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
            # Transporté jusqu'au `<Stream>` parce que la trame `start` ne porte
            # pas le `From` : c'est la seule façon pour `/ws/media` de connaître
            # le numéro appelant si ce webhook n'est pas passé le premier.
            caller_number=from_number,
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


@router.post("/sms/inbound")
async def sms_inbound(request: Request) -> Response:
    """Réponse du proche au SMS « Est-ce toi qui appelles en ce moment ? ».

    URL à coller dans la console Twilio, champ « A MESSAGE COMES IN » du numéro :
    `https://<PUBLIC_HOSTNAME>/twilio/sms/inbound` (POST). Sans ça, le SMS part
    mais la réponse n'arrive nulle part — et c'est l'information la plus décisive
    du système : le vrai Lucas qui dit « non » tranche ce qu'aucun score ne peut
    trancher seul.

    Ce webhook n'émet RIEN sur le bus. Il appelle
    `pipeline.contact_replied(call_id, denied=True)` et c'est le pipeline qui
    décide quel événement en sortir (`reputation` portant `contact_denied`) : le
    contrat d'événement reste figé et la route n'a pas à connaître le scoring.

    Twilio rejoue ce webhook si notre réponse tarde : `contact_replied` doit donc
    être idempotent côté pipeline. On ne dédoublonne pas ici — il faudrait
    mémoriser les `MessageSid` vus, soit un état par appel de plus à purger dans
    `lifecycle.release_call`, pour une garantie que le pipeline doit de toute
    façon porter (un même « non » peut aussi arriver deux fois en deux SMS).
    """
    form = await request.form()
    from_number = str(form.get("From") or "")
    body = str(form.get("Body") or "")
    message_sid = str(form.get("MessageSid") or "")

    state = request.app.state
    settings = state.settings

    # 1. Garde-fou expéditeur. Notre numéro Twilio est public : sans cette
    # vérification, n'importe qui pourrait injecter « le proche a dit non » par
    # SMS et faire mettre en attente un appel parfaitement légitime. C'est la
    # seule authentification dont dispose cette route.
    #
    # `normalize_number` et pas `==` : le `.env` est saisi à la main
    # ("06 12 34 56 78") quand Twilio renvoie de l'E.164 ("+33612345678") — une
    # comparaison brute raterait le bon numéro et jetterait la vraie réponse.
    #
    # Contact de confiance non configuré : on refuse tout. Normalisés, deux
    # numéros vides seraient égaux, et un `From` absent passerait pour le proche.
    trusted = normalize_number(settings.trusted_contact_phone_number)
    if not trusted or normalize_number(from_number) != trusted:
        log.warning(
            "SMS entrant de %s ignoré : seul le contact de confiance peut répondre (sid=%s)",
            from_number or "(numéro absent)",
            message_sid,
        )
        return _xml(empty_twiml())

    # 2. Interpréter le corps. Un affirmatif rassure et ne déclenche rien ; un
    # corps incompris — ou contradictoire — ne déclenche rien non plus
    # (cf. `_reply_is_denial`).
    denial = _reply_is_denial(body)
    if denial is None:
        log.warning(
            "Réponse du proche incomprise, rien déclenché : « %s » (sid=%s)", body, message_sid
        )
        return _xml(empty_twiml())
    if denial is False:
        log.info("Le proche confirme être l'appelant : « %s » (sid=%s)", body, message_sid)
        return _xml(empty_twiml())

    # 3. Retrouver l'appel concerné. Le SMS ne porte aucun `call_id` : Twilio ne
    # transporte que l'expéditeur, le destinataire et le texte. On se rabat donc
    # sur « quel appel est en cours ? ».
    sessions = state.registry.active()
    if not sessions:
        # Cas réel et fréquent, pas une anomalie : le proche répond 30 s trop
        # tard, l'appel est déjà raccroché. Rien à faire peser, mais la trace
        # compte — c'est elle qui distingue « il n'a pas répondu » de « il a
        # répondu après ».
        log.warning(
            "Réponse du proche reçue hors appel (« %s », sid=%s) : plus aucun appel actif",
            body,
            message_sid,
        )
        return _xml(empty_twiml())
    if len(sessions) > 1:
        # Limite assumée de la démo, pas un oubli : accuser le mauvais appel est
        # pire que rater le signal. Désambiguïser demanderait de mémoriser à quel
        # appel chaque SMS de vérification a été envoyé — faisable, mais c'est un
        # état par appel de plus, et deux appels simultanés n'arrivent pas en
        # démo (un seul numéro Twilio, un seul téléphone sur la table).
        log.warning(
            "Réponse du proche ambiguë : %d appels actifs, aucun ne peut être accusé (sid=%s)",
            len(sessions),
            message_sid,
        )
        return _xml(empty_twiml())

    # 4. Un seul appel actif : c'est forcément celui qui a déclenché le SMS.
    call_id = sessions[0].call_id
    log.info("Le proche nie être l'appelant de %s : « %s » (sid=%s)", call_id, body, message_sid)
    await _notify_contact_denied(getattr(state, "pipeline", None), call_id)

    # 5. TwiML vide, jamais de SMS de confirmation : il coûterait un message par
    # réponse, et un auto-répondeur qui réagit à ses propres envois est la boucle
    # classique du SMS (Twilio reposterait ici notre propre texte).
    return _xml(empty_twiml())


def _reply_is_denial(body: str) -> bool | None:
    """`True` si le proche nie être l'appelant, `False` s'il le confirme, `None` si
    on n'a pas su lire.

    Un démenti n'est retenu QUE si le message porte un marqueur négatif et AUCUN
    marqueur affirmatif. C'est l'application littérale du principe annoncé plus
    haut (« en cas de doute, ne rien faire ») : un seul `True` de trop met en
    attente un vrai petit-fils, alors qu'un `True` manqué ne coûte qu'un signal
    sur un score qui monte déjà seul.

    Les deux familles dans le même message (« non mais oui c'est moi ») rendent
    donc la réponse INCOMPRISE, pas confirmée : on ne sait pas laquelle porte le
    sens, et prétendre dans le journal que « le proche confirme » serait aussi
    faux que de l'accuser.
    """
    folded = _fold_words(body)
    if not folded.strip():
        return None

    affirmed = _has_affirmation(folded)
    denied = _has_denial(folded)

    if denied and affirmed:
        return None
    if denied:
        return True
    if affirmed:
        return False
    return None


def _has_denial(folded: str) -> bool:
    """Marqueur négatif dans un corps déjà replié par `_fold_words`.

    Deux régimes, parce que les négations françaises n'ont pas toutes le même
    pouvoir : les particules ambiguës ne valent qu'en tête (`folded` est bordé
    d'espaces, donc `startswith` teste bien le PREMIER mot), les expressions non
    ambiguës valent partout.
    """
    return any(folded.startswith(f" {term} ") for term in _DENIAL_ANCHORED) or any(
        f" {term} " in folded for term in _DENIAL_ANYWHERE
    )


def _has_affirmation(folded: str) -> bool:
    """Marqueur d'accord dans un corps déjà replié par `_fold_words`.

    `_AFFIRMATION_ALONE` est comparé au message ENTIER : ces mots-là ne veulent
    dire « oui » que s'ils constituent toute la réponse.
    """
    return any(f" {term} " in folded for term in _AFFIRMATION_TERMS) or (
        folded.strip() in _AFFIRMATION_ALONE
    )


def _fold_words(body: str) -> str:
    """Forme comparable d'un SMS : mots repliés, séparés par des espaces, bordés.

    La ponctuation devient un espace et le résultat est encadré d'espaces, pour
    que `" no "` ne se trouve pas dans « nous », ni `" si "` dans « assistance ».
    Comparer des mots entourés d'espaces donne cette frontière sans regex par
    terme, et laisse les expressions (« c est pas moi ») se comparer pareil.
    """
    return f" {_NON_WORD.sub(' ', fold_text(body)).strip()} "


async def _notify_contact_denied(pipeline: Any, call_id: str) -> None:
    """Pousse le démenti du proche vers le pipeline de score, s'il sait l'entendre.

    `getattr` plutôt qu'un appel direct, exactement comme `lifecycle._forget` :
    `contact_replied` n'est pas dans les ports, et le pipeline peut être une
    doublure (rejeu, tests) qui ne l'implémente pas. Absent, on le dit au lieu de
    lever — une 500 ferait rejouer le webhook par Twilio sans plus de succès.

    On teste le RETOUR avec `inspect.isawaitable` au lieu de la fonction avec
    `asyncio.iscoroutinefunction` : le hook peut être une méthode synchrone, une
    coroutine, ou un objet appelable / un `functools.partial` que
    `iscoroutinefunction` déclare synchrone à tort. Le retour, lui, ne ment pas.
    """
    hook = getattr(pipeline, "contact_replied", None)
    if hook is None:
        log.warning(
            "Pipeline sans `contact_replied` : la réponse du proche est reçue mais "
            "ne pèse pas sur le score de %s",
            call_id,
        )
        return
    try:
        result = hook(call_id, denied=True)
        if inspect.isawaitable(result):
            await result
    except Exception:
        # Même raison que `_dial_victim` : l'erreur est à nous, la 500 serait pour
        # Twilio, qui rejouerait le SMS sans rien résoudre.
        log.exception("`contact_replied` en échec pour %s", call_id)


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
