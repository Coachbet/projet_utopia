"""Vérification d'identité par code à 4 chiffres — le moment fort du pitch.

Un arnaqueur peut usurper le numéro affiché : la liste blanche du Lot 1 le laisse
alors passer pour « Lucas ». Ce qu'il ne peut pas faire, c'est lire un SMS envoyé
au VRAI téléphone de Lucas. À tout moment, la personne protégée peut donc
demander une vérification : un code à 4 chiffres part vers le numéro que **le
carnet d'adresses** connaît pour ce proche, et elle lui demande de le lui lire.
L'usurpateur, lui, n'a rien reçu.

Trois décisions de conception portent tout le fichier.

**1. Le destinataire vient TOUJOURS du carnet, jamais de l'appel.** C'est
l'invariant de sécurité, et il se formule mieux ainsi que « jamais au numéro
appelant » : en cas d'usurpation, le numéro du carnet *est* celui qui s'affiche,
et refuser cette coïncidence tuerait précisément le scénario pour lequel ce lot
existe. Elle n'est pas dangereuse — usurper un numéro d'appel ne donne pas la
réception de ses SMS, le message arrive sur la SIM du vrai Lucas. Ce qui serait
dangereux, c'est de prendre le `From` de l'appel comme destinataire : le code
irait au téléphone de l'arnaqueur. Le `From` n'est donc jamais passé à
`send_sms` ; seul `contact.phone_number` l'est. La coïncidence reste refusée
quand on est arrivé au contact par l'identité REVENDIQUÉE (`by_identity`) : là,
rien ne relie le numéro au carnet, et l'appelant a lui-même choisi le nom.

**2. Le code ne passe pas sur le bus d'événements.** `verify_requested` ne porte
que le nom du contact. Le bus est diffusé à deux consommateurs — le dashboard
*et* l'app — par un WebSocket sans authentification derrière une URL ngrok
publique : qui connaît l'URL lirait le code et pourrait le dicter, c'est-à-dire
réussir la vérification sans jamais avoir eu le téléphone du proche. La route
HTTP, elle, le renvoie : c'est l'app de la personne protégée qui la demande, et
l'affichage du code en grand est la moitié de l'usage.

**3. La baisse du risque après un code correct passe par le pipeline, et par le
seul signal de réputation.** `_disculper` appelle
`ScoringPipeline.contact_verified`, symétrique exact de `contact_replied` : un
ensemble `_identity_verified` par appel, réinjecté dans `ReputationSignal` par
`_with_verdict` (`identity_mismatch=False`, donc coupe-circuit « paiement +
identité » désarmé, et `risk` ramené à `KNOWN_CONTACT_RISK`), puis un score
republié. Écrire `session.signals.reputation` depuis ici aurait été un mensonge à
retardement : le pipeline reconstruit cet objet à chaque vérification de
réputation — c'est documenté dans `domain/models.py` — donc la baisse serait
effacée au retour suivant de Claude, sans que rien ne le dise.

Ce qui ne bouge PAS, et c'est la partie honnête : les signaux de contenu. Un vrai
petit-fils vérifié qui réclame quand même un virement en urgence garde son risque
de contenu, et le score peut rester élevé pour cette raison-là. Vérifier une
identité ne blanchit pas un propos. Un démenti du proche déjà reçu l'emporte
d'ailleurs sur un code correct arrivé après : c'est la personne qui parle en son
nom qui tranche.

Le reste du fichier applique la règle maison : préférer ne rien faire à agir sur
une information douteuse (cf. `_reply_is_denial` dans `interface/http/webhooks.py`).
Un code entendu faux fait répéter, il n'accuse pas ; une fin de fenêtre sur un
appel qu'on n'écoute pas ne conclut rien.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vocalguard.config import Settings
from vocalguard.domain.alerts import (
    ANNOUNCE_VERIFY_EXPIRED,
    ANNOUNCE_VERIFY_NO_CONTACT,
    ANNOUNCE_VERIFY_OK,
    ANNOUNCE_VERIFY_SAME_NUMBER,
    ANNOUNCE_VERIFY_SMS_FAILED,
    ANNOUNCE_VERIFY_UNHEARD,
    ANNOUNCE_VERIFY_WRONG,
    ReportedAction,
    announce_verify_code,
    sms_verify_code,
)
from vocalguard.domain.events import Event, EventType
from vocalguard.infrastructure.contacts.book import normalize_number
from vocalguard.infrastructure.keywords.detector import normalize as fold_text

if TYPE_CHECKING:
    from vocalguard.application.sessions import CallSession, SessionRegistry
    from vocalguard.domain.models import Contact
    from vocalguard.domain.ports import CallController, ContactBook

logger = logging.getLogger(__name__)


# --- Lecture des nombres français dits à voix haute -------------------------
#
# Le proche lit « 4821 » sur son téléphone et le dicte comme il le sent :
# « quatre huit deux un », « quarante-huit vingt et un », « quatre mille huit
# cent vingt et un », ou « 48 21 » tel que Transcribe l'écrit. Comparer la chaîne
# brute raterait tout sauf le dernier cas, et la vérification échouerait sur des
# appels parfaitement honnêtes.
#
# La segmentation est GLOUTONNE : on lit le plus long cardinal français valide
# qui commence à chaque mot, puis on concatène les groupes consécutifs. C'est ce
# qui donne à « quatre-vingt-un » sa seule lecture française (81) au lieu des
# trois que permettrait une énumération (81, « 4-20-1 », « 4-21 »). Une
# segmentation permissive ferait l'inverse de ce qu'on veut : elle accepterait un
# code que personne n'a lu, et chaque faux positif d'acceptation est un
# usurpateur validé.

_MOT = re.compile(r"[a-z]+|\d+")

_UNITS = {
    "zero": 0,
    "un": 1,
    "une": 1,
    "deux": 2,
    "trois": 3,
    "quatre": 4,
    "cinq": 5,
    "six": 6,
    "sept": 7,
    "huit": 8,
    "neuf": 9,
    "dix": 10,
    "onze": 11,
    "douze": 12,
    "treize": 13,
    "quatorze": 14,
    "quinze": 15,
    "seize": 16,
}

_TENS = {"vingt": 20, "trente": 30, "quarante": 40, "cinquante": 50}
"""« soixante » et « quatre-vingt » ne sont pas ici : ils portent des formes
composées (70-79, 80-99) que l'addition simple des autres dizaines ne couvre
pas, et chacun a sa fonction dédiée."""

_CENT = ("cent", "cents")
_VINGT = ("vingt", "vingts")

_PAD_MIN = 100
"""Un groupe n'est complété à 4 chiffres que s'il vaut au moins 100.

« quatre cent quatre-vingt-un » doit pouvoir valider le code 0481 : lu comme un
nombre, il vaut 481, et seul le zéro-complètement le rend comparable. Mais
compléter les petites valeurs transformerait le « un » de « j'ai un problème » en
candidat « 0001 » — un mot de liaison deviendrait une tentative de code."""


def _petit(mots: list[str], i: int) -> tuple[int, int] | None:
    """0 à 99 à partir de `mots[i]` : (valeur, index du mot suivant)."""
    n = len(mots)
    if i >= n:
        return None
    mot = mots[i]

    if mot == "soixante":
        return _soixante(mots, i)
    if mot == "quatre" and i + 1 < n and mots[i + 1] in _VINGT:
        return _quatre_vingt(mots, i)

    dizaine = _TENS.get(mot)
    if dizaine is not None:
        return _dizaine_unite(mots, i + 1, dizaine)

    valeur = _UNITS.get(mot)
    if valeur is None:
        return None
    if mot == "dix" and i + 1 < n and mots[i + 1] in ("sept", "huit", "neuf"):
        # 17, 18, 19 : écrits en deux mots dès que le trait d'union tombe.
        return 10 + _UNITS[mots[i + 1]], i + 2
    return valeur, i + 1


def _dizaine_unite(mots: list[str], j: int, base: int) -> tuple[int, int]:
    """Une dizaine simple (20 à 50) et son unité éventuelle."""
    n = len(mots)
    if j < n and mots[j] == "et" and j + 1 < n and _UNITS.get(mots[j + 1]) in (1, 11):
        # « vingt et un », « vingt et onze » (fautif mais entendu).
        return base + _UNITS[mots[j + 1]], j + 2
    if j < n:
        unite = _UNITS.get(mots[j])
        if unite is not None and 1 <= unite <= 9:
            return base + unite, j + 1
    # Le « et » qui suit n'appartenait pas au nombre (« trente et des
    # poussières ») : on ne le consomme pas, le mot suivant sera relu seul.
    return base, j


def _soixante(mots: list[str], i: int) -> tuple[int, int]:
    """60 à 79, y compris « soixante-dix-neuf » et « soixante et onze »."""
    n = len(mots)
    j = i + 1
    if j < n and mots[j] == "dix":
        j += 1
        if j < n:
            unite = _UNITS.get(mots[j])
            if unite is not None and 1 <= unite <= 9:
                return 70 + unite, j + 1
        return 70, j
    if j < n and mots[j] == "et" and j + 1 < n and _UNITS.get(mots[j + 1]) in (1, 11):
        return 60 + _UNITS[mots[j + 1]], j + 2
    if j < n:
        unite = _UNITS.get(mots[j])
        if unite is not None and (1 <= unite <= 9 or 11 <= unite <= 16):
            return 60 + unite, j + 1
    return 60, j


def _quatre_vingt(mots: list[str], i: int) -> tuple[int, int]:
    """80 à 99. `mots[i:i + 2]` vaut déjà « quatre vingt(s) »."""
    n = len(mots)
    j = i + 2
    if j < n and mots[j] == "dix":
        j += 1
        if j < n:
            unite = _UNITS.get(mots[j])
            if unite is not None and 1 <= unite <= 9:
                return 90 + unite, j + 1
        return 90, j
    if j < n:
        unite = _UNITS.get(mots[j])
        if unite is not None and (1 <= unite <= 9 or 11 <= unite <= 16):
            return 80 + unite, j + 1
    return 80, j


def _centaines(mots: list[str], i: int) -> tuple[int, int] | None:
    n = len(mots)
    if i >= n:
        return None
    if mots[i] in _CENT:
        return 100, i + 1
    multiplicateur = _UNITS.get(mots[i])
    if multiplicateur is not None and 2 <= multiplicateur <= 9:
        if i + 1 < n and mots[i + 1] in _CENT:
            return multiplicateur * 100, i + 2
    return None


def _sous_mille(mots: list[str], i: int) -> tuple[int, int] | None:
    total = 0
    j = i
    lu = False
    centaines = _centaines(mots, j)
    if centaines is not None:
        total += centaines[0]
        j = centaines[1]
        lu = True
    petit = _petit(mots, j)
    if petit is not None:
        total += petit[0]
        j = petit[1]
        lu = True
    return (total, j) if lu else None


def _cardinal(mots: list[str], i: int) -> tuple[int, int] | None:
    """Le plus long cardinal français qui commence à `mots[i]`, ou `None`.

    Borné à 9999 par construction : un seul « mille », et ce qui le précède est
    un sous-mille. Un code n'a que 4 chiffres, personne ne dira « un million ».
    """
    total = 0
    j = i
    lu = False

    if j < len(mots) and mots[j] == "mille":
        total += 1000
        j += 1
        lu = True
    else:
        milliers = _sous_mille(mots, j)
        if milliers is not None and milliers[1] < len(mots) and mots[milliers[1]] == "mille":
            total += milliers[0] * 1000
            j = milliers[1] + 1
            lu = True

    reste = _sous_mille(mots, j)
    if reste is not None:
        total += reste[0]
        j = reste[1]
        lu = True

    return (total, j) if lu else None


def codes_entendus(texte: str) -> tuple[str, ...]:
    """Toutes les suites de chiffres qu'on peut raisonnablement lire dans `texte`.

    Deux formes de candidats, parce que le proche dicte de deux façons :

      - la CONCATÉNATION des groupes numériques consécutifs — « quarante-huit
        vingt et un » donne « 4821 », comme « 48 21 » et « quatre huit deux un » ;
      - le groupe seul complété à quatre chiffres quand il vaut au moins 100 —
        « quatre cent quatre-vingt-un » donne « 0481 ».

    Les mots non numériques coupent la concatenation : « le code est 4821 »
    n'emprunte rien à « code ».
    """
    mots = _MOT.findall(fold_text(texte))
    candidats: list[str] = []
    groupe: list[str] = []
    i = 0
    n = len(mots)

    while i < n:
        if mots[i].isdigit():
            groupe.append(mots[i])
            i += 1
            continue
        lecture = _cardinal(mots, i)
        if lecture is None:
            if groupe:
                candidats.append("".join(groupe))
                groupe = []
            i += 1
            continue
        valeur, i = lecture
        groupe.append(str(valeur))
        if valeur >= _PAD_MIN:
            candidats.append(f"{valeur:04d}")

    if groupe:
        candidats.append("".join(groupe))

    # `dict.fromkeys` : dédoublonne en gardant l'ordre de lecture, utile quand on
    # inspecte la liste en débogage d'un appel réel.
    return tuple(c for c in dict.fromkeys(candidats) if c)


def code_reconnu(texte: str, code: str) -> bool:
    """Le code à 4 chiffres a-t-il été lu dans `texte` ?

    Inclusion et non égalité : le proche dit « alors, c'est le 4821 d'accord ? »,
    jamais « 4821 » tout court. Le prix est connu et assumé — un interlocuteur
    qui récite une longue suite de chiffres contenant par hasard le code serait
    cru. Sur une fenêtre de deux minutes ouverte par une demande explicite, c'est
    quelques chances sur dix mille, contre des échecs quotidiens si l'on exigeait
    l'égalité stricte.
    """
    if not code:
        return False
    return any(code in candidat for candidat in codes_entendus(texte))


# --- Déclencheur « la personne protégée demande une vérification » ----------
#
# Conservateur par construction : « code » SEUL ne déclenche RIEN. Elle dit
# « code » en lisant le code de sa carte bancaire, en épelant un code postal, en
# parlant du code de l'immeuble — et chaque faux déclenchement envoie un SMS au
# proche en pleine nuit. Il faut donc un mot de vérification, ou « code » dans
# une tournure qui en demande un.
#
# Résidu assumé : les accents tombent à la normalisation, donc « j'ai vérifié »
# se replie comme « je vérifie » et déclenche. Les distinguer demanderait une
# analyse grammaticale pour un gain nul — le coût de ce faux déclenchement est un
# SMS et un code annoncé, jamais une mise en attente.

_VERIFY_WORDS = (
    "verification",
    "verifications",
    "verifie",
    "verifies",
    "verifier",
    "verifiez",
    "verifions",
)

_VERIFY_PHRASES = (
    "code de verification",
    "demande un code",
    "demander un code",
    "envoie un code",
    "envoie le code",
    "envoyez un code",
    "envoyez le code",
)

_NON_MOT = re.compile(r"[^a-z0-9]+")


def _mots_bordes(texte: str) -> str:
    """Forme comparable d'un énoncé : mots repliés, séparés et bordés d'espaces.

    Même procédé que `webhooks._fold_words`, et pour la même raison : comparer
    des mots entourés d'espaces donne une frontière de mot sans une regex par
    terme, donc « verifie » ne se trouve pas dans « verification ».
    """
    return f" {_NON_MOT.sub(' ', fold_text(texte)).strip()} "


def demande_de_verification(texte: str) -> bool:
    """La personne protégée demande-t-elle une vérification à voix haute ?"""
    borde = _mots_bordes(texte)
    if any(f" {mot} " in borde for mot in _VERIFY_WORDS):
        return True
    return any(f" {phrase} " in borde for phrase in _VERIFY_PHRASES)


# --- Le service -------------------------------------------------------------

REASON_UNKNOWN_CALL = "appel_inconnu"
REASON_NO_CONTACT = "contact_inconnu"
REASON_CALLER_IS_TARGET = "destinataire_est_l_appelant"
REASON_SMS_REFUSED = "sms_refuse"
REASON_ALREADY_ACTIVE = "deja_actif"
"""Codes machine renvoyés par la route HTTP. Liste fermée : l'app de la personne
protégée affiche un texte par valeur, en ajouter une sans le dire laisserait un
écran muet."""

_VIA_CARNET = "numero_appelant_au_carnet"
_VIA_IDENTITE = "identite_revendiquee"


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    """Résultat d'une demande de vérification, tel que la route HTTP le renvoie.

    `code` est renseigné pour l'app (qui l'affiche en grand) et jamais pour le
    bus. `reason` reste informatif même quand `ok` est vrai : une demande
    répétée répond `deja_actif` avec le code déjà en vol.
    """

    ok: bool
    code: str | None = None
    sent_to: str | None = None
    expires_in: int = 0
    reason: str | None = None


@dataclass(slots=True)
class _Verification:
    """Le seul code actif d'un appel, et ce qu'on a entendu depuis."""

    code: str
    contact_name: str
    expires_at: float
    heard_caller: bool = False
    """Au moins un segment final de la piste APPELANT est arrivé pendant la
    fenêtre. C'est la preuve qu'on écoutait vraiment : sans elle, la fin de la
    fenêtre ne peut rien conclure (appel de confiance sans `<Stream>`, ou
    transcription tombée)."""
    wrong_heard: bool = False


class IdentityVerifier:
    """Implémente `EventSink` : on le branche dans le `FanOutSink` de `create_app`.

    Il y est abonné pour deux raisons, et une seule suffirait à l'y mettre :
    entendre le code sur la piste appelant, et entendre la demande de
    vérification sur la piste victime. Les deux n'existent que sur un appel
    analysé ; le déclencheur principal (`POST /control/calls/{id}/verify`) n'a
    besoin de rien de tout ça et marche aussi sur un appel de confiance.
    """

    def __init__(
        self,
        *,
        registry: SessionRegistry,
        controller: CallController,
        contacts: ContactBook,
        settings: Settings,
        pipeline: Any = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._registry = registry
        self._controller = controller
        self._contacts = contacts
        self._settings = settings
        self._pipeline = pipeline
        self._clock = clock
        self._sleep = sleep
        """Point d'injection, pas un détail de test : une suite qui attendrait
        vraiment la fenêtre de 120 s ne serait jamais lancée, et une suite qui
        la mettrait à 0 s ne pourrait rien faire entendre à l'appelant avant la
        fermeture. On veut fermer la fenêtre à l'instant choisi."""

        self._pending: dict[str, _Verification] = {}
        # Un minuteur par appel, groupé comme dans `ScoringPipeline._tasks` :
        # oublier un appel ne doit pas toucher à la fenêtre d'un autre, et deux
        # appels simultanés arrivent vraiment. La référence est tenue ici parce
        # que la boucle asyncio ne garde qu'une référence faible sur ses tâches :
        # sans ce dictionnaire, le minuteur peut être ramassé par le GC pendant
        # son `sleep` et la fenêtre ne se fermerait jamais.
        self._tasks: dict[str, set[asyncio.Task[None]]] = {}

    @property
    def _ttl(self) -> int:
        return max(0, int(self._settings.verify_code_ttl_seconds))

    # --- Déclencheurs ------------------------------------------------------

    async def request(self, call_id: str, *, source: str = "app") -> VerificationOutcome:
        """Envoie un code au vrai proche et l'annonce à la personne protégée.

        `source` ne sert qu'à la trace (`app`, `dtmf`, `phrase`) : il figure dans
        l'événement et répond, en démo comme en terrain, à la question du Lot 0
        §5 — quel déclencheur a réellement fonctionné.
        """
        session = self._registry.get(call_id)
        if session is None:
            # La route HTTP répond 404 avant d'arriver ici ; ce chemin protège
            # les déclencheurs internes (touche, phrase) d'une course avec le
            # raccrochage.
            logger.warning("vérification demandée pour un appel inconnu : %s", call_id)
            return VerificationOutcome(ok=False, reason=REASON_UNKNOWN_CALL)

        active = self._pending.get(call_id)
        if active is not None:
            # Idempotence : deux clics, ou un clic puis la phrase « vérifie ».
            # Un second SMS poserait au proche une question qu'il a déjà sous les
            # yeux, et un second code énoncé dans l'oreille de la personne
            # protégée rendrait la vérification impossible — elle ne saurait plus
            # lequel attendre.
            logger.info(
                "appel %s : vérification déjà en cours, déclencheur %s ignoré", call_id, source
            )
            return VerificationOutcome(
                ok=True,
                code=active.code,
                sent_to=active.contact_name,
                expires_in=self._remaining(active),
                reason=REASON_ALREADY_ACTIVE,
            )

        cible = self._destinataire(session)
        if cible is None:
            await self._announce(call_id, ANNOUNCE_VERIFY_NO_CONTACT)
            return VerificationOutcome(ok=False, reason=REASON_NO_CONTACT)

        contact, numero, via = cible
        if via == _VIA_IDENTITE and numero == normalize_number(session.caller_number):
            # Arrivé par l'identité revendiquée, donc par un nom que l'appelant a
            # choisi lui-même, et le numéro du carnet est celui de l'appel : le
            # code irait à celui qu'on vérifie. (Inatteignable en théorie —
            # `by_number` aurait répondu avant — mais c'est la garde dont
            # l'absence coûterait le plus cher.)
            logger.warning(
                "appel %s : vérification refusée, le contact %s porte le numéro appelant",
                call_id,
                contact.name,
            )
            await self._announce(call_id, ANNOUNCE_VERIFY_SAME_NUMBER)
            return VerificationOutcome(ok=False, reason=REASON_CALLER_IS_TARGET)

        code = f"{secrets.randbelow(10000):04d}"
        pending = _Verification(
            code=code,
            contact_name=contact.name,
            expires_at=self._clock() + self._ttl,
        )
        # Réservation AVANT le premier `await`, comme `InterventionRunner
        # .handle_score` : deux demandes concurrentes ne peuvent plus produire
        # deux codes, puisque la seconde trouve la première posée.
        self._pending[call_id] = pending

        envoye = await self._send_sms(numero, sms_verify_code(code, self._owner()))
        if not envoye:
            # On rend la réservation : le code n'a jamais atteint le proche, donc
            # il n'existe pas. Le garder rendrait la vérification suivante
            # impossible (« déjà en cours ») pour un code que personne n'a reçu.
            self._pending.pop(call_id, None)
            await self._announce(call_id, ANNOUNCE_VERIFY_SMS_FAILED)
            return VerificationOutcome(ok=False, reason=REASON_SMS_REFUSED)

        await self._announce(call_id, announce_verify_code(contact.name, code))
        self._arm_expiry(call_id, pending)

        logger.info(
            "appel %s : code de vérification envoyé à %s (%s, déclencheur %s)",
            call_id,
            contact.name,
            via,
            source,
        )
        await session.emit(
            EventType.ACTION,
            # Pas de `code` ici, et ce n'est pas un oubli : cf. la décision 2 du
            # docstring de module. Le dashboard et l'app lisent ce bus.
            {
                "action": ReportedAction.VERIFY_REQUESTED.value,
                "target": "trusted_contact",
                "contact": contact.name,
                "source": source,
                "ok": True,
            },
        )
        return VerificationOutcome(ok=True, code=code, sent_to=contact.name, expires_in=self._ttl)

    async def on_dtmf(self, call_id: str, digit: str) -> None:
        """Touche pressée pendant l'appel : « 1 » demande une vérification.

        **Déclencheur non éprouvé, jamais le chemin principal.** `<Start><Stream>`
        est unidirectionnel, et il n'est PAS vérifié que Twilio remonte les
        touches par ce canal : c'est précisément la question laissée ouverte par
        le Lot 0 §5, qui n'a pas encore été jouée au téléphone. Le parseur sait
        lire une trame `dtmf` (`infrastructure/telephony/media_stream.py`), donc
        le câblage coûte trois lignes et ne coûte rien s'il ne sert jamais. Tant
        que le test terrain n'a pas dit « oui », le bouton de l'app reste le seul
        déclencheur sur lequel compter.

        Ne lève jamais : on est dans la boucle média, et perdre une touche est
        acceptable là où perdre l'appel ne l'est pas.
        """
        try:
            if digit.strip() != "1":
                return
            logger.info("appel %s : touche 1 reçue, vérification demandée", call_id)
            await self.request(call_id, source="dtmf")
        except Exception:
            logger.exception("touche %r ingérable sur %s", digit, call_id)

    async def emit(self, event: Event) -> None:
        """Port `EventSink` : n'écoute que les `transcript` finaux.

        Ne lève jamais — une exception remonterait dans le `FanOutSink` et
        priverait le dashboard des événements suivants.
        """
        try:
            if event.type is not EventType.TRANSCRIPT:
                return
            # Les partiels de Transcribe bougent encore : un code à moitié
            # reconnu serait comparé, puis démenti au segment suivant.
            if not event.data.get("is_final", True):
                return
            texte = str(event.data.get("text") or "")
            if not texte.strip():
                return
            if event.speaker == "caller":
                await self._on_caller(event.call_id, texte)
            elif event.speaker == "victim":
                await self._on_victim(event.call_id, texte)
        except Exception:
            logger.exception("transcript ingérable pour la vérification de %s", event.call_id)

    # --- Écoute de la fenêtre ----------------------------------------------

    async def _on_caller(self, call_id: str, texte: str) -> None:
        pending = self._pending.get(call_id)
        if pending is None:
            return

        # Marqué avant toute comparaison : ce qui compte ici n'est pas le
        # contenu mais le fait qu'on reçoive bien la piste appelant. C'est cette
        # preuve d'écoute qui autorisera la fin de la fenêtre à conclure.
        pending.heard_caller = True

        if code_reconnu(texte, pending.code):
            await self._conclure_correct(call_id, pending)
            return

        if not any(len(candidat) >= 4 for candidat in codes_entendus(texte)):
            # Aucune tentative de code : il parle, c'est tout.
            return
        if pending.wrong_heard:
            # Déjà dit une fois. Le répéter à chaque phrase chiffrée occuperait
            # la ligne au moment où elle a besoin d'écouter.
            return
        pending.wrong_heard = True
        logger.info("appel %s : un code faux a été lu par l'appelant", call_id)
        await self._announce(call_id, ANNOUNCE_VERIFY_WRONG)

    async def _on_victim(self, call_id: str, texte: str) -> None:
        if self._pending.get(call_id) is not None:
            return
        if not demande_de_verification(texte):
            return
        if self._registry.get(call_id) is None:
            return
        logger.info("appel %s : vérification demandée à voix haute", call_id)
        await self.request(call_id, source="phrase")

    async def _conclure_correct(self, call_id: str, pending: _Verification) -> None:
        self._pending.pop(call_id, None)
        self._cancel_tasks(call_id)
        session = self._registry.get(call_id)
        logger.info(
            "appel %s : code correct, %s est bien au téléphone", call_id, pending.contact_name
        )
        await self._announce(call_id, ANNOUNCE_VERIFY_OK)
        if session is None:
            return
        await session.emit(
            EventType.ACTION,
            {
                "action": ReportedAction.IDENTITY_VERIFIED.value,
                "target": "caller",
                "contact": pending.contact_name,
                "ok": True,
            },
        )
        # La baisse du risque, par le pipeline et par le signal de réputation :
        # c'est le pendant exact de `_escalader`, et pour la même raison. Écrire
        # `session.signals.reputation` depuis ici serait un mensonge à
        # retardement — `_check_reputation` reconstruit cet objet à neuf au
        # prochain retour de Claude, et la disculpation disparaîtrait sans que
        # rien ne le dise.
        await self._disculper(call_id)

    def _arm_expiry(self, call_id: str, pending: _Verification) -> None:
        task = asyncio.create_task(self._expire(call_id, pending))
        self._tasks.setdefault(call_id, set()).add(task)
        task.add_done_callback(lambda done, cid=call_id: self._discard_task(cid, done))

    async def _expire(self, call_id: str, pending: _Verification) -> None:
        """Fin de la fenêtre : ce qu'on n'a pas entendu doit être dit honnêtement."""
        await self._sleep(self._ttl)

        # Gardes après l'`await`, motif de `ScoringPipeline._run_llm` : la
        # vérification a pu être conclue, remplacée, ou l'appel raccroché
        # pendant qu'on dormait. Émettre alors mettrait un événement après la
        # fin de l'appel sur la frise du dashboard.
        if self._pending.get(call_id) is not pending:
            return
        session = self._registry.get(call_id)
        if session is None:
            self._pending.pop(call_id, None)
            return
        self._pending.pop(call_id, None)

        if not pending.heard_caller:
            # Aucun segment de la piste appelant : soit l'appel est un appel de
            # confiance (pas de `<Stream>`, donc pas un octet d'audio ici, par
            # conception), soit la transcription est tombée. Dans les deux cas on
            # ne sait pas si le code a été lu — et conclure au démenti sur un
            # silence qu'on n'a pas pu écouter mettrait en attente un vrai
            # petit-fils. On informe, on ne conclut pas.
            logger.info(
                "appel %s : fin de la fenêtre de vérification sans aucune écoute de l'appelant",
                call_id,
            )
            await self._announce(call_id, ANNOUNCE_VERIFY_UNHEARD)
            await session.emit(
                EventType.ACTION,
                {
                    "action": ReportedAction.VERIFY_EXPIRED.value,
                    "target": "caller",
                    "ok": False,
                    "reasons": ["appel non analysé : le code n'a pas pu être entendu"],
                },
            )
            return

        # On écoutait, et le bon code n'est jamais venu. Un code faux lu se
        # distingue d'un silence : le premier est une tentative échouée
        # (`identity_verified` faux), le second est une fenêtre vide
        # (`verify_expired`). Même conséquence, mais la frise du dashboard et le
        # débriefing terrain ne racontent pas la même histoire.
        action = (
            ReportedAction.IDENTITY_VERIFIED.value
            if pending.wrong_heard
            else ReportedAction.VERIFY_EXPIRED.value
        )
        logger.info("appel %s : vérification échouée (%s)", call_id, action)
        await self._announce(call_id, ANNOUNCE_VERIFY_EXPIRED)
        await session.emit(
            EventType.ACTION,
            {
                "action": action,
                "target": "caller",
                "contact": pending.contact_name,
                "ok": False,
            },
        )
        await self._escalader(call_id)

    # --- Purge --------------------------------------------------------------

    def forget(self, call_id: str) -> None:
        """Oublie un appel terminé et annule son minuteur.

        Appelée par `lifecycle.release_call` via son helper `_forget`, au même
        titre que `pipeline` et `controller`. Sans l'annulation, un minuteur armé
        se réveillerait après le raccrochage pour annoncer une expiration dans
        une conférence qui n'existe plus et poser un événement après la fin de
        l'appel sur la frise — un symptôme qu'on aurait créé nous-mêmes.

        Les tâches ne sont pas retirées de `_tasks` ici : leur rappel de fin s'en
        charge, ce qui laisse `aclose()` les drainer.
        """
        self._pending.pop(call_id, None)
        self._cancel_tasks(call_id)

    async def aclose(self) -> None:
        """Attend les minuteurs en vol, tous appels confondus (tests, rejeu)."""
        pending = [task for tasks in self._tasks.values() for task in tasks]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _cancel_tasks(self, call_id: str) -> None:
        for task in tuple(self._tasks.get(call_id, ())):
            task.cancel()

    def _discard_task(self, call_id: str, task: asyncio.Task[None]) -> None:
        """Retire une tâche terminée sans ressusciter l'entrée d'un appel oublié."""
        tasks = self._tasks.get(call_id)
        if tasks is None:
            return
        tasks.discard(task)
        if not tasks:
            self._tasks.pop(call_id, None)

    # --- Dehors -------------------------------------------------------------

    def _destinataire(self, session: CallSession) -> tuple[Contact, str, str] | None:
        """Le contact à qui envoyer le code, son numéro de carnet, et par quelle voie.

        L'ordre est celui du cahier des charges, et il n'est pas interchangeable :
        le numéro appelant d'abord, parce qu'en cas d'usurpation c'est lui qui
        désigne le proche dont l'arnaqueur se réclame.
        """
        contact: Contact | None = None
        via = _VIA_CARNET
        if session.caller_number:
            contact = self._contacts.by_number(session.caller_number)

        if contact is None:
            content = session.signals.content
            claimed = content.claimed_identity if content is not None else None
            if claimed:
                contact = self._contacts.by_identity(claimed)
                via = _VIA_IDENTITE

        if contact is None:
            logger.info(
                "appel %s : ni le numéro appelant ni l'identité revendiquée ne sont au carnet",
                session.call_id,
            )
            return None

        numero = normalize_number(contact.phone_number)
        if not numero:
            # Un numéro vide normalisé resterait vide, et `send_sms` partirait
            # vers nulle part en renvoyant peut-être `True` : on refuse ici.
            logger.warning(
                "appel %s : contact %s sans numéro exploitable, vérification impossible",
                session.call_id,
                contact.name,
            )
            return None
        return contact, numero, via

    def _owner(self) -> str:
        """Le prénom de la personne protégée, s'il est déclaré.

        `getattr` : `owner` n'est pas dans le port `ContactBook`, et la doublure
        `StaticContactBook` ne l'expose pas. Son absence donne un SMS sans nom,
        ce que `sms_verify_code` sait formuler.
        """
        return str(getattr(self._contacts, "owner", "") or "")

    def _remaining(self, pending: _Verification) -> int:
        return max(0, int(pending.expires_at - self._clock()))

    async def _send_sms(self, numero: str, message: str) -> bool:
        """Le SEUL appel à `send_sms` du fichier, et son numéro vient du carnet.

        Un seul point de sortie : c'est ce qui rend l'invariant vérifiable d'un
        coup d'œil, et c'est aussi ce qu'un test peut surveiller.
        """
        try:
            return bool(await self._controller.send_sms(numero, message))
        except Exception:
            logger.exception("SMS de vérification impossible")
            return False

    async def _announce(self, call_id: str, message: str) -> bool:
        """Parle à la personne protégée SEULE (`announce_to_victim`).

        Jamais `<Say>` dans la conférence : l'appelant entendrait le code et le
        répéterait, ce qui transformerait la vérification en formalité.
        """
        try:
            return bool(await self._controller.announce_to_victim(call_id, message))
        except Exception:
            logger.exception("annonce de vérification impossible sur %s", call_id)
            return False

    async def _escalader(self, call_id: str) -> None:
        """Vérification manquée = démenti du proche, par LE chemin existant.

        On n'écrit pas un second coupe-circuit : `contact_replied(denied=True)`
        est déjà celui du SMS « NON » et du bouton du dashboard, il est
        idempotent, et c'est l'escalade existante qui met l'appelant en attente
        et prévient le proche. Un chemin parallèle aurait deux comportements à
        maintenir pour un seul fait.

        `getattr` + `inspect.isawaitable`, comme `webhooks._notify_contact_denied` :
        le hook n'est pas dans les ports, et le pipeline peut être une doublure.
        """
        hook = getattr(self._pipeline, "contact_replied", None)
        if hook is None:
            logger.warning(
                "Pipeline sans `contact_replied` : la vérification manquée de %s "
                "ne pèse pas sur le score",
                call_id,
            )
            return
        try:
            result = hook(call_id, denied=True)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("escalade impossible après la vérification manquée de %s", call_id)

    async def _disculper(self, call_id: str) -> None:
        """Code correct : on abaisse le risque, par le pipeline et par lui seul.

        Symétrique d'`_escalader` jusque dans sa prudence. `contact_verified`
        n'abaisse QUE le signal de réputation : il désarme `identity_mismatch`,
        donc le coupe-circuit « paiement + identité ». Les signaux de contenu ne
        bougent pas, et c'est la partie honnête du dispositif — un vrai
        petit-fils vérifié qui réclame quand même un virement en urgence garde
        son risque de contenu. Vérifier une identité ne blanchit pas un propos.

        Un pipeline sans ce hook (doublure, rejeu) ne fait pas échouer la
        vérification : la personne protégée a déjà entendu « Code correct », ce
        qui est l'essentiel. On nomme la conséquence au lieu de lever.
        """
        hook = getattr(self._pipeline, "contact_verified", None)
        if hook is None:
            logger.warning(
                "Pipeline sans `contact_verified` : le code correct de %s rassure "
                "la personne protégée mais n'abaisse pas le score",
                call_id,
            )
            return
        try:
            result = hook(call_id)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("disculpation impossible après le code correct de %s", call_id)
