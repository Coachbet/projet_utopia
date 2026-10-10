"""Coupe-circuits : empêchent qu'un signal très fort soit dilué par la moyenne,
et qu'un signal peu fiable déclenche seul une intervention.

Un coupe-circuit renvoie un plancher (`floor`) ou un plafond (`cap`) appliqué
au score pondéré. Les planchers sont appliqués avant les plafonds, donc **un
plafond gagne contre un plancher** — c'est pourquoi le plafond « voix seule »
doit s'effacer devant un démenti du proche (cf. coupe-circuit 5).

Les six règles, par ordre de définition :

1. `payment_request_with_identity_mismatch` — plancher 85.
2. `false_authority_with_payment` — plancher 85.
3. `llm_high_confidence` — plancher 65.
4. `keywords_alone_watch` — plancher 40, filet anti-latence.
5. `voice_alone_capped` — plafond 64, la voix ne décide jamais seule.
6. `trusted_contact_denied` — plancher 85.

Pourquoi 2 et 6 comptent structurellement : sans elles et sans la 1, **85 est
mathématiquement hors d'atteinte**. Les poids hors voix plafonnent à
45 + 20 + 15 = 80, et le signal voix vaut 0 tant que `torch` n'est pas installé
(`make install-voice`). Toute la mise en attente du scammeur reposait sur un
seul coupe-circuit, qui exige un rapprochement d'identité ; il y a maintenant
trois chemins indépendants vers l'intervention.
"""

from __future__ import annotations

from dataclasses import dataclass

from vocalguard.domain.models import SignalSet
from vocalguard.domain.scoring.weights import (
    VOICE_ALONE_CAP,
    VOICE_ALONE_MARGIN,
    Weights,
)

PAYMENT_MARKERS = (
    "paiement",
    "payment",
    "virement",
    "transfer",
    "crypto",
    "bitcoin",
    "carte cadeau",
    "gift card",
    "coursier",
    "courier",
    "mandat",
)

AUTHORITY_MARKERS = (
    "fausse autorité",
    "fausse autorite",
    "false authority",
)
"""Libellé imposé à Claude dans `prompts.py`. C'est déjà un jugement du modèle :
il ne dit pas « l'appelant parle de la police », il dit que l'autorité
revendiquée est fausse."""

LLM_HIGH_CONFIDENCE = 0.9
KEYWORDS_ALONE_FLOOR = 40
KEYWORDS_ALONE_TRIGGER = 0.6
CONTACT_DENIED_FLOOR = 85

FALSE_AUTHORITY_FLOOR = 85
FALSE_AUTHORITY_MIN_RISK = 0.85
"""Seuil de confiance exigé en plus de la conjonction. Sans lui, une hypothèse
d'autorité peu assurée suffirait à mettre un appelant en attente."""


@dataclass(frozen=True, slots=True)
class BreakerResult:
    floor: int | None = None
    cap: int | None = None
    name: str = ""


def _mentions_payment(tactics: tuple[str, ...]) -> bool:
    joined = " ".join(tactics).casefold()
    return any(marker in joined for marker in PAYMENT_MARKERS)


def _mentions_false_authority(tactics: tuple[str, ...]) -> bool:
    joined = " ".join(tactics).casefold()
    return any(marker in joined for marker in AUTHORITY_MARKERS)


def evaluate(signals: SignalSet, weighted_score: float, weights: Weights) -> list[BreakerResult]:
    """Liste les coupe-circuits déclenchés, dans l'ordre où ils sont définis."""
    fired: list[BreakerResult] = []
    content = signals.content
    reputation = signals.reputation
    keywords = signals.keywords

    # 1. Paiement inhabituel + identité revendiquée qui ne colle pas au numéro.
    #    La conjonction des deux est quasi décisive : on force l'intervention.
    if (
        content is not None
        and _mentions_payment(content.tactics)
        and reputation is not None
        and reputation.identity_mismatch
    ):
        fired.append(BreakerResult(floor=85, name="payment_request_with_identity_mismatch"))

    # 2. Fausse autorité + demande de paiement. Aucune gendarmerie, aucun service
    #    des impôts ne réclame un virement au téléphone : la conjonction n'a
    #    pratiquement pas de version légitime.
    #
    #    Cette règle existe parce que la 1 est structurellement inatteignable
    #    pour cet archétype : une institution usurpée n'est pas dans le carnet
    #    d'adresses de la victime, donc `identity_mismatch` ne peut pas s'armer.
    #    Mesuré avec le vrai Claude sur `scenarios/scam_03_fausse_autorite.json`
    #    (risque 0,97) : le score plafonnait à 66, l'appelant n'était jamais mis
    #    en attente. C'est l'une des arnaques les plus répandues en France.
    #
    #    Le seuil de risque en plus de la conjonction est volontaire : on exige
    #    que Claude soit sûr, pas seulement qu'il évoque l'hypothèse.
    if (
        content is not None
        and content.risk >= FALSE_AUTHORITY_MIN_RISK
        and _mentions_false_authority(content.tactics)
        and _mentions_payment(content.tactics)
    ):
        fired.append(
            BreakerResult(floor=FALSE_AUTHORITY_FLOOR, name="false_authority_with_payment")
        )

    # 3. Claude très sûr de lui : au minimum l'alerte, quels que soient les autres.
    if content is not None and content.risk >= LLM_HIGH_CONFIDENCE:
        fired.append(BreakerResult(floor=65, name="llm_high_confidence"))

    # 4. Plan B latence (cf. README) : si Claude n'a pas encore répondu, les
    #    mots-clés déclenchent seuls la vigilance. Ils plafonnent sinon à 20
    #    points, donc n'atteindraient jamais le seuil.
    if content is None and keywords is not None and keywords.risk >= KEYWORDS_ALONE_TRIGGER:
        fired.append(BreakerResult(floor=KEYWORDS_ALONE_FLOOR, name="keywords_alone_watch"))

    # 5. La voix synthétique seule ne dépasse jamais 64.
    #    Sauf si le proche a démenti : le plafond existe pour empêcher la VOIX de
    #    décider seule, et un démenti humain est précisément une preuve non
    #    vocale. Sans cette exception, le plafond écraserait le plancher de la
    #    règle 5 (les plafonds sont appliqués après les planchers) et un « non »
    #    du vrai Lucas retomberait à 64, soit sous le seuil d'intervention.
    voice = signals.voice
    denied = reputation is not None and reputation.contact_denied
    if voice is not None and voice.risk > 0 and not denied:
        non_voice = weighted_score - weights.voice * voice.risk
        if non_voice < VOICE_ALONE_MARGIN:
            fired.append(BreakerResult(cap=VOICE_ALONE_CAP, name="voice_alone_capped"))

    # 6. Le vrai proche répond que ce n'est pas lui qui appelle. La seule
    #    personne qui puisse trancher l'a tranché : on force l'intervention.
    if denied:
        fired.append(BreakerResult(floor=CONTACT_DENIED_FLOOR, name="trusted_contact_denied"))

    return fired
