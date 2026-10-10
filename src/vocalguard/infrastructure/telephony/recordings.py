"""Lecture/suppression de messages vocaux auprès de Twilio, sans URL du client."""

from __future__ import annotations

import re

import httpx

from vocalguard.config import Settings


class RecordingUnavailable(Exception):
    pass


class TwilioRecordings:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.account = settings.twilio_account_sid
        self.username = settings.twilio_api_key_sid or settings.twilio_account_sid
        self.password = settings.twilio_api_key_secret or settings.twilio_auth_token
        self.transport = transport

    def _url(self, sid: str, extension: str) -> str:
        if not re.fullmatch(r"RE[0-9a-fA-F]{32}", sid):
            raise RecordingUnavailable("Identifiant de message invalide")
        return (
            f"https://api.twilio.com/2010-04-01/Accounts/{self.account}/Recordings/{sid}{extension}"
        )

    async def audio(self, sid: str) -> bytes:
        try:
            async with httpx.AsyncClient(
                auth=(self.username, self.password),
                timeout=15,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                async with client.stream("GET", self._url(sid, ".mp3")) as response:
                    response.raise_for_status()
                    audio = bytearray()
                    async for chunk in response.aiter_bytes():
                        audio.extend(chunk)
                        if len(audio) > 2_000_000:
                            raise RecordingUnavailable("Message trop volumineux")
            if not audio:
                raise RecordingUnavailable("Message audio vide")
            return bytes(audio)
        except httpx.HTTPError:
            raise RecordingUnavailable("Le message ne peut pas être lu actuellement") from None

    async def delete(self, sid: str) -> None:
        try:
            async with httpx.AsyncClient(
                auth=(self.username, self.password),
                timeout=10,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                response = await client.delete(self._url(sid, ".json"))
                if response.status_code != 404:
                    response.raise_for_status()
        except httpx.HTTPError:
            raise RecordingUnavailable("Suppression du message indisponible") from None
