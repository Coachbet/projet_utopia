"""Contrat d'événement commun — figé à H0.

Toute brique (téléphonie, signaux, score, dashboard) n'échange que des `Event`.
Changer ce fichier casse simultanément Dev 1, Dev 2 et le dashboard : à valider
en équipe avant. Le schéma de référence est dans `contracts/event.schema.json`.
"""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class EventType(StrEnum):
    TRANSCRIPT = "transcript"
    KEYWORD = "keyword"
    LLM = "llm"
    VOICE = "voice"
    REPUTATION = "reputation"
    SCORE = "score"
    ACTION = "action"


Speaker = Literal["caller", "victim"]
"""`caller` = l'appelant (scammeur présumé) ; `victim` = la personne appelée.

Mappe sur les pistes Twilio : track `inbound` -> caller, `outbound` -> victim.
C'est cette séparation des pistes qui nous dit qui parle.
"""


class Event(BaseModel):
    """L'unité d'échange unique du système."""

    call_id: str
    t: float = Field(ge=0, description="Secondes depuis le début de l'appel")
    type: EventType
    speaker: Speaker | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    # Hors contrat : utile en interne, jamais sérialisé sur le fil.
    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12], exclude=True)
    wall_clock: float = Field(default_factory=time.time, exclude=True)

    def to_wire(self) -> dict[str, Any]:
        """Forme JSON envoyée sur le WebSocket du dashboard."""
        return self.model_dump(mode="json")
