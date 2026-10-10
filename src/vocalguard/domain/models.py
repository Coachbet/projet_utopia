"""Objets métier : ce que le système sait d'un appel, indépendamment de Twilio/AWS."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from vocalguard.domain.alerts import AlertLevel
from vocalguard.domain.events import Speaker


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    speaker: Speaker
    text: str
    t: float
    is_final: bool


@dataclass(frozen=True, slots=True)
class KeywordHit:
    term: str
    weight: int
    category: str


class LineType(StrEnum):
    """Valeurs renvoyées par Twilio Lookup v2 `line_type_intelligence`."""

    MOBILE = "mobile"
    LANDLINE = "landline"
    FIXED_VOIP = "fixedVoip"
    NON_FIXED_VOIP = "nonFixedVoip"
    TOLL_FREE = "tollFree"
    PERSONAL = "personal"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Contact:
    """Entrée du carnet d'adresses de la victime."""

    name: str
    phone_number: str
    relation: str = ""
    aliases: tuple[str, ...] = ()

    def matches_identity(self, claimed: str) -> bool:
        """L'identité revendiquée par l'appelant désigne-t-elle ce contact ?

        Claude renvoie du texte libre ("Lucas, petit-fils"), donc on cherche
        le nom ou la relation à l'intérieur de la phrase.
        """
        needle = claimed.strip().casefold()
        if not needle:
            return False
        candidates = (self.name, self.relation, *self.aliases)
        return any(c and c.casefold() in needle for c in candidates)


# --- Les quatre signaux ----------------------------------------------------
# Chacun expose `risk` dans [0, 1]. La pondération vit dans scoring/.


@dataclass(frozen=True, slots=True)
class ContentSignal:
    """Sortie JSON de Claude."""

    risk: float
    tactics: tuple[str, ...] = ()
    claimed_identity: str | None = None
    explanation: str = ""


@dataclass(frozen=True, slots=True)
class KeywordSignal:
    risk: float
    hits: tuple[KeywordHit, ...] = ()

    @property
    def terms(self) -> tuple[str, ...]:
        return tuple(h.term for h in self.hits)


@dataclass(frozen=True, slots=True)
class VoiceSignal:
    """Probabilité « synthétique » lissée sur plusieurs fenêtres.

    Indice seulement. Un plafond dans le scoring l'empêche de déclencher une
    intervention à elle seule — le modèle généralise mal sur de l'audio
    téléphonique 8 kHz.
    """

    risk: float
    windows_analyzed: int = 0


@dataclass(frozen=True, slots=True)
class ReputationSignal:
    risk: float
    line_type: LineType = LineType.UNKNOWN
    in_contacts: bool = False
    identity_mismatch: bool = False
    reasons: tuple[str, ...] = ()

    contact_denied: bool = False
    """Le vrai proche a répondu au SMS que ce n'est pas lui qui appelle.

    La seule personne qui sache trancher l'a tranché : c'est le signal le plus
    fort du système. Porté ici parce qu'un démenti est de la vérification
    d'identité, comme `identity_mismatch`.

    Attention, la source de vérité n'est pas cet objet : il est reconstruit à
    neuf à chaque vérification de réputation. `ScoringPipeline` garde le démenti
    en état par appel et le réinjecte ici — un démenti est un fait sur l'appel,
    pas sur le numéro.
    """


@dataclass(slots=True)
class SignalSet:
    """Dernier état connu des quatre signaux pour un appel.

    `None` = le signal n'a pas encore parlé. Le scoring compte alors 0 pour lui
    plutôt que de renormaliser : une absence de preuve ne doit pas gonfler le score.
    """

    content: ContentSignal | None = None
    keywords: KeywordSignal | None = None
    voice: VoiceSignal | None = None
    reputation: ReputationSignal | None = None


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    score: int
    level: AlertLevel
    reasons: tuple[str, ...] = ()
    breakers_fired: tuple[str, ...] = ()
    explanation: str = ""
