"""Doublures des signaux de Dev 2 : tout le pipeline sans AWS ni réseau.

C'est ce qui permet à Dev 2 et Biz 1 d'avancer pendant que la téléphonie se
monte, et de rejouer un scénario écrit (`make replay`) de façon déterministe.

Les doublures de Dev 1 vivent à côté : `fakes/event_sink.py` (dont
`InMemoryEventSink` et `FanOutSink`), `fakes/transcriber.py` et
`fakes/call_controller.py`. Rien n'est dupliqué ici.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from vocalguard.domain.events import Speaker
from vocalguard.domain.models import (
    Contact,
    ContentSignal,
    LineType,
    ReputationSignal,
    TranscriptSegment,
    VoiceSignal,
)

# Marqueurs du scénario d'arnaque. Volontairement grossiers : la doublure sert à
# vérifier le câblage du pipeline, pas à remplacer le jugement de Claude.
_TACTIC_PATTERNS: tuple[tuple[str, str], ...] = (
    ("urgence", r"urgen|tout de suite|vite|immédiat|immediat|maintenant|dépêche|depeche"),
    ("secret", r"ne dis rien|ne le dis|entre nous|secret|surtout pas"),
    (
        "paiement inhabituel",
        r"virement|paiement|payer|carte cadeau|crypto|bitcoin|coursier|mandat|iban|rib",
    ),
    ("fausse autorité", r"police|gendarmerie|banque|impôts|impots|huissier|technicien"),
    (
        "identité revendiquée",
        r"c'est moi|c'est ton|c'est votre|mamie|papi|petit[- ]fils|ton fils|ta fille",
    ),
    ("pression émotionnelle", r"accident|hôpital|hopital|prison|problème|probleme|peur|pleure"),
)

_IDENTITY_RE = re.compile(
    # `(?i:...)` seulement sur l'amorce : la majuscule du prénom est justement ce
    # qui le distingue d'un mot courant, donc la capture reste sensible à la casse.
    # Sans ça, « C'est Lucas » en début de phrase n'était pas reconnu et le
    # coupe-circuit paiement + identité ne s'armait jamais en mode hors ligne.
    # U+2019 est l'apostrophe typographique, que la reconnaissance vocale
    # produit aussi souvent que l'apostrophe droite.
    r"(?i:c['\u2019]est)\s+(?:moi\s*,?\s*)?([A-ZÉÈÀÂÎÔÛÇ][\wéèàâîôûç'\u2019-]+)",
    re.UNICODE,
)


class FakeContentAnalyzer:
    """Heuristique à la place de Claude : déterministe, instantanée, gratuite.

    Produit la même forme de signal que Bedrock, avec un vocabulaire de
    tactiques compatible avec les coupe-circuits (un paiement détecté donne bien
    une tactique contenant « paiement »).
    """

    def __init__(self, *, risk_per_tactic: float = 0.22, max_risk: float = 0.95) -> None:
        self._per_tactic = risk_per_tactic
        self._max = max_risk

    async def analyze(self, window: Sequence[TranscriptSegment]) -> ContentSignal | None:
        caller_text = " ".join(t.text for t in window if t.is_final and t.speaker == "caller")
        if not caller_text.strip():
            return None

        haystack = caller_text.casefold()
        tactics = tuple(name for name, pattern in _TACTIC_PATTERNS if re.search(pattern, haystack))
        risk = min(self._max, len(tactics) * self._per_tactic)

        match = _IDENTITY_RE.search(caller_text)
        claimed = match.group(1) if match else None
        if claimed is None and re.search(r"mamie|papi", haystack):
            claimed = "un proche"

        explanation = (
            "L'appelant cumule " + ", ".join(tactics) + "."
            if tactics
            else "Rien de suspect dans le contenu."
        )
        return ContentSignal(
            risk=risk, tactics=tactics, claimed_identity=claimed, explanation=explanation
        )


class FakeVoiceDetector:
    """Renvoie un risque fixe une fois assez de fenêtres accumulées."""

    def __init__(self, *, risk: float = 0.7, windows_before_signal: int = 1) -> None:
        self._risk = risk
        self._needed = windows_before_signal
        self._windows = 0

    async def feed(self, speaker: Speaker, pcm16: bytes, sample_rate: int) -> VoiceSignal | None:
        # Seul l'appelant est suspecté : la victime n'est pas sous surveillance.
        if speaker != "caller":
            return None
        self._windows += 1
        if self._windows < self._needed:
            return None
        return VoiceSignal(risk=self._risk, windows_analyzed=self._windows)


class FakeReputationChecker:
    """Réputation scriptée — utile pour forcer un `identity_mismatch` en démo."""

    def __init__(
        self,
        *,
        risk: float = 0.6,
        line_type: LineType = LineType.NON_FIXED_VOIP,
        in_contacts: bool = False,
        identity_mismatch: bool = False,
        reasons: tuple[str, ...] = ("numéro inconnu",),
    ) -> None:
        self._signal = ReputationSignal(
            risk=risk,
            line_type=line_type,
            in_contacts=in_contacts,
            identity_mismatch=identity_mismatch,
            reasons=reasons,
        )

    async def check(
        self, phone_number: str, *, claimed_identity: str | None = None
    ) -> ReputationSignal:
        # Le mismatch ne peut exister que si une identité est revendiquée : sans
        # identité, il n'y a rien à contredire.
        if claimed_identity is None and self._signal.identity_mismatch:
            return ReputationSignal(
                risk=self._signal.risk,
                line_type=self._signal.line_type,
                in_contacts=self._signal.in_contacts,
                identity_mismatch=False,
                reasons=self._signal.reasons,
            )
        return self._signal


class StaticContactBook:
    """Carnet d'adresses en dur, sans fichier."""

    def __init__(self, contacts: Sequence[Contact] = ()) -> None:
        self._contacts = tuple(contacts)

    def by_number(self, phone_number: str) -> Contact | None:
        digits = re.sub(r"\D", "", phone_number)[-9:]
        for c in self._contacts:
            if digits and re.sub(r"\D", "", c.phone_number).endswith(digits):
                return c
        return None

    def by_identity(self, claimed_identity: str) -> Contact | None:
        matches = [c for c in self._contacts if c.matches_identity(claimed_identity)]
        return max(matches, key=lambda c: len(c.name)) if matches else None

    def all(self) -> Sequence[Contact]:
        return self._contacts
