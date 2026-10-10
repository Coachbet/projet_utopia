"""Poids des quatre signaux.

Point de départ à calibrer sur les scénarios de test de Biz 1 — notamment les
deux appels légitimes, qui doivent rester sous 40.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Weights:
    llm: int = 45
    keywords: int = 20
    voice: int = 20
    reputation: int = 15

    @property
    def total(self) -> int:
        return self.llm + self.keywords + self.voice + self.reputation


DEFAULT_WEIGHTS = Weights()

VOICE_ALONE_CAP = 64
"""La voix synthétique seule ne fait jamais dépasser 64 : elle peut aggraver,
pas déclencher une intervention. Limite les faux positifs."""

VOICE_ALONE_MARGIN = 15
"""En dessous de ce nombre de points hors-voix, on considère que la voix est le
seul signal qui parle, et le plafond ci-dessus s'applique."""
