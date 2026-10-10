"""Le dashboard vu depuis un vrai client WebSocket.

Tout se passe dans la boucle de l'application (routes `_emit` de test) : le hub
porte un `asyncio.Lock`, qui ne supporte pas d'être utilisé depuis deux boucles.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vocalguard.domain.events import Event, EventType
from vocalguard.interface.dashboard import DASHBOARD_HTML, create_dashboard_router
from vocalguard.interface.event_hub import EventHub


@pytest.fixture
def app_and_hub() -> tuple[FastAPI, EventHub]:
    hub = EventHub()
    app = FastAPI()
    app.include_router(create_dashboard_router(hub))

    @app.post("/_emit/{score}")
    async def emit(score: int) -> dict[str, bool]:
        await hub.publish(
            Event(
                call_id="CA1",
                t=float(score),
                type=EventType.SCORE,
                data={"score": score, "level": "alert", "reasons": ["urgence"]},
            )
        )
        return {"ok": True}

    return app, hub


def test_live_event_reaches_a_connected_dashboard(app_and_hub):
    app, _ = app_and_hub
    with TestClient(app) as client, client.websocket_connect("/ws/dashboard") as ws:
        client.post("/_emit/72")
        payload = ws.receive_json()

    assert payload["type"] == "score"
    assert payload["data"]["score"] == 72
    assert payload["data"]["reasons"] == ["urgence"]


def test_dashboard_opened_late_receives_a_replay_frame(app_and_hub):
    """Cas réel de démo : on ouvre l'écran une fois l'appel commencé."""
    app, _ = app_and_hub
    with TestClient(app) as client:
        client.post("/_emit/40")
        client.post("/_emit/90")

        with client.websocket_connect("/ws/dashboard") as ws:
            frame = ws.receive_json()

    assert frame["type"] == "__replay__"
    assert [e["data"]["score"] for e in frame["events"]] == [40, 90]


def test_no_replay_frame_when_there_is_no_history(app_and_hub):
    app, _ = app_and_hub
    with TestClient(app) as client, client.websocket_connect("/ws/dashboard") as ws:
        client.post("/_emit/55")
        # Première trame = l'événement live, pas un rejeu vide.
        assert ws.receive_json()["type"] == "score"


def test_disconnecting_unsubscribes(app_and_hub):
    app, hub = app_and_hub
    with TestClient(app) as client:
        with client.websocket_connect("/ws/dashboard") as ws:
            client.post("/_emit/10")
            ws.receive_json()
        client.post("/_emit/20")
        assert hub.subscriber_count == 0


def test_two_dashboards_both_receive_events(app_and_hub):
    app, _ = app_and_hub
    with TestClient(app) as client:
        with (
            client.websocket_connect("/ws/dashboard") as a,
            client.websocket_connect("/ws/dashboard") as b,
        ):
            client.post("/_emit/88")
            assert a.receive_json()["data"]["score"] == 88
            assert b.receive_json()["data"]["score"] == 88


def test_dashboard_page_is_served():
    assert DASHBOARD_HTML.exists(), "la page du dashboard est absente"
    hub = EventHub()
    app = FastAPI()
    app.include_router(create_dashboard_router(hub))
    with TestClient(app) as client:
        response = client.get("/dashboard")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
