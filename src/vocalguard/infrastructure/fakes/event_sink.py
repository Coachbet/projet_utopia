"""Doublures du port `EventSink`.

Le vrai bus d'événements est la part de Dev 2 (sa tâche 7) : ces trois classes
sont des doublures minimales, pas un bus. Elles existent pour que Dev 1 puisse
tester le pipeline et rejouer un scénario sans attendre le bus, et pour que les
assertions des tests portent sur des objets `Event` réels. À remplacer par le
bus dès qu'il est là — le port est le même, il n'y aura rien à changer ailleurs.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from vocalguard.domain.alerts import ReportedAction
from vocalguard.domain.events import Event, EventType

if TYPE_CHECKING:
    from vocalguard.domain.ports import EventSink

logger = logging.getLogger(__name__)


class InMemoryEventSink:
    """Garde tout en mémoire : c'est ce qui rend les tests observables."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)

    def by_type(self, type: EventType | str) -> list[Event]:
        """Accepte l'enum comme la forme wire (`"transcript"`), au choix de l'appelant."""
        wanted = EventType(type)
        return [event for event in self.events if event.type is wanted]

    def actions(self) -> list[Event]:
        """Les événements `action` de la **politique d'alerte** seulement.

        Écarte les faits de routage et de vérification
        (`domain.alerts.ReportedAction`), qui voyagent dans le même type
        d'événement parce que le contrat figé à H0 ne permet rien d'autre.

        Sans cette distinction, tout test qui ouvre son appel par
        `/twilio/voice/inbound` — c'est-à-dire tout test qui part du même état
        que la production — doit connaître la liste des faits de routage pour
        affirmer « rien d'audible n'a été déclenché ». Cette liste s'allongerait
        à chaque lot, dans chaque fichier de test, et c'est précisément le genre
        de liste parallèle que ce dépôt a déjà payé une fois. Un test qui veut
        voir les faits de routage, lui, demande toujours
        `by_type(EventType.ACTION)`.
        """
        rapportes = {fait.value for fait in ReportedAction}
        return [
            event
            for event in self.by_type(EventType.ACTION)
            if event.data.get("action") not in rapportes
        ]

    def clear(self) -> None:
        self.events.clear()


class ConsoleEventSink:
    """Une ligne par événement, pour suivre un replay à l'œil nu dans le terminal.

    `print` et pas `logging` : c'est la sortie utile d'un script de replay, elle
    ne doit pas dépendre du niveau de log configuré.
    """

    async def emit(self, event: Event) -> None:
        speaker = event.speaker or "-"
        # Les terminaux peuvent être redirigés vers un fichier. Garder la preuve
        # d'activité, sans persister des conversations ni des codes de SMS.
        if event.type is EventType.TRANSCRIPT:
            data = {"is_final": event.data.get("is_final", True)}
        else:
            allowed = ("risk", "score", "level", "action", "ok", "windows_analyzed")
            data = {key: event.data[key] for key in allowed if key in event.data}
        payload = json.dumps(data, ensure_ascii=False, default=str)
        print(f"[{event.t:6.2f}s] {event.type.value:<10} {speaker:<7} {payload}")


class FanOutSink:
    """Diffuse chaque événement vers plusieurs destinations (dashboard + console + tests).

    Un sink qui lève ne doit pas priver les autres de l'événement : pendant la
    démo, un dashboard déconnecté ne doit pas interrompre le pipeline. Diffusion
    séquentielle, pour que l'ordre des événements reste celui de l'émission.
    """

    def __init__(self, *sinks: EventSink) -> None:
        self._sinks: list[EventSink] = list(sinks)

    def add(self, sink: EventSink) -> None:
        """Abonne une destination après coup.

        Nécessaire parce que le câblage est circulaire : la session a besoin du
        sink pour émettre, et le pipeline de score a besoin de la session pour
        retrouver l'appel — donc il ne peut pas être construit avant le sink.
        """
        self._sinks.append(sink)

    async def emit(self, event: Event) -> None:
        for sink in self._sinks:
            try:
                await sink.emit(event)
            except Exception:
                logger.warning(
                    "sink %s en échec sur un événement %s, on continue",
                    type(sink).__name__,
                    event.type.value,
                    exc_info=True,
                )
