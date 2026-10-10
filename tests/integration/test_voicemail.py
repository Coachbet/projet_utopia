"""Dépôt, lecture et suppression réels du stockage, avec fournisseur audio simulé."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vocalguard.application.voicemail import VoicemailService
from vocalguard.config import Settings
from vocalguard.domain.history import utc_iso
from vocalguard.infrastructure.contacts.book import JsonContactBook
from vocalguard.infrastructure.history.sqlite import SQLiteCallHistory
from vocalguard.infrastructure.history.voicemail import SQLiteVoicemailStore
from vocalguard.infrastructure.telephony.recordings import RecordingUnavailable
from vocalguard.interface.http.voicemail import router

SID = "RE" + "a" * 32
SID2 = "RE" + "b" * 32
PIN = "432198"
AUDIO = b"FAKE_MP3_AUDIO_PRIVATE_5910"


@pytest.fixture
async def mailbox(tmp_path: Path):
    store = SQLiteVoicemailStore(tmp_path / "voicemail.sqlite")
    recordings = SimpleNamespace(audio=AsyncMock(return_value=AUDIO), delete=AsyncMock())
    service = VoicemailService(store=store, recordings=recordings, contacts=JsonContactBook())
    await service.initialize()
    return service


async def deposit(service, call_id="CA_test", sid=SID):
    assert await service.expect(call_id, "+33700000000")
    assert await service.capture(call_id, sid, 24)
    return (await service.messages())["items"][0]


async def test_deposit_requires_expected_call_and_survives_restart_without_audio_in_sqlite(mailbox):
    assert not await mailbox.capture("CA_never_expected", SID, 24)
    row = await deposit(mailbox)
    assert await mailbox.capture("CA_test", SID, 24)  # Twilio réessaie le callback.
    assert not await mailbox.capture("CA_test", SID2, 24)
    await mailbox.expect("CA_other")
    assert not await mailbox.capture("CA_other", SID, 24)
    second = VoicemailService(
        store=SQLiteVoicemailStore(mailbox.store.path),
        recordings=mailbox.recordings,
        contacts=JsonContactBook(),
    )
    await second.initialize()
    assert (await second.messages())["total"] == 1
    assert await second.audio(row["id"]) == AUDIO
    assert AUDIO not in mailbox.store.path.read_bytes()
    assert "recording_sid" not in row
    assert "url" not in row


async def test_delete_removes_provider_audio_and_prevents_late_callback_resurrection(mailbox):
    row = await deposit(mailbox)
    assert await mailbox.mark_heard(row["id"])
    assert (await mailbox.messages())["items"][0]["listened"] is True
    assert await mailbox.delete(row["id"])
    mailbox.recordings.delete.assert_awaited_once_with(SID)
    assert await mailbox.audio(row["id"]) is None
    assert not await mailbox.capture("CA_test", SID, 24)
    assert (await mailbox.messages())["items"] == []


async def test_provider_failure_never_reports_successful_deletion(mailbox):
    row = await deposit(mailbox)
    mailbox.recordings.delete.side_effect = RecordingUnavailable("down")
    with pytest.raises(RecordingUnavailable):
        await mailbox.delete(row["id"])
    assert (await mailbox.messages())["total"] == 1
    mailbox.recordings.delete.side_effect = None
    assert await mailbox.delete(row["id"])


async def test_expired_audio_hidden_and_remote_purge_retried_after_failure(mailbox):
    row = await deposit(mailbox)
    with sqlite3.connect(mailbox.store.path) as db:
        db.execute(
            "UPDATE voicemail SET created_at=?", (utc_iso(datetime.now(UTC) - timedelta(days=31)),)
        )
    assert (await mailbox.messages())["total"] == 0
    assert await mailbox.audio(row["id"]) is None
    mailbox.recordings.delete.side_effect = RecordingUnavailable("down")
    await mailbox.purge()
    assert len(await mailbox.store.expired()) == 1
    mailbox.recordings.delete.side_effect = None
    await mailbox.purge()
    assert await mailbox.store.expired() == []
    assert mailbox.recordings.delete.await_count == 2


@pytest.mark.parametrize(
    "sid,duration", [("https://evil.invalid/audio", 24), (SID, -1), (SID, 121)]
)
async def test_invalid_recording_not_stored(mailbox, sid, duration):
    await mailbox.expect("CA_test")
    assert not await mailbox.capture("CA_test", sid, duration)
    assert (await mailbox.messages())["items"] == []


async def test_routes_require_pin_before_audio_access_and_expose_no_provider_identifier(mailbox):
    row = await deposit(mailbox)
    app = FastAPI()
    app.include_router(router)
    app.state.settings = Settings(_env_file=None, app_pin=PIN)
    app.state.voicemail = mailbox
    with TestClient(app) as client:
        for suffix in ("audio", "heard", "delete"):
            response = client.post(f"/app/voicemail/{row['id']}/{suffix}", json={"pin": "wrong"})
            assert response.status_code == 403
        mailbox.recordings.audio.assert_not_awaited()
        mailbox.recordings.delete.assert_not_awaited()
        response = client.post("/app/voicemail", json={"pin": PIN})
        assert response.status_code == 200
        assert SID not in response.text
        response = client.post(f"/app/voicemail/{row['id']}/audio", json={"pin": PIN})
        assert response.content == AUDIO
        assert response.headers["content-type"] == "audio/mpeg"
        assert response.headers["cache-control"] == "no-store"
        assert client.post("/app/voicemail/unknown/audio", json={"pin": PIN}).status_code == 404
        assert client.post(f"/app/voicemail/{row['id']}/delete", json={"pin": PIN}).json() == {
            "ok": True
        }
        assert (
            client.post(f"/app/voicemail/{row['id']}/audio", json={"pin": PIN}).status_code == 404
        )


async def test_disabled_mailbox_never_creates_file_or_offers_recording(tmp_path):
    path = tmp_path / "disabled.sqlite"
    service = VoicemailService(
        store=SQLiteVoicemailStore(path),
        recordings=SimpleNamespace(),
        contacts=JsonContactBook(),
        enabled=False,
    )
    await service.initialize()
    assert not await service.expect("CA_disabled")
    assert not await service.capture("CA_disabled", SID, 24)
    assert not (await service.messages())["enabled"]
    assert not path.exists()


async def test_history_database_migrates_old_incoming_calls_before_saving_outgoing(tmp_path):
    from vocalguard.domain.history import CallRecord

    path = tmp_path / "old.sqlite"
    store = SQLiteCallHistory(path)
    await store.initialize()
    await store.save(CallRecord("CA_old", utc_iso()))
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE calls DROP COLUMN direction")
    await store.initialize()
    assert (await store.get_call("CA_old")).direction == "incoming"
    await store.save(CallRecord("CA_new", utc_iso(), direction="outgoing"))
    assert (await store.get_call("CA_new")).direction == "outgoing"
