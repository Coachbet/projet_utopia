"""Doublures : tout le pipeline sans téléphone, sans AWS, sans réseau.

C'est ce qui permet à Dev 2 et Biz 1 d'avancer pendant que la téléphonie se
monte, et de rejouer un scénario écrit (`make replay`) de façon déterministe.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Sequence

from vocalguard.domain.events import Event, Speaker
from vocalguard.domain.models import (
    Contact,
    ContentSignal,
    LineType,
    ReputationSignal,
    TranscriptSegment,
    VoiceSignal,
)

logger = logging.getLogger(__name__)


class InMemoryEventSink:
    """Garde tout en mémoire, et peut relayer vers un abonné (le dashboard)."""

    def __init__(self, relay: Callable[[Event], Awaitable[None]] | None = None) -> None:
        self.events: list[Event] = []
        self._relay = relay

    async def publish(self, event: Event) -> None:
        self.events.append(event)
        if self._relay is not None:
            await self._relay(event)

    def of_type(self, type_: str) -> list[Event]:
        return [e for e in self.events if e.type.value == type_]

    @property
    def last_score(self) -> int | None:
        scores = self.of_type("score")
        return scores[-1].data.get("score") if scores else None

    @property
    def last_level(self) -> str | None:
        scores = self.of_type("score")
        return scores[-1].data.get("level") if scores else None


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

_IDENTITY_RE = re.compile(r"c'est\s+(?:moi\s*,?\s*)?([A-ZÉÈÀÂÎÔÛÇ][\wéèàâîôûç'-]+)", re.UNICODE)


class FakeContentAnalyzer:
    """Heuristique à la place de Claude : déterministe, instantanée, gratuite.

    Produit la même forme de signal que Bedrock, avec un vocabulaire de
    tactiques compatible avec les coupe-circuits (un paiement détecté donne bien
    une tactique contenant « paiement »).
    """

    def __init__(self, *, risk_per_tactic: float = 0.22, max_risk: float = 0.95) -> None:
        self._per_tactic = risk_per_tactic
        self._max = max_risk

    async def analyze(self, turns: Sequence[TranscriptSegment]) -> ContentSignal | None:
        caller_text = " ".join(t.text for t in turns if t.is_final and t.speaker == "caller")
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
        self, phone_number: str, claimed_identity: str | None = None
    ) -> ReputationSignal:
        # Le mismatch ne peut exister que si une identité est revendiquée.
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


class RecordingCallController:
    """Enregistre les actions au lieu de les exécuter, et les journalise."""

    def __init__(self, *, succeed: bool = True) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._succeed = succeed

    def _record(self, kind: str, target: str, message: str = "") -> bool:
        self.calls.append((kind, target, message))
        logger.info("[action] %s -> %s %s", kind, target, message)
        return self._succeed

    async def announce(self, target: str, message: str) -> bool:
        return self._record("announce", target, message)

    async def hold(self, target: str) -> bool:
        return self._record("hold", target)

    async def resume(self, target: str) -> bool:
        return self._record("resume", target)

    async def send_sms(self, to: str, message: str) -> bool:
        return self._record("sms", to, message)
