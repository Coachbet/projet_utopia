"""Coupe-circuits : empêchent qu'un signal très fort soit dilué par la moyenne,
et qu'un signal peu fiable déclenche seul une intervention.

Un coupe-circuit renvoie un plancher (`floor`) ou un plafond (`cap`) appliqué
au score pondéré. Les planchers sont appliqués avant les plafonds, donc **un
plafond gagne contre un plancher** — c'est pourquoi le plafond « voix seule »
doit s'effacer devant un démenti du proche (cf. coupe-circuit 4).

Les cinq règles, par ordre de définition :

1. `payment_request_with_identity_mismatch` — plancher 85.
2. `llm_high_confidence` — plancher 65.
3. `keywords_alone_watch` — plancher 40, filet anti-latence.
4. `voice_alone_capped` — plafond 64, la voix ne décide jamais seule.
5. `trusted_contact_denied` — plancher 85.

Pourquoi la 5 compte structurellement : sans elle et sans la 1, **85 est
mathématiquement hors d'atteinte**. Les poids hors voix plafonnent à
45 + 20 + 15 = 80, et le signal voix vaut 0 tant que `torch` n'est pas installé
(`make install-voice`). Toute la mise en attente du scammeur reposait donc sur
un seul coupe-circuit ; il y en a maintenant deux, indépendants.
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

LLM_HIGH_CONFIDENCE = 0.9
KEYWORDS_ALONE_FLOOR = 40
KEYWORDS_ALONE_TRIGGER = 0.6
CONTACT_DENIED_FLOOR = 85


@dataclass(frozen=True, slots=True)
class BreakerResult:
    floor: int | None = None
    cap: int | None = None
    name: str = ""


def _mentions_payment(tactics: tuple[str, ...]) -> bool:
    joined = " ".join(tactics).casefold()
    return any(marker in joined for marker in PAYMENT_MARKERS)


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

    # 2. Claude très sûr de lui : au minimum l'alerte, quels que soient les autres.
    if content is not None and content.risk >= LLM_HIGH_CONFIDENCE:
        fired.append(BreakerResult(floor=65, name="llm_high_confidence"))

    # 3. Plan B latence (cf. README) : si Claude n'a pas encore répondu, les
    #    mots-clés déclenchent seuls la vigilance. Ils plafonnent sinon à 20
    #    points, donc n'atteindraient jamais le seuil.
    if content is None and keywords is not None and keywords.risk >= KEYWORDS_ALONE_TRIGGER:
        fired.append(BreakerResult(floor=KEYWORDS_ALONE_FLOOR, name="keywords_alone_watch"))

    # 4. La voix synthétique seule ne dépasse jamais 64.
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

    # 5. Le vrai proche répond que ce n'est pas lui qui appelle. La seule
    #    personne qui puisse trancher l'a tranché : on force l'intervention.
    if denied:
        fired.append(BreakerResult(floor=CONTACT_DENIED_FLOOR, name="trusted_contact_denied"))

    return fired
