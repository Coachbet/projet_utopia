"""Métadonnées de messagerie vocale ; les fichiers audio restent chez Twilio."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar
from uuid import uuid4

from vocalguard.domain.history import utc_iso

T = TypeVar("T")


class SQLiteVoicemailStore:
    def __init__(self, path: Path | str, *, retention_days: int = 30) -> None:
        self.path = Path(path)
        self.retention_days = retention_days
        self._lock = threading.RLock()

    async def _run(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        def run() -> T:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                db = sqlite3.connect(self.path, timeout=5)
                try:
                    self.path.chmod(0o600)
                    db.row_factory = sqlite3.Row
                    db.execute("PRAGMA secure_delete=ON")
                    with db:
                        return operation(db)
                finally:
                    db.close()

        return await asyncio.to_thread(run)

    async def initialize(self) -> None:
        def create(db: sqlite3.Connection) -> None:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS voicemail ("
                "call_id TEXT PRIMARY KEY, id TEXT NOT NULL UNIQUE, "
                "recording_sid TEXT UNIQUE, caller_number TEXT NOT NULL, "
                "caller_name TEXT NOT NULL, created_at TEXT NOT NULL, "
                "duration_s INTEGER NOT NULL DEFAULT 0, listened INTEGER NOT NULL DEFAULT 0, "
                "deleted INTEGER NOT NULL DEFAULT 0)"
            )

        await self._run(create)

    async def expect(self, call_id: str, number: str, name: str) -> None:
        await self._run(
            lambda db: db.execute(
                "INSERT OR IGNORE INTO voicemail "
                "(call_id,id,caller_number,caller_name,created_at) VALUES (?,?,?,?,?)",
                (call_id, uuid4().hex, number, name, utc_iso()),
            )
        )

    async def capture(self, call_id: str, recording_sid: str, duration_s: int) -> bool:
        def capture(db: sqlite3.Connection) -> bool:
            row = db.execute("SELECT * FROM voicemail WHERE call_id=?", (call_id,)).fetchone()
            if not row or row["deleted"]:
                return False
            if row["recording_sid"]:
                return row["recording_sid"] == recording_sid
            try:
                db.execute(
                    "UPDATE voicemail SET recording_sid=?,duration_s=?,created_at=? "
                    "WHERE call_id=?",
                    (recording_sid, duration_s, utc_iso(), call_id),
                )
            except sqlite3.IntegrityError:
                return False
            return True

        return await self._run(capture)

    def _cutoff(self) -> str:
        return utc_iso(datetime.now(UTC) - timedelta(days=self.retention_days))

    async def list_messages(self, *, offset: int = 0, limit: int = 50) -> dict[str, object]:
        def read(db: sqlite3.Connection) -> dict[str, object]:
            db.execute("BEGIN")
            where = "recording_sid IS NOT NULL AND deleted=0 AND created_at>=?"
            cutoff = self._cutoff()
            total = db.execute(
                f"SELECT count(*) FROM voicemail WHERE {where}", (cutoff,)
            ).fetchone()[0]
            rows = db.execute(
                "SELECT id,call_id,caller_number,caller_name,created_at,duration_s,listened "
                f"FROM voicemail WHERE {where} ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                (cutoff, limit, offset),
            ).fetchall()
            items = [{**dict(row), "listened": bool(row["listened"])} for row in rows]
            return {
                "items": items,
                "total": total,
                "offset": offset,
                "limit": limit,
                "has_more": offset + len(items) < total,
            }

        return await self._run(read)

    async def get(self, message_id: str) -> dict[str, object] | None:
        def read(db: sqlite3.Connection) -> dict[str, object] | None:
            row = db.execute(
                "SELECT * FROM voicemail WHERE id=? AND deleted=0 "
                "AND recording_sid IS NOT NULL AND created_at>=?",
                (message_id, self._cutoff()),
            ).fetchone()
            return dict(row) if row else None

        return await self._run(read)

    async def mark_heard(self, message_id: str) -> None:
        await self._run(
            lambda db: db.execute("UPDATE voicemail SET listened=1 WHERE id=?", (message_id,))
        )

    async def mark_deleted(self, message_id: str) -> None:
        # Le tombstone empêche un callback Twilio répété de recréer le message.
        await self._run(
            lambda db: db.execute(
                "UPDATE voicemail SET deleted=1,recording_sid=NULL,caller_number='',caller_name='' "
                "WHERE id=?",
                (message_id,),
            )
        )

    async def expired(self) -> list[dict[str, object]]:
        return await self._run(
            lambda db: [
                dict(row)
                for row in db.execute(
                    "SELECT id,recording_sid FROM voicemail "
                    "WHERE recording_sid IS NOT NULL AND (deleted=1 OR created_at<?) LIMIT 50",
                    (self._cutoff(),),
                )
            ]
        )

    async def purge_metadata(self) -> None:
        await self._run(
            lambda db: db.execute(
                "DELETE FROM voicemail WHERE recording_sid IS NULL AND created_at<?",
                (self._cutoff(),),
            )
        )
