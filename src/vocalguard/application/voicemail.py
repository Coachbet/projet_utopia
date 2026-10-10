"""Messagerie explicitement enregistrée, indépendante de l'analyse sans audio conservé."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vocalguard.domain.ports import ContactBook, RecordingFiles, VoicemailStore

log = logging.getLogger(__name__)


class VoicemailService:
    def __init__(
        self,
        *,
        store: VoicemailStore,
        recordings: RecordingFiles,
        contacts: ContactBook,
        enabled: bool = True,
    ) -> None:
        self.store = store
        self.recordings = recordings
        self.contacts = contacts
        self.enabled = enabled
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self.enabled:
            await self.store.initialize()

    async def expect(self, call_id: str, caller_number: str = "") -> bool:
        if not self.enabled or not call_id:
            return False
        number = caller_number if re.fullmatch(r"\+[1-9][0-9]{6,14}", caller_number) else ""
        contact = self.contacts.by_number(number) if number else None
        try:
            await self.store.expect(call_id, number, contact.name if contact else "")
            return True
        except Exception:
            log.warning("Messagerie indisponible : dépôt non proposé")
            return False

    async def capture(self, call_id: str, recording_sid: str, duration_s: int) -> bool:
        if (
            not self.enabled
            or not re.fullmatch(r"RE[0-9a-fA-F]{32}", recording_sid)
            or isinstance(duration_s, bool)
            or not isinstance(duration_s, int)
            or not 1 <= duration_s <= 120
        ):
            return False
        async with self._lock:
            return await self.store.capture(call_id, recording_sid, duration_s)

    async def messages(self, *, offset: int = 0, limit: int = 50) -> dict[str, object]:
        if not self.enabled:
            return {
                "items": [],
                "total": 0,
                "offset": offset,
                "limit": limit,
                "has_more": False,
                "enabled": False,
            }
        return {**await self.store.list_messages(offset=offset, limit=limit), "enabled": True}

    async def audio(self, message_id: str) -> bytes | None:
        if not self.enabled:
            return None
        record = await self.store.get(message_id)
        if record is None:
            return None
        return await self.recordings.audio(str(record["recording_sid"]))

    async def mark_heard(self, message_id: str) -> bool:
        if not self.enabled or await self.store.get(message_id) is None:
            return False
        await self.store.mark_heard(message_id)
        return True

    async def delete(self, message_id: str) -> bool:
        if not self.enabled:
            return False
        async with self._lock:
            record = await self.store.get(message_id)
            if record is None:
                return False
            await self.recordings.delete(str(record["recording_sid"]))
            await self.store.mark_deleted(message_id)
            return True

    async def purge(self) -> None:
        if not self.enabled:
            return
        async with self._lock:
            for record in await self.store.expired():
                try:
                    await self.recordings.delete(str(record["recording_sid"]))
                    await self.store.mark_deleted(str(record["id"]))
                except Exception:
                    log.warning("Purge des messages vocaux différée : fournisseur indisponible")
                    break
            await self.store.purge_metadata()

    async def maintain(self) -> None:
        while True:
            try:
                await self.purge()
            except Exception:
                log.warning("Purge des messages vocaux différée : stockage indisponible")
            await asyncio.sleep(3600)
