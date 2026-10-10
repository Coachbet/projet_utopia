"""État d'un appel en cours.

Un `CallSession` par appel. Il garde le dernier état connu des quatre signaux et
ce qui a déjà été fait, parce que les actions ne doivent pas se répéter : la
victime n'a pas besoin d'entendre trois fois la même annonce.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from vocalguard.domain.alerts import AlertLevel
from vocalguard.domain.events import Event, EventType, Speaker
from vocalguard.domain.models import RiskAssessment, SignalSet, TranscriptSegment


@dataclass(slots=True)
class CallSession:
    call_id: str
    caller_number: str = ""
    clock: Callable[[], float] = time.monotonic
    started_at: float = field(default=0.0)

    turns: list[TranscriptSegment] = field(default_factory=list)
    signals: SignalSet = field(default_factory=SignalSet)
    last_assessment: RiskAssessment | None = None

    # Ce qui a déjà été déclenché. `acted_level` ne redescend jamais : on
    # n'annonce pas deux fois le même niveau si le score oscille autour du seuil.
    acted_level: AlertLevel = AlertLevel.NONE
    caller_on_hold: bool = False
    sms_sent: bool = False

    # Cadence de l'analyse de contenu : un seul appel Claude à la fois.
    llm_in_flight: bool = False
    claimed_identity: str | None = None

    def __post_init__(self) -> None:
        if not self.started_at:
            self.started_at = self.clock()

    def elapsed(self) -> float:
        return max(0.0, self.clock() - self.started_at)

    def add_turn(self, segment: TranscriptSegment) -> None:
        self.turns.append(segment)

    def final_turns(self) -> Sequence[TranscriptSegment]:
        return [t for t in self.turns if t.is_final]

    def event(
        self,
        type: EventType,
        data: dict[str, Any],
        *,
        speaker: Speaker | None = None,
        t: float | None = None,
    ) -> Event:
        return Event(
            call_id=self.call_id,
            t=self.elapsed() if t is None else t,
            type=type,
            speaker=speaker,
            data=data,
        )


class SessionRegistry:
    """Les sessions vivantes, indexées par `call_id`."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._sessions: dict[str, CallSession] = {}

    def get_or_create(self, call_id: str, *, caller_number: str = "") -> CallSession:
        session = self._sessions.get(call_id)
        if session is None:
            session = CallSession(call_id=call_id, caller_number=caller_number, clock=self._clock)
            self._sessions[call_id] = session
        elif caller_number and not session.caller_number:
            session.caller_number = caller_number
        return session

    def get(self, call_id: str) -> CallSession | None:
        return self._sessions.get(call_id)

    def drop(self, call_id: str) -> CallSession | None:
        return self._sessions.pop(call_id, None)

    def __len__(self) -> int:
        return len(self._sessions)
