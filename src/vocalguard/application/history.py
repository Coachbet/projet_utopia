"""Projection blanche du bus vers des métriques, jamais des transcripts."""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections import OrderedDict
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from vocalguard.domain.alerts import ActionKind, AlertLevel, ReportedAction, Thresholds
from vocalguard.domain.events import Event, EventType
from vocalguard.domain.history import (
    CallDirection,
    CallRecord,
    HistoryRoute,
    caller_origin,
    utc_iso,
)
from vocalguard.domain.models import LineType

if TYPE_CHECKING:
    from vocalguard.application.sessions import SessionRegistry
    from vocalguard.domain.ports import CallHistory, ContactBook

logger = logging.getLogger(__name__)
_LEVELS = {level.wire: level for level in AlertLevel}
_ACTIONS = (
    {action.value for action in ActionKind}
    | {action.value for action in ReportedAction}
    | {"contact_denied"}
)
_PEAKS = {
    EventType.LLM: "peak_llm",
    EventType.KEYWORD: "peak_keywords",
    EventType.VOICE: "peak_voice",
    EventType.REPUTATION: "peak_reputation",
}
_SCORE_FIELDS = (
    "max_score",
    "max_level",
    "watch_at_s",
    "alert_at_s",
    "intervene_at_s",
    "peak_llm",
    "peak_keywords",
    "peak_voice",
    "peak_reputation",
)


def _finite_number(value: object, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or not minimum <= numeric <= maximum:
        return None
    return numeric


def _short_text(value: object, length: int) -> str:
    return " ".join(value.split())[:length] if isinstance(value, str) else ""


class HistoryRecorder:
    """Abonné au bus et points explicites de début, décroché et fin d'appel.

    L'historique est secondaire : un disque plein ne doit jamais interrompre
    l'analyse ni l'appel. Les erreurs de persistance ne quittent pas ce service.
    Une écriture suivante retransmet la projection entière et répare une perte
    transitoire. Aucun objet Event n'est gardé après `emit`.
    """

    def __init__(
        self,
        *,
        store: CallHistory,
        registry: SessionRegistry,
        contacts: ContactBook,
        thresholds: Thresholds | None = None,
    ) -> None:
        self.store = store
        self._registry = registry
        self._contacts = contacts
        self._thresholds = thresholds or Thresholds()
        self._records: dict[str, CallRecord] = {}
        self._lock = asyncio.Lock()
        # Empêche les événements tardifs et les appels effacés en cours de
        # ressusciter. Taille bornée, donc aucune fuite après de nombreux appels.
        self._closed: OrderedDict[str, None] = OrderedDict()

    def _block(self, call_id: str) -> None:
        self._closed[call_id] = None
        self._closed.move_to_end(call_id)
        while len(self._closed) > 1024:
            self._closed.popitem(last=False)

    async def _save(self, record: CallRecord) -> None:
        try:
            await self.store.save(record)
        except Exception:
            logger.exception("Écriture de l'historique indisponible pour %s", record.call_id)

    async def _start(
        self,
        call_id: str,
        caller_number: str,
        route: HistoryRoute,
        started_at: datetime | None,
        direction: CallDirection = "incoming",
    ) -> CallRecord | None:
        if call_id in self._closed:
            return None
        existing = self._records.get(call_id)
        if existing is not None:
            return existing
        try:
            stored = await self.store.get_call(call_id)
        except Exception:
            stored = None
        if stored is not None:
            if stored.ended_at:
                self._block(call_id)
                return None
            self._records[call_id] = stored
            return stored
        number = caller_number if re.fullmatch(r"\+[1-9][0-9]{6,14}", caller_number) else ""
        contact = self._contacts.by_number(number) if number else None
        origin, country = caller_origin(number, is_contact=contact is not None)
        record = CallRecord(
            call_id=call_id,
            started_at=utc_iso(started_at),
            caller_number=number,
            caller_name=_short_text(contact.name, 160) if contact else "",
            relation=_short_text(contact.relation, 160) if contact else "",
            origin=origin,
            country=country,
            route=route,
            direction=direction,
        )
        self._records[call_id] = record
        await self._save(record)
        return record

    async def start_call(
        self,
        call_id: str,
        *,
        caller_number: str = "",
        route: HistoryRoute = "analyzed",
        started_at: datetime | None = None,
        direction: CallDirection = "incoming",
    ) -> None:
        if not self.store.enabled or not call_id:
            return
        try:
            async with self._lock:
                await self._start(call_id, caller_number, route, started_at, direction)
        except Exception:
            logger.exception("Ouverture de l'historique indisponible pour %s", call_id)

    async def mark_answered(self, call_id: str) -> None:
        if not self.store.enabled:
            return
        try:
            async with self._lock:
                record = self._records.get(call_id)
                if record is not None and not record.answered:
                    record.answered = True
                    await self._save(record)
        except Exception:
            logger.exception("Mise à jour du décroché indisponible pour %s", call_id)

    async def emit(self, event: Event) -> None:
        # Test AVANT de lire data : un transcript ne traverse jamais la projection.
        if not self.store.enabled or event.type is EventType.TRANSCRIPT:
            return
        try:
            async with self._lock:
                if event.call_id in self._closed:
                    return
                record = self._records.get(event.call_id)
                if record is None:
                    session = self._registry.get(event.call_id)
                    if session is None:
                        return
                    contact = self._contacts.by_number(session.caller_number)
                    route: HistoryRoute = (
                        "trusted_passthrough" if contact and contact.is_trusted else "analyzed"
                    )
                    # Rejeu sans webhook : reconstitue le début depuis le temps
                    # relatif sans stocker d'autres données de session.
                    started = datetime.fromtimestamp(event.wall_clock - event.t, UTC)
                    record = await self._start(event.call_id, session.caller_number, route, started)
                    if record is None:
                        return
                self._project(record, event)
                await self._save(record)
        except Exception:
            logger.exception("Projection de l'historique indisponible pour %s", event.call_id)

    def _project(self, record: CallRecord, event: Event) -> None:
        data = event.data
        if event.type is EventType.ACTION:
            action = data.get("action")
            if action == "trusted_passthrough" and data.get("ok") is True:
                record.route = "trusted_passthrough"
                for name in _SCORE_FIELDS:
                    setattr(record, name, None)
                record.tactics.clear()
                record.explanation = ""
            if isinstance(action, str) and action in _ACTIONS:
                # Une action non exécutée ne doit pas apparaître comme faite.
                if data.get("ok") is True or action == "verify_expired":
                    if action not in record.actions:
                        record.actions.append(action)
                if action == "identity_verified":
                    record.verification = "verified" if data.get("ok") is True else "failed"
                elif action == "verify_expired":
                    record.verification = "expired"
                elif action == "contact_denied" and data.get("ok") is True:
                    record.verification = "failed"
            return

        # Même en mode analyse de la victime seule, un contact transmis reste
        # sans métriques de fraude dans l'historique (promesse produit).
        if record.route == "trusted_passthrough":
            return
        if event.type in _PEAKS:
            risk = _finite_number(data.get("risk"), 0, 1)
            if risk is not None:
                name = _PEAKS[event.type]
                previous = getattr(record, name)
                setattr(record, name, max(previous if previous is not None else 0, risk))
        if event.type is EventType.LLM:
            # Liste blanche : ni claimed_identity, ni `text`, ni champ inconnu.
            tactics = data.get("tactics")
            if isinstance(tactics, (list, tuple)):
                for value in tactics[:32]:
                    tactic = _short_text(value, 80)
                    if tactic and tactic not in record.tactics and len(record.tactics) < 32:
                        record.tactics.append(tactic)
            record.explanation = _short_text(data.get("explanation"), 300)
        elif event.type is EventType.REPUTATION:
            line_type = data.get("line_type")
            if line_type in {kind.value for kind in LineType}:
                record.line_type = str(line_type)
            if data.get("contact_denied") is True:
                record.verification = "failed"
                if "contact_denied" not in record.actions:
                    record.actions.append("contact_denied")
        elif event.type is EventType.SCORE:
            score = _finite_number(data.get("score"), 0, 100)
            if score is None:
                return
            record.max_score = max(record.max_score or 0, int(score))
            level = _LEVELS.get(str(data.get("level")))
            if level is None:
                level = self._thresholds.level_for(int(score))
            previous_level = _LEVELS.get(record.max_level or "none", AlertLevel.NONE)
            record.max_level = max(level, previous_level).wire
            for threshold, name in (
                (self._thresholds.watch, "watch_at_s"),
                (self._thresholds.alert, "alert_at_s"),
                (self._thresholds.intervene, "intervene_at_s"),
            ):
                if score >= threshold:
                    previous = getattr(record, name)
                    first_at = min(previous, event.t) if previous is not None else event.t
                    setattr(record, name, first_at)

    async def finalize(
        self,
        call_id: str,
        *,
        duration_s: float | None = None,
        ended_at: datetime | None = None,
    ) -> None:
        if not self.store.enabled:
            return
        try:
            async with self._lock:
                record = self._records.pop(call_id, None)
                self._block(call_id)
                if record is None:
                    return
                end = ended_at or datetime.now(UTC)
                record.ended_at = utc_iso(end)
                if duration_s is None:
                    duration_s = (end - datetime.fromisoformat(record.started_at)).total_seconds()
                record.duration_s = max(0.0, duration_s)
                await self._save(record)
        except Exception:
            logger.exception("Finalisation de l'historique indisponible pour %s", call_id)

    async def clear(self) -> int:
        """L'effacement reste effectif quand un appel est encore en cours."""
        async with self._lock:
            deleted = await self.store.clear()
            for call_id in self._records:
                self._block(call_id)
            self._records.clear()
            return deleted
