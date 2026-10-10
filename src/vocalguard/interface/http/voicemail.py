"""Messagerie vocale privée : le navigateur ne reçoit aucune URL ni clé Twilio."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException, Request, Response

from vocalguard.infrastructure.telephony.recordings import RecordingUnavailable
from vocalguard.interface.auth import require_pin
from vocalguard.interface.http.phone_app import DemandeApp, DemandeHistorique

router = APIRouter(tags=["messagerie"])


def _service(request: Request, pin: str):
    require_pin(request.app.state, pin)
    service = getattr(request.app.state, "voicemail", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Messagerie indisponible.")
    return service


@router.post("/app/voicemail")
async def messages(demande: DemandeHistorique, request: Request) -> dict[str, object]:
    service = _service(request, demande.pin)
    try:
        return await service.messages(offset=demande.offset, limit=demande.limit)
    except (sqlite3.Error, OSError):
        raise HTTPException(status_code=503, detail="Messagerie indisponible.") from None


@router.post("/app/voicemail/{message_id}/audio")
async def audio(message_id: str, demande: DemandeApp, request: Request) -> Response:
    service = _service(request, demande.pin)
    try:
        content = await service.audio(message_id)
    except (RecordingUnavailable, sqlite3.Error, OSError):
        raise HTTPException(status_code=503, detail="Lecture momentanément indisponible.") from None
    if content is None:
        raise HTTPException(status_code=404, detail="Message absent ou supprimé.")
    return Response(content, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})


@router.post("/app/voicemail/{message_id}/heard")
async def heard(message_id: str, demande: DemandeApp, request: Request) -> dict[str, bool]:
    service = _service(request, demande.pin)
    try:
        found = await service.mark_heard(message_id)
    except (sqlite3.Error, OSError):
        raise HTTPException(status_code=503, detail="Messagerie indisponible.") from None
    if not found:
        raise HTTPException(status_code=404, detail="Message absent ou supprimé.")
    return {"ok": True}


@router.post("/app/voicemail/{message_id}/delete")
async def delete(message_id: str, demande: DemandeApp, request: Request) -> dict[str, bool]:
    service = _service(request, demande.pin)
    try:
        found = await service.delete(message_id)
    except (RecordingUnavailable, sqlite3.Error, OSError):
        raise HTTPException(
            status_code=503, detail="Suppression momentanément indisponible."
        ) from None
    if not found:
        raise HTTPException(status_code=404, detail="Message absent ou supprimé.")
    return {"ok": True}
