"""Routes du dashboard : la page et son flux d'événements.

Monté par `create_app` via `_mount_dashboard`, qui attend précisément ce chemin
et un attribut `router` au niveau du module — d'où le `HUB` module, et pas une
fabrique seule.

Le `HUB` est la destination des événements : l'appelant le compose dans le sink
pour que le pipeline l'alimente.

    from vocalguard.interface.ws.dashboard import HUB
    sink = FanOutSink(ConsoleEventSink(), HUB, intervention, pipeline)

`create_dashboard_router` reste disponible pour les tests, qui ont besoin d'un
hub isolé plutôt que du singleton.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from vocalguard.interface.auth import require_pin
from vocalguard.interface.event_hub import EventHub

logger = logging.getLogger(__name__)

# `interface/ws/dashboard.py` -> `interface/static/dashboard.html`
DASHBOARD_HTML = Path(__file__).parent.parent / "static" / "dashboard.html"


def create_dashboard_router(hub: EventHub) -> APIRouter:
    router = APIRouter(tags=["dashboard"])

    @router.get("/dashboard", include_in_schema=False)
    async def page() -> FileResponse:
        # Page autonome, sans CDN : le réseau du lieu peut bloquer l'extérieur.
        return FileResponse(DASHBOARD_HTML, media_type="text/html")

    @router.websocket("/ws/dashboard")
    async def feed(websocket: WebSocket) -> None:
        try:
            require_pin(websocket.app.state, websocket.query_params.get("pin", ""))
        except HTTPException:
            await websocket.close(code=1008)
            return
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


HUB = EventHub()
"""Destination par défaut des événements, alimentée par le sink de `create_app`."""

router = create_dashboard_router(HUB)
