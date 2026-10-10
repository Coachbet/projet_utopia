"""Routes du dashboard : la page et son flux d'événements.

Exposé comme un `APIRouter` et non comme une application : Dev 1 possède
`interface/app.py` (webhooks TwiML, WebSocket média) et n'a qu'à inclure ce
routeur.

    from vocalguard.interface.dashboard import create_dashboard_router
    from vocalguard.interface.event_hub import EventHub

    hub = EventHub()                      # à passer aussi au ScoringPipeline
    app.include_router(create_dashboard_router(hub))
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from vocalguard.interface.event_hub import EventHub

logger = logging.getLogger(__name__)

DASHBOARD_HTML = Path(__file__).parent / "static" / "dashboard.html"


def create_dashboard_router(hub: EventHub) -> APIRouter:
    router = APIRouter(tags=["dashboard"])

    @router.get("/dashboard", include_in_schema=False)
    async def page() -> FileResponse:
        # Page autonome, sans CDN : le réseau du lieu peut bloquer l'extérieur.
        return FileResponse(DASHBOARD_HTML, media_type="text/html")

    @router.websocket("/ws/dashboard")
    async def feed(websocket: WebSocket) -> None:
        await websocket.accept()

        async def send(payload: dict) -> None:
            await websocket.send_json(payload)

        # Un dashboard ouvert en cours d'appel rattrape ce qu'il a manqué.
        history = tuple(hub.replay())
        if history:
            await websocket.send_json({"type": "__replay__", "events": list(history)})

        await hub.subscribe(send)
        try:
            while True:
                # Rien à lire, mais cet await est ce qui détecte la fermeture.
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.debug("dashboard déconnecté", exc_info=True)
        finally:
            await hub.unsubscribe(send)

    return router
