"""Le hub de diffusion : un dashboard qui tombe ne doit pas gêner l'appel."""

from __future__ import annotations

from vocalguard.domain.events import Event, EventType
from vocalguard.interface.event_hub import EventHub


def ev(score: int = 50, t: float = 1.0) -> Event:
    return Event(call_id="CA1", t=t, type=EventType.SCORE, data={"score": score, "level": "watch"})


async def test_publish_without_subscribers_is_fine():
    hub = EventHub()
    await hub.emit(ev())
    assert len(tuple(hub.replay())) == 1


async def test_subscriber_receives_wire_payload():
    hub = EventHub()
    received: list[dict] = []

    async def sub(payload: dict) -> None:
        received.append(payload)

    await hub.subscribe(sub)
    await hub.emit(ev(score=72))

    assert len(received) == 1
    assert received[0]["data"]["score"] == 72
    assert received[0]["call_id"] == "CA1"
    # Les champs internes ne partent pas sur le fil.
    assert "event_id" not in received[0]
    assert "wall_clock" not in received[0]


async def test_unsubscribe_stops_delivery():
    hub = EventHub()
    received: list[dict] = []

    async def sub(payload: dict) -> None:
        received.append(payload)

    await hub.subscribe(sub)
    await hub.emit(ev())
    await hub.unsubscribe(sub)
    await hub.emit(ev())

    assert len(received) == 1
    assert hub.subscriber_count == 0


async def test_a_broken_subscriber_is_dropped_and_others_still_get_events():
    """Un onglet fermé brutalement ne doit pas interrompre la protection."""
    hub = EventHub()
    good: list[dict] = []

    async def healthy(payload: dict) -> None:
        good.append(payload)

    async def broken(payload: dict) -> None:
        raise RuntimeError("socket fermée")

    await hub.subscribe(healthy)
    await hub.subscribe(broken)

    await hub.emit(ev(score=10))
    await hub.emit(ev(score=20))

    assert [p["data"]["score"] for p in good] == [10, 20]
    assert hub.subscriber_count == 1


async def test_history_is_bounded_and_ordered():
    hub = EventHub(history=3)
    for i in range(6):
        await hub.emit(ev(score=i, t=float(i)))

    replayed = tuple(hub.replay())
    assert [p["data"]["score"] for p in replayed] == [3, 4, 5]


async def test_clear_empties_history_between_calls():
    hub = EventHub()
    await hub.emit(ev())
    hub.clear()
    assert tuple(hub.replay()) == ()


async def test_late_subscriber_can_replay_what_it_missed():
    hub = EventHub()
    await hub.emit(ev(score=40))
    await hub.emit(ev(score=90))

    # C'est le cas réel : on ouvre le dashboard pendant l'appel.
    assert [p["data"]["score"] for p in hub.replay()] == [40, 90]
