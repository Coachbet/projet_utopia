"""Diffusion des événements vers les dashboards connectés.

`EventHub` est un `EventSink` : le pipeline lui parle sans savoir qu'il y a des
WebSockets derrière. Il garde les derniers événements en mémoire pour qu'un
dashboard ouvert en retard — ce qui arrive toujours en démo — rattrape l'appel
en cours au lieu d'afficher une page vide.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from vocalguard.domain.events import Event

logger = logging.getLogger(__name__)

Subscriber = Callable[[dict[str, Any]], Awaitable[None]]


class EventHub:
    def __init__(self, history: int = 400) -> None:
        self._subscribers: set[Subscriber] = set()
        self._history: deque[dict[str, Any]] = deque(maxlen=history)
        self._lock = asyncio.Lock()

    async def publish(self, event: Event) -> None:
        payload = event.to_wire()
        self._history.append(payload)
        await self._fanout(payload)

    async def _fanout(self, payload: dict[str, Any]) -> None:
        # Sans abonné, on ne prend même pas le verrou : c'est le cas courant
        # (rejeu de scénario, tests) et le set ne bouge qu'aux points d'await.
        if not self._subscribers:
            return
        async with self._lock:
            targets = list(self._subscribers)
        if not targets:
            return
        results = await asyncio.gather(*(s(payload) for s in targets), return_exceptions=True)
        # Un dashboard fermé brutalement ne doit pas interrompre l'appel.
        dead = [s for s, r in zip(targets, results, strict=True) if isinstance(r, Exception)]
        if dead:
            async with self._lock:
                self._subscribers.difference_update(dead)

    async def subscribe(self, subscriber: Subscriber) -> None:
        async with self._lock:
            self._subscribers.add(subscriber)

    async def unsubscribe(self, subscriber: Subscriber) -> None:
        async with self._lock:
            self._subscribers.discard(subscriber)

    def replay(self) -> Iterable[dict[str, Any]]:
        return tuple(self._history)

    def clear(self) -> None:
        self._history.clear()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
