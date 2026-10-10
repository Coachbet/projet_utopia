"""File d'annonces en attente, par appel.

Pourquoi un store plutôt que le texte dans l'URL : `announce_url` n'est pas jouée
par Twilio, elle est *rappelée* — Twilio vient chercher le TwiML à cette adresse.
Mettre la phrase dans l'URL voudrait dire encoder du texte libre venu de Claude
dans un query string (longueur non bornée, échappement, phrase lisible dans les
logs Twilio). On passe donc un identifiant d'appel, et l'endpoint lit ici.
"""

from __future__ import annotations

from collections import defaultdict, deque


class AnnouncementStore:
    """Annonces en attente d'être réclamées par Twilio, FIFO et par appel.

    L'ordre compte : le score peut passer de 65 à 85 en deux secondes, donc
    l'annonce d'alerte et celle d'intervention s'enchaînent. Les jouer dans
    l'ordre inverse donnerait un message incohérent à la victime.
    """

    def __init__(self) -> None:
        self._queues: dict[str, deque[str]] = defaultdict(deque)

    def queue(self, call_id: str, message: str) -> None:
        self._queues[call_id].append(message)

    def pop(self, call_id: str) -> str | None:
        """Retire et renvoie la plus ancienne annonce, `None` s'il n'y en a plus.

        Twilio peut rappeler `announce_url` sans qu'on ait rien à dire (retry) :
        l'absence d'annonce est un cas normal, pas une erreur.
        """
        queue = self._queues.get(call_id)
        if not queue:
            return None
        return queue.popleft()

    def pending(self, call_id: str) -> tuple[str, ...]:
        """Lecture sans consommer, pour le debug et le dashboard."""
        return tuple(self._queues.get(call_id, ()))

    def clear(self, call_id: str) -> None:
        """Fin d'appel : ne pas garder une phrase qui ne sera jamais prononcée."""
        self._queues.pop(call_id, None)
