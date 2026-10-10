"""Webhooks TwiML appelés par Twilio pendant l'appel.

Toutes les réponses sont du XML TwiML. Deux contraintes dictent la forme de ce
module : Twilio attend une réponse en quelques centaines de millisecondes (donc
aucun appel réseau sortant dans le chemin de la réponse, l'appel de la victime
part en tâche de fond), et Twilio rappelle ces URLs lui-même pour l'annonce et
la musique d'attente (donc le texte à dire est lu dans `AnnouncementStore`,
jamais passé dans l'URL).

Les formulaires sont lus via `request.form()` : Twilio poste en
`application/x-www-form-urlencoded`, que Starlette décode sans `python-multipart`.

`/twilio/voice/inbound` porte en plus la décision la plus visible du produit —
transmettre ou analyser. La ligne protégée étant renvoyée ici, c'est la seule
route que TOUS les appels traversent, y compris ceux de la famille : voir sa
docstring pour l'ordre des étapes, qui n'est pas négociable.

Une route de ce module ne sert pas de la voix : `/twilio/sms/inbound` reçoit la
RÉPONSE du proche au SMS de vérification (`domain.alerts.SMS_VERIFY_CONTACT`).
Elle vit ici parce que c'est le même émetteur, le même format de formulaire et la
même exigence de latence — pas parce que c'est du TwiML vocal.
"""

from __future__ import annotations

import asyncio
import inspect
import io
import logging
import math
import re
import struct
import wave
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from twilio.request_validator import RequestValidator

from vocalguard.domain.alerts import ReportedAction
from vocalguard.domain.events import EventType, Speaker
from vocalguard.infrastructure.contacts.book import normalize_number
from vocalguard.infrastructure.keywords.detector import normalize as fold_text
from vocalguard.infrastructure.telephony.twiml import (
    STREAM_TRACK_BOTH,
    STREAM_TRACK_VICTIM_ONLY,
    announce_twiml,
    empty_twiml,
    hold_music_twiml,
    inbound_call_twiml,
    join_conference_twiml,
    outgoing_call_twiml,
    outgoing_done_twiml,
    refusal_twiml,
    ringback_twiml,
    unavailable_twiml,
    voicemail_done_twiml,
)
from vocalguard.interface.http.phone_app import outgoing_authorizations, outgoing_number
from vocalguard.interface.lifecycle import release_call

if TYPE_CHECKING:
    from starlette.datastructures import QueryParams

    from vocalguard.config import Settings
    from vocalguard.domain.models import Contact

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

    Activé par défaut. Un réglage faux permet uniquement les tests locaux
    qui simulent Twilio sans credentials.

    L'URL validée est reconstruite depuis `settings.public_base_url` + le chemin,
    JAMAIS `request.url` : la signature de Twilio porte sur l'URL PUBLIQUE qu'il
    a appelée, alors que derrière ngrok le serveur ne voit qu'un
    `http://localhost:8000/...` (hôte réécrit, schéma perdu). C'est exactement à
    cet endroit que la validation échoue en pratique — d'où le log explicite.
    """
    settings = request.app.state.settings
    # Fichier sonore déterministe et public, aucun état d'appel. Les lecteurs
    # média Twilio doivent pouvoir le télécharger sans formulaire signé.
    if request.method == "GET" and request.url.path == "/twilio/voice/ringback.wav":
        return
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
            "webhook %s refusé. Renseigner le token avant de recevoir des appels réels.",
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
        "derrière un tunnel, "
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


# --- Routage de l'appel entrant : liste blanche vs analyse ------------------

LOOP_REFUSAL_TEXT = "This call cannot be completed."
"""Phrase servie à un appel refusé par la garde anti-boucle. Volontairement muette
sur la cause : l'appelant est soit nous-mêmes, soit la personne protégée qui a
composé son propre numéro renvoyé — dans les deux cas, expliquer le montage ne
sert personne, et le dire à un tiers renseignerait sur notre infrastructure."""

VICTIM_NOTICE_UNKNOWN = "Call from an unknown number. This call is protected."
"""Ce que la personne protégée entend, elle seule, quand l'appelant n'est pas au
carnet. « Appel protégé » est là pour qu'elle sache que l'analyse tourne : c'est
la contrepartie rassurante de l'annonce faite à l'appelant."""

_MASKED_CALLER_IDS = frozenset({"anonymous", "restricted", "unknown"})
"""Identifiants d'appelant que Twilio substitue au numéro quand il n'y en a pas.

Le test est explicite alors qu'il serait presque redondant — aucun de ces mots ne
se normalise en numéro, donc aucun ne peut être trouvé au carnet. Il sert à deux
choses : dire « appel masqué » dans le journal plutôt que « numéro absent du
carnet », et rendre impossible qu'un carnet un jour mal indexé fasse d'un appel
masqué un contact de confiance. C'est la seule erreur de ce routage qui serait
grave : accorder le silence à quelqu'un qui cache son numéro."""

_DEMO_OUTBOUND_TRUE = frozenset({"1", "true", "yes", "oui"})
"""Valeurs acceptées pour `?demo_outbound=` : le drapeau est écrit à la main dans
une URL, et un `demo_outbound=true` silencieusement ignoré ferait échouer la démo
par la garde anti-boucle, c'est-à-dire de la façon la plus déroutante possible."""


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
    "not me",
    "it s not me",
    "it is not me",
    "that s not me",
    "wasn t me",
    "not calling",
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
    "it s me",
    "it is me",
    "that s me",
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
    """Entrée de l'appel : on décide d'abord s'il se transmet ou s'il s'analyse.

    La personne protégée garde son numéro et renvoie sa ligne ici : TOUS ses
    appels passent donc par cette route, y compris ceux de sa famille. D'où
    l'ordre ci-dessous, qui est le produit lui-même et non de la plomberie :

    1. pas de `CallSid` → rien à faire, on accuse réception ;
    2. réécritures de DÉMO du `From` (jambe sortante du script, puis
       `DEMO_SPOOF_AS`), toujours loguées ;
    3. garde anti-boucle : un appel venant d'un de nos propres numéros ne doit
       surtout pas déclencher un rappel ;
    4. résolution de l'appelant dans le carnet ;
    5. **contact de confiance** → `<Dial><Conference>` nu : aucune annonce,
       aucun `<Stream>`, aucune transcription. La famille n'est pas écoutée, et
       c'est la promesse du produit, pas un réglage ;
    6. **inconnu / masqué / mode `analyze`** → `<Say>` à l'appelant, PUIS
       `<Start><Stream>`, PUIS la conférence. Cet ordre est le montage
       juridique : on n'analyse que ce qui suit l'information de l'appelant.

    Dans les deux cas une session est ouverte et la victime est rappelée : c'est
    la session que retrouvent les routes de contrôle et le dashboard, même pour
    un appel dont on ne captera pas un octet.
    """
    form = await request.form()
    call_sid = str(form.get("CallSid") or "")
    from_number = str(form.get("From") or "")

    settings = request.app.state.settings
    registry = request.app.state.registry
    controller = request.app.state.controller
    contacts = request.app.state.contacts

    if not call_sid:
        log.warning("Webhook inbound sans CallSid — appel ignoré")
        return _xml(empty_twiml())

    # Compatibilité avec une TwiML App dont l'URL Voice pointait déjà sur cette
    # route. Un appel SDK ne doit jamais être traité comme un inconnu entrant.
    if from_number.startswith("client:"):
        return await _outbound_call(request, form)

    # --- 2. Ce que l'appelant PRÉTEND être (démo seulement) ----------------
    #
    # Les deux réécritures se chaînent exprès : le script de démo déclare « c'est
    # le téléphone de l'arnaqueur qui parle », et `DEMO_SPOOF_AS` fait ensuite
    # croire que ce téléphone est celui de Lucas. C'est le scénario fort du pitch,
    # joué sans qu'aucun appel international ne soit payé — et sans que le chemin
    # de code diffère de celui d'un vrai appel entrant.
    numero_reel = from_number
    jambe_de_demo = _jambe_de_demo(request.query_params)
    if jambe_de_demo:
        from_number = _identite_presentee(request.query_params, from_number)
    from_number, usurpation_simulee = _usurpation_simulee(settings, from_number)

    # --- 3. Garde anti-boucle ---------------------------------------------
    if _boucle_de_renvoi(settings, from_number) and not jambe_de_demo:
        # ERROR et pas WARNING : c'est la panne qui se paie en argent. La ligne
        # protégée est renvoyée vers notre numéro Twilio, donc rappeler ce
        # numéro-là nous ferait rappeler nous-mêmes, à deux jambes facturées par
        # tour, jusqu'à épuisement du crédit. On refuse l'appel sans rien tenter.
        log.error(
            "APPEL REFUSÉ (boucle de renvoi) : %s est un de nos propres numéros "
            "(PROTECTED/VICTIM/TWILIO_PHONE_NUMBER). La victime n'est PAS rappelée : "
            "chaque tour de boucle coûterait deux jambes facturées. Si c'est la démo "
            "sortante, l'URL du webhook doit porter « ?demo_outbound=1 » "
            "(cf. scripts/lancer_appel_demo.py).",
            from_number,
        )
        return _xml(refusal_twiml(LOOP_REFUSAL_TEXT))

    # --- 4. Qui appelle ? --------------------------------------------------
    masque = _numero_masque(from_number)
    contact = None if masque else contacts.by_number(from_number)
    # Deux notions distinctes, deux variables : `contact` dit « on sait le
    # nommer », `confiance` dit « on lui doit le silence ». Une entrée en mode
    # `analyze` est au carnet — on l'annoncera par son nom à la personne
    # protégée — sans pour autant échapper à l'analyse. Les confondre serait
    # l'erreur dangereuse de ce routage : lire « connu » comme « de confiance ».
    confiance = contact if contact is not None and contact.is_trusted else None

    # Une session dans tous les cas, mais une ÉCOUTE seulement si l'appel
    # s'analyse. `transcribe=False` n'ouvre aucune piste Transcribe : l'appel de
    # confiance ne porte de toute façon aucun `<Stream>`, et deux connexions AWS
    # facturées pour une piste muette seraient payées pour rien.
    ecoute = confiance is None or settings.trusted_victim_only_analysis

    # Les locuteurs transcrits doivent correspondre aux pistes que le TwiML
    # diffuse, sinon on ouvre une connexion Transcribe facturée sur une piste qui
    # ne recevra jamais un octet — et une transcription vide ne ressemble à
    # aucune panne, donc personne ne le verrait. En mode « voix de l'abonnée
    # seule », le `<Stream>` ne porte que `outbound_track`.
    locuteurs: tuple[Speaker, ...] = (
        ("victim",) if confiance is not None and ecoute else ("caller", "victim")
    )

    # `open()` est idempotent : Twilio rejoue ce webhook en cas de timeout, et
    # deux sessions pour un appel dédoubleraient transcript et score.
    session = await registry.open(
        call_sid,
        caller_number=from_number,
        victim_number=settings.victim_phone_number,
        transcribe=ecoute,
        speakers=locuteurs,
    )
    recorder = getattr(request.app.state, "history_recorder", None)
    if recorder is not None:
        await recorder.start_call(
            call_sid,
            caller_number="" if masque else from_number,
            route="trusted_passthrough" if confiance else "analyzed",
            started_at=datetime.now(UTC) - timedelta(seconds=session.t()),
        )
    remember = getattr(controller, "remember_caller", None)
    if remember is not None:
        remember(call_sid, "" if masque else from_number, has_stream=ecoute)

    # L'appel de la victime part en tâche de fond : Twilio attend ce TwiML pour
    # placer l'appelant dans la conférence, et un aller-retour REST ici coûterait
    # une seconde d'attente côté scammeur — assez pour qu'il raccroche. Il part
    # aussi pour un appel de confiance : c'est un appel à transmettre, pas à
    # ignorer. Seule la boucle (ci-dessus) n'y a pas droit.
    task = asyncio.create_task(_dial_victim(controller, call_sid, request.app.state))
    _background.add(task)
    task.add_done_callback(_background.discard)

    if usurpation_simulee:
        # Le dashboard doit MONTRER le mensonge, pas seulement le journal : devant
        # un jury, « le serveur croit que c'est Lucas » est exactement ce qu'on
        # démontre, et un réglage qui ment sur l'identité ne doit laisser aucun
        # écran dans l'ignorance.
        await session.emit(
            EventType.ACTION,
            {
                "action": ReportedAction.SPOOF_SIMULATED.value,
                "target": "caller",
                # `ok` sur un fait de routage : aucune API n'a été appelée, mais
                # TOUS les événements `action` du système en portent un, et des
                # consommateurs lisent `data["ok"]` sans détour. L'omettre les
                # ferait tomber sur un `KeyError` au moment le plus bruyant de la
                # démo. Vrai, donc, au sens « la décision a bien été appliquée ».
                "ok": True,
                "real_from": numero_reel,
                "presented_as": from_number,
                "message": (
                    f"SIMULATED spoofing (demo mode): the call comes from {numero_reel} "
                    f"and is treated as coming from {from_number}."
                ),
            },
        )

    conference_status = f"{settings.public_base_url}/twilio/conference/status"

    if confiance is not None:
        log.info(
            "Appel %s de %s (%s) : contact de confiance, transmis sans annonce — %s",
            call_sid,
            confiance.name,
            from_number,
            "piste de la personne protégée seule (Lot 3)"
            if ecoute
            else "aucun flux audio, aucune transcription",
        )
        await session.emit(
            EventType.ACTION,
            {
                "action": ReportedAction.TRUSTED_PASSTHROUGH.value,
                "target": "caller",
                "ok": True,
                "contact": confiance.name,
                "message": (
                    f"Call from {confiance.name}: passed through without notice or analysis."
                ),
            },
        )
        return _xml(
            inbound_call_twiml(
                stream_url=settings.media_stream_url,
                conference_name=settings.conference_name_for(call_sid),
                conference_status_callback=conference_status,
                caller_number=from_number,
                caller_wait_url=f"{settings.public_base_url}/twilio/voice/ringback",
                # Aucune annonce : l'appelant de confiance ne doit rien entendre
                # qui lui apprenne qu'il a été reconnu — ni qu'un service existe.
                track=STREAM_TRACK_VICTIM_ONLY if ecoute else None,
            )
        )

    log.info(
        "Appel %s : %s — annonce à l'appelant puis analyse",
        call_sid,
        _motif_inconnu(contact, from_number, masque=masque),
    )
    await session.emit(
        EventType.ACTION,
        {
            "action": ReportedAction.CALLER_NOTICE.value,
            "target": "caller",
            "ok": True,
            # La phrase exacte qui a été dite, pas un résumé : c'est la pièce
            # qu'on produirait pour justifier l'analyse de cet appel.
            "message": settings.caller_notice_text,
        },
    )
    return _xml(
        inbound_call_twiml(
            stream_url=settings.media_stream_url,
            conference_name=settings.conference_name_for(call_sid),
            conference_status_callback=conference_status,
            # Transporté jusqu'au `<Stream>` parce que la trame `start` ne porte
            # pas le `From` : c'est la seule façon pour `/ws/media` de connaître
            # le numéro appelant si ce webhook n'est pas passé le premier.
            caller_number=from_number,
            caller_notice=settings.caller_notice_text,
            caller_notice_language=settings.caller_notice_language,
            caller_notice_bilingual=settings.caller_notice_bilingual,
            caller_wait_url=f"{settings.public_base_url}/twilio/voice/ringback",
            track=STREAM_TRACK_BOTH,
        )
    )


def _jambe_de_demo(params: QueryParams) -> bool:
    """L'URL porte-t-elle `?demo_outbound=1` ?

    Le piège que ce drapeau existe pour désamorcer : `scripts/lancer_appel_demo.py`
    inverse le sens de l'appel (Twilio appelle le téléphone de l'arnaqueur, pour
    que la France ne facture rien) et branche la jambe SORTANTE sur cette route.
    Dans ce sens, `From` vaut notre propre `TWILIO_PHONE_NUMBER` — la garde
    anti-boucle reconnaîtrait donc la démo comme une boucle et la refuserait.

    Pourquoi un paramètre d'URL et non un réglage `.env` : le drapeau appartient à
    UN appel, celui que le script vient de créer, pas à la configuration du
    service. Un `DEMO_MODE=true` oublié dans un `.env` désarmerait la garde pour
    tous les appels, y compris les vrais — c'est précisément la boucle à deux
    jambes facturées par tour qu'on refuse de rendre possible.
    """
    return (params.get("demo_outbound") or "").strip().casefold() in _DEMO_OUTBOUND_TRUE


def _identite_presentee(params: QueryParams, defaut: str) -> str:
    """Identité à présenter pour la jambe sortante de démo (`?presented_from=`).

    Sans elle, l'appel de démo s'annoncerait avec notre numéro Twilio : inconnu du
    carnet, donc analysé — ce qui marche, mais ne démontre ni la liste blanche ni
    l'usurpation. Avec elle, le script dit la vérité du plateau (« c'est le
    téléphone de l'arnaqueur qui parle »), et `DEMO_SPOOF_AS` peut ensuite jouer
    son rôle par-dessus, exactement comme sur un appel entrant.
    """
    presentee = (params.get("presented_from") or "").strip()
    if not presentee:
        return defaut
    log.warning(
        "IDENTITÉ DE DÉMO (presented_from) : la jambe sortante %s est traitée comme "
        "un appel entrant de %s. Garde anti-boucle désarmée pour cet appel seul.",
        defaut or "(sans numéro)",
        presentee,
    )
    return presentee


def _usurpation_simulee(settings: Settings, from_number: str) -> tuple[str, bool]:
    """Fait comme si l'appel du téléphone « arnaqueur » venait de `DEMO_SPOOF_AS`.

    Twilio interdit d'usurper un numéro qu'on ne possède pas — à raison — donc le
    moment fort du pitch (« Lucas » appelle depuis le numéro de Lucas, mais ce
    n'est pas lui) serait indémontrable. On simule la seule chose qui compte pour
    la démonstration : ce que notre serveur CROIT du `From`.

    Tout le reste du chemin ignore cette réécriture, et c'est voulu — on démontre
    le vrai routage, pas une maquette.

    Chaque usage est logué en MAJUSCULES et publié sur le bus. Un réglage qui ment
    sur l'identité de l'appelant ne doit jamais pouvoir agir en silence : un
    `.env` oublié après la démo ferait passer un inconnu pour un proche, donc
    accorderait le silence à l'appel qu'il fallait analyser.
    """
    if not settings.demo_spoof_active:
        return from_number, False
    # `normalize_number` des deux côtés : le `.env` est saisi à la main et Twilio
    # répond en E.164 strict — une comparaison brute raterait le bon numéro et la
    # démo ne montrerait rien, sans qu'aucune erreur ne l'explique.
    if normalize_number(from_number) != normalize_number(settings.scammer_phone_number):
        return from_number, False
    log.warning(
        "USURPATION SIMULÉE (DEMO_SPOOF_AS) : l'appel de %s est traité comme venant "
        "de %s. Réglage de DÉMO — aucun service réel ne doit tourner avec.",
        from_number or "(sans numéro)",
        settings.demo_spoof_as,
    )
    return settings.demo_spoof_as, True


def _boucle_de_renvoi(settings: Settings, from_number: str) -> bool:
    """Le `From` est-il un de nos propres numéros ?

    Comparaison normalisée des deux côtés (cf. `Settings.loop_guard_numbers`, qui
    les renvoie bruts et écarte les vides). Un `From` absent n'est jamais une
    boucle : normalisés, deux numéros vides seraient égaux, et un appel masqué —
    exactement celui qu'on veut analyser — serait refusé.
    """
    numero = normalize_number(from_number)
    if not numero:
        return False
    return any(numero == normalize_number(mien) for mien in settings.loop_guard_numbers)


def _numero_masque(from_number: str) -> bool:
    """Appel sans numéro présentable : champ vide ou mot-clé Twilio."""
    return not from_number.strip() or from_number.strip().casefold() in _MASKED_CALLER_IDS


def _motif_inconnu(contact: Contact | None, from_number: str, *, masque: bool) -> str:
    """Pourquoi cet appel est analysé, en une phrase, pour le journal.

    Les quatre motifs se ressemblent à l'écran et se déboguent très différemment :
    « masqué » est normal, « absent du carnet » peut être un contact à ajouter, et
    « mode analyze » est une décision prise exprès qu'il ne faut pas chercher à
    corriger comme un oubli.
    """
    if contact is not None:
        return f"contact {contact.name} en mode « analyze », traité comme un inconnu"
    if masque:
        return f"appel masqué ({from_number.strip() or 'numéro absent'})"
    return f"numéro {from_number} absent du carnet"


async def _dial_victim(controller, call_id: str, state: Any = None) -> None:
    # `dial_victim` n'est pas dans le port `CallController` : seul l'adapter
    # Twilio sait passer un appel. Sans lui (démo sans téléphone), on ne fait rien.
    dial = getattr(controller, "dial_victim", None)
    if dial is None:
        log.info("Contrôleur sans téléphonie : jambe victime non appelée (%s)", call_id)
        return
    try:
        victim_call_sid = await dial(call_id)
        log.info("Jambe victime créée pour %s : %s", call_id, victim_call_sid)
        if victim_call_sid is None and state is not None:
            await release_call(state, call_id)
    except Exception:
        log.exception("Impossible d'appeler la victime pour %s", call_id)


@router.api_route("/voice/join/{call_id}", methods=["GET", "POST"])
async def voice_join(call_id: str, request: Request) -> Response:
    """TwiML de la jambe sortante : la victime rejoint la conférence de l'appel.

    Le `<Say>` qui précède la conférence est le REPLI téléphonique, et il faut le
    lire comme tel : avec l'app du Lot 1 bis (jambe `client:`), l'identité de
    l'appelant s'affiche à l'écran et cette phrase devient inutile. Sur un
    téléphone ordinaire, elle est notre seule réponse au problème d'affichage —
    l'écran de la personne protégée montre notre numéro Twilio, jamais celui de
    l'appelant, ce qui rendrait la liste blanche invisible pour elle.
    """
    state = request.app.state
    settings = state.settings
    if state.registry.get(call_id) is None:
        # L'appelant a raccroché pendant que la victime décrochait : ne pas
        # recréer une conférence orpheline.
        return _xml(unavailable_twiml())
    return _xml(
        join_conference_twiml(
            conference_name=settings.conference_name_for(call_id),
            conference_status_callback=f"{settings.public_base_url}/twilio/conference/status",
            victim_notice=_annonce_appelant(state, call_id),
        )
    )


def _annonce_appelant(state: Any, call_id: str) -> str:
    """« Appel de Lucas Martin. » ou « Appel d'un numéro inconnu, appel protégé. »

    Reconstruit à la demande depuis la session et le carnet, et SURTOUT pas
    mémorisé au moment du routage : un état par appel de plus devrait être purgé
    dans `lifecycle.release_call`, et un état qu'on oublie d'y purger fuit d'un
    appel au suivant — la personne protégée entendrait le nom de l'appelant
    précédent.

    Session introuvable → aucune annonce. Dire « numéro inconnu » quand on ne
    sait pas serait une annonce FAUSSE, donc pire que le silence : elle vaudrait
    exactement le contraire de sa valeur pour un appel de confiance mal retrouvé.
    """
    session = state.registry.get(call_id)
    if session is None:
        log.warning(
            "Jambe victime de %s : session inconnue, aucune annonce plutôt qu'une annonce fausse",
            call_id,
        )
        return ""
    contact = state.contacts.by_number(session.caller_number) if session.caller_number else None
    # Le nom, même si le contact est en mode `analyze` : la personne protégée a
    # le droit de savoir qui appelle, et c'est une question d'affichage, pas la
    # décision de confiance (qui s'est prise, elle, dans `voice_inbound`).
    if contact is not None:
        return f"Call from {contact.name}."
    return VICTIM_NOTICE_UNKNOWN


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

    La jambe victime porte maintenant un call_id parent et son type de
    destination. Son SID est vérifié dans le contrôleur avant tout repli ou fin.
    """
    form = await request.form()
    call_sid = str(form.get("CallSid") or "")
    status = str(form.get("CallStatus") or "")
    log.info("Statut d'appel %s : %s", call_sid, status)

    parent = str(request.query_params.get("call_id") or "")
    leg = str(request.query_params.get("leg") or "")
    if parent or leg:
        state = request.app.state
        if parent and leg in {"app", "phone"} and state.registry.get(parent) is not None:
            task = asyncio.create_task(_victim_status(state, parent, call_sid, leg, status))
            _background.add(task)
            task.add_done_callback(_background.discard)
        return _xml(empty_twiml())

    if call_sid and status in {"completed", "canceled", "failed", "busy", "no-answer"}:
        await release_call(request.app.state, call_sid)

    return _xml(empty_twiml())


async def _victim_status(state: Any, parent: str, sid: str, leg: str, status: str) -> None:
    hook = getattr(state.controller, "victim_status", None)
    if hook is None or state.registry.get(parent) is None:
        return
    try:
        outcome = await hook(parent, sid, leg=leg, status=status)
        recorder = getattr(state, "history_recorder", None)
        # `completed` est aussi une preuve de connexion (les appels sans
        # réponse ont des statuts terminaux distincts). Le callback answered
        # peut arriver après le terminal, Twilio ne garantit pas leur ordre.
        if outcome in {"answered", "completed"} and recorder is not None:
            await recorder.mark_answered(parent)
        if outcome in {"unavailable", "completed"}:
            await release_call(state, parent)
    except Exception:
        log.exception("Statut de jambe victime ingérable pour %s", parent)


@router.api_route("/voice/ringback", methods=["GET", "POST"])
async def voice_ringback(request: Request) -> Response:
    base = request.app.state.settings.public_base_url
    return _xml(ringback_twiml(f"{base}/twilio/voice/ringback.wav"))


@lru_cache(maxsize=1)
def _ringback_audio() -> bytes:
    """Cadence téléphonique 440 Hz, 1,5 s de son puis 3,5 s de silence.

    Générée en mémoire, indépendante de tout audio reçu ou donné personnel.
    """
    sample_rate = 8000
    frames = bytearray()
    for index in range(5 * sample_rate):
        sample = (
            int(6000 * math.sin(2 * math.pi * 440 * index / sample_rate))
            if index < 1.5 * sample_rate
            else 0
        )
        frames.extend(struct.pack("<h", sample))
    output = io.BytesIO()
    with wave.open(output, "wb") as sound:
        sound.setnchannels(1)
        sound.setsampwidth(2)
        sound.setframerate(sample_rate)
        sound.writeframes(frames)
    return output.getvalue()


@router.get("/voice/ringback.wav")
async def voice_ringback_audio() -> Response:
    return Response(
        content=_ringback_audio(),
        media_type="audio/wav",
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def _require_private_voice_signature(request: Request) -> None:
    """Les sortants et dépôts vocaux exigent une vraie signature même en démo.

    Le mode de rejeu des appels entrants ne doit jamais autoriser un appel
    facturable ni l'ajout d'un enregistrement par un visiteur de l'URL publique.
    """
    settings = request.app.state.settings
    token = settings.twilio_auth_token
    signature = request.headers.get(SIGNATURE_HEADER, "")
    if not token or not settings.public_hostname or not signature:
        raise HTTPException(403, "Signature Twilio requise.")
    form = await request.form()
    params = {key: str(value) for key, value in form.items()}
    url = f"{settings.public_base_url}{request.url.path}"
    if request.url.query:
        url += f"?{request.url.query}"
    if not RequestValidator(token).validate(url, params, signature):
        # Le nonce d'appel est dans le corps, jamais dans les logs/URLs.
        raise HTTPException(403, "Signature Twilio invalide.")
    account = str(form.get("AccountSid") or "")
    if account and account != settings.twilio_account_sid:
        raise HTTPException(403, "Compte Twilio incorrect.")


@router.post("/voice/outbound")
async def voice_outbound(request: Request) -> Response:
    return await _outbound_call(request, await request.form())


async def _outbound_call(request: Request, form) -> Response:
    await _require_private_voice_signature(request)
    state = request.app.state
    settings = state.settings
    identity = str(form.get("From") or "")
    if identity != settings.voice_client_target:
        raise HTTPException(403, "Voice identity not allowed.")
    call_id = str(form.get("CallSid") or "")
    nonce = str(form.get("Authorization") or "")
    if not call_id or len(form.getlist("Authorization")) != 1:
        raise HTTPException(403, "Missing outgoing call authorization.")
    authorizations = outgoing_authorizations(state)
    already_active = authorizations.owns(call_id)
    number = authorizations.consume(nonce, identity=settings.voice_client_identity, call_id=call_id)
    if number is None:
        raise HTTPException(403, "Outgoing call authorization expired or already used.")
    # Le webhook ignore toute destination envoyée par le client. Vérifier une
    # seconde fois celle réservée protège aussi un changement de configuration.
    try:
        number = outgoing_number(number, settings)
        if already_active and state.registry.get(call_id) is None:
            raise HTTPException(409, "This call has ended.")
        if not already_active and state.registry.active():
            raise HTTPException(409, "A call is already in progress.")
        if not already_active:
            session = await state.registry.open(call_id, caller_number=number, transcribe=False)
            recorder = getattr(state, "history_recorder", None)
            if recorder is not None:
                await recorder.start_call(
                    call_id,
                    caller_number=number,
                    route="trusted_passthrough",
                    direction="outgoing",
                    started_at=datetime.now(UTC) - timedelta(seconds=session.t()),
                )
    except Exception:
        authorizations.forget(call_id)
        raise
    base = settings.public_base_url
    return _xml(
        outgoing_call_twiml(
            number=number,
            caller_id=settings.twilio_phone_number,
            done_url=f"{base}/twilio/voice/outbound/done/{call_id}",
            status_url=f"{base}/twilio/voice/outbound/status/{call_id}",
        )
    )


@router.post("/voice/outbound/status/{call_id}")
async def voice_outbound_status(call_id: str, request: Request) -> Response:
    await _require_private_voice_signature(request)
    state = request.app.state
    form = await request.form()
    authorizations = outgoing_authorizations(state)
    child_sid = str(form.get("CallSid") or "")
    parent = str(form.get("ParentCallSid") or "")
    if parent != call_id or not authorizations.bind_child(call_id, child_sid):
        return _xml(empty_twiml())
    status = str(form.get("CallStatus") or "")
    recorder = getattr(state, "history_recorder", None)
    if status in {"in-progress", "completed"} and recorder is not None:
        await recorder.mark_answered(call_id)
    if status in {"completed", "no-answer", "busy", "failed", "canceled"}:
        # Pas d'audio/Stream sur un sortant : les callbacks signés portent son
        # cycle de vie. Le statut parent reste un second filet idempotent.
        await release_call(state, call_id)
        authorizations.forget(call_id)
    return _xml(empty_twiml())


@router.post("/voice/outbound/done/{call_id}")
async def voice_outbound_done(call_id: str, request: Request) -> Response:
    await _require_private_voice_signature(request)
    state = request.app.state
    form = await request.form()
    authorizations = outgoing_authorizations(state)
    if str(form.get("CallSid") or "") != call_id:
        raise HTTPException(403, "Invalid outgoing call.")
    status = str(form.get("DialCallStatus") or "")
    child_sid = str(form.get("DialCallSid") or "")
    if authorizations.owns(call_id) and (
        not child_sid or authorizations.bind_child(call_id, child_sid)
    ):
        recorder = getattr(state, "history_recorder", None)
        if status == "completed" and recorder is not None:
            await recorder.mark_answered(call_id)
        await release_call(state, call_id)
        authorizations.forget(call_id)
    return _xml(outgoing_done_twiml(status))


@router.post("/voicemail/status")
async def voicemail_status(request: Request) -> Response:
    await _require_private_voice_signature(request)
    form = await request.form()
    if str(form.get("RecordingStatus") or "") != "completed":
        return _xml(empty_twiml())
    service = getattr(request.app.state, "voicemail", None)
    if service is None or not service.enabled:
        return _xml(empty_twiml())
    duration = str(form.get("RecordingDuration") or "")
    if not re.fullmatch(r"[0-9]{1,3}", duration) or not 0 < int(duration) <= 120:
        return _xml(empty_twiml())
    # Le service exige une attente persistée pour ce call_id. RecordingUrl est
    # ignorée : aucune URL fournie par le callback ne sera téléchargée.
    await service.capture(
        call_id=str(form.get("CallSid") or ""),
        recording_sid=str(form.get("RecordingSid") or ""),
        duration_s=int(duration),
    )
    return _xml(empty_twiml())


@router.post("/voicemail/done")
async def voicemail_done(request: Request) -> Response:
    await _require_private_voice_signature(request)
    return _xml(voicemail_done_twiml())


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
    #
    # L'ensemble accepté est `TRUSTED_CONTACT_PHONE_NUMBER` **et tout numéro du
    # carnet d'adresses**, parce que la vérification d'identité par code
    # (`application/verification.py`) envoie son SMS au numéro que LE CARNET
    # connaît pour le proche revendiqué — qui peut être Camille, le médecin, la
    # voisine, et pas seulement le contact du `.env`. Avec la garde d'origine,
    # leur « NON » était rejeté en silence : le texte du SMS leur demandait de
    # répondre, et leur réponse ne pesait sur rien. Ça reste un ensemble FERMÉ,
    # que la personne protégée contrôle entièrement — c'est ce qui compte, cette
    # garde étant la seule authentification de la route.
    expediteurs = _expediteurs_autorises(state, settings)
    if not expediteurs or normalize_number(from_number) not in expediteurs:
        log.warning(
            "SMS entrant de %s ignoré : seuls le contact de confiance et les "
            "numéros du carnet peuvent répondre (sid=%s)",
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


def _expediteurs_autorises(state: Any, settings: Any) -> frozenset[str]:
    """Numéros normalisés dont un SMS entrant peut être cru, sur cette route.

    Deux sources réunies, pas une :

      - `TRUSTED_CONTACT_PHONE_NUMBER`, le proche du `.env`, qui reçoit « Est-ce
        toi qui appelles en ce moment ? » lors d'une alerte ;
      - **tout numéro du carnet d'adresses**, parce que le code de vérification
        part au numéro que le carnet connaît pour le proche revendiqué, qui n'est
        pas forcément celui du `.env`.

    Les valeurs vides sont écartées, et c'est la garde qui porte tout le reste :
    normalisés, deux numéros vides sont égaux, donc un `From` absent passerait
    pour un expéditeur légitime et n'importe qui pourrait injecter « le proche a
    dit non » pour faire mettre en attente un appel parfaitement légitime.

    `getattr` sur `contacts` : les tests et le rejeu passent parfois un simple
    namespace comme `state`. Carnet absent = on se rabat sur le seul numéro du
    `.env`, soit le comportement d'avant la vérification par code — jamais sur
    « tout le monde ».
    """
    numeros = {normalize_number(settings.trusted_contact_phone_number)}

    carnet = getattr(state, "contacts", None)
    if carnet is not None:
        try:
            numeros.update(normalize_number(c.phone_number) for c in carnet.all())
        except Exception:
            # Un carnet illisible ne doit pas ouvrir la route : on garde ce qu'on
            # a, on le dit, et la réponse du proche du `.env` continue de passer.
            log.warning("Carnet illisible : seuls les numéros du .env peuvent répondre par SMS")

    return frozenset(numero for numero in numeros if numero)


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
