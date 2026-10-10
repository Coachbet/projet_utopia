"""SQLite local, opérations hors boucle asyncio et écritures sérialisées."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar

from vocalguard.domain.history import CallRecord, HistoryPage, utc_iso

_T = TypeVar("_T")
_COLUMNS = tuple(field.name for field in fields(CallRecord))
_JSON_COLUMNS = {"actions", "tactics"}
_REAL_COLUMNS = {
    "duration_s",
    "watch_at_s",
    "alert_at_s",
    "intervene_at_s",
    "peak_llm",
    "peak_keywords",
    "peak_voice",
    "peak_reputation",
}


class SQLiteCallHistory:
    """Une connexion par opération : aucune connexion liée à un thread asyncio.

    `secure_delete` et DELETE journal évitent de laisser les anciennes données
    d'historique dans un WAL après un effacement. Le verrou couvre chaque
    transaction, y compris les lectures liste+compteur qui doivent être cohérentes.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        enabled: bool = True,
        retention_days: int = 30,
    ) -> None:
        if retention_days < 1:
            raise ValueError("retention_days doit être supérieur ou égal à 1")
        self.path = Path(path)
        self.enabled = enabled
        self.retention_days = retention_days
        self._lock = threading.RLock()

    async def _run(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        def execute() -> _T:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(self.path, timeout=5)
                try:
                    self.path.chmod(0o600)
                    connection.row_factory = sqlite3.Row
                    connection.execute("PRAGMA secure_delete = ON")
                    return operation(connection)
                finally:
                    connection.close()

        return await asyncio.to_thread(execute)

    async def initialize(self, *, now: datetime | None = None) -> None:
        if not self.enabled:
            return

        def create(connection: sqlite3.Connection) -> None:
            connection.execute("PRAGMA journal_mode = DELETE")
            definitions = []
            for name in _COLUMNS:
                kind = "REAL" if name in _REAL_COLUMNS else "TEXT"
                if name in {"answered", "max_score"}:
                    kind = "INTEGER"
                suffix = " PRIMARY KEY" if name == "call_id" else ""
                definitions.append(f"{name} {kind}{suffix}")
            connection.execute(f"CREATE TABLE IF NOT EXISTS calls ({','.join(definitions)})")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(calls)")}
            if "direction" not in columns:
                connection.execute(
                    "ALTER TABLE calls ADD COLUMN direction TEXT NOT NULL DEFAULT 'incoming'"
                )
            connection.execute("CREATE INDEX IF NOT EXISTS calls_started_at ON calls(started_at)")
            cutoff = utc_iso((now or datetime.now(UTC)) - timedelta(days=self.retention_days))
            connection.execute("DELETE FROM calls WHERE started_at < ?", (cutoff,))
            connection.commit()

        await self._run(create)

    async def save(self, record: CallRecord) -> None:
        if not self.enabled:
            return
        values = record.to_dict()
        for key in _JSON_COLUMNS:
            values[key] = json.dumps(values[key], ensure_ascii=False)
        values["answered"] = int(record.answered)
        parameters = tuple(values[column] for column in _COLUMNS)

        def write(connection: sqlite3.Connection) -> None:
            placeholders = ",".join("?" for _ in _COLUMNS)
            assignments = ",".join(
                f"{column}=excluded.{column}" for column in _COLUMNS if column != "call_id"
            )
            connection.execute(
                f"INSERT INTO calls ({','.join(_COLUMNS)}) VALUES ({placeholders}) "
                f"ON CONFLICT(call_id) DO UPDATE SET {assignments}",
                parameters,
            )
            connection.commit()

        await self._run(write)

    @staticmethod
    def _record(row: sqlite3.Row) -> CallRecord:
        values = dict(row)
        for key in _JSON_COLUMNS:
            values[key] = json.loads(values[key] or "[]")
        values["answered"] = bool(values["answered"])
        return CallRecord(**values)

    async def list_calls(self, *, offset: int = 0, limit: int = 50) -> HistoryPage:
        if offset < 0 or not 1 <= limit <= 50:
            raise ValueError("Pagination invalide")
        if not self.enabled:
            return HistoryPage([], 0, offset, limit)

        def read(connection: sqlite3.Connection) -> HistoryPage:
            connection.execute("BEGIN")
            total = connection.execute("SELECT count(*) FROM calls").fetchone()[0]
            rows = connection.execute(
                "SELECT * FROM calls ORDER BY started_at DESC, call_id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return HistoryPage([self._record(row) for row in rows], total, offset, limit)

        return await self._run(read)

    async def get_call(self, call_id: str) -> CallRecord | None:
        if not self.enabled:
            return None

        def read(connection: sqlite3.Connection) -> CallRecord | None:
            row = connection.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,)).fetchone()
            return self._record(row) if row else None

        return await self._run(read)

    async def clear(self) -> int:
        if not self.enabled:
            return 0

        def delete(connection: sqlite3.Connection) -> int:
            total = connection.execute("SELECT count(*) FROM calls").fetchone()[0]
            connection.execute("DELETE FROM calls")
            connection.commit()
            return total

        return await self._run(delete)
