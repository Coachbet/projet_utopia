"""Doublure du port `CallController` : les actions sont tracées, pas exécutées.

Permet de dérouler une intervention complète (attente, annonce, SMS) sans compte
Twilio ni crédit, et de vérifier dans un test que le scammeur est bien mis en
attente *avant* l'annonce. Pas de `hangup` : le port n'en expose pas, c'est un
choix produit — un faux positif sur un vrai appel d'urgence serait grave.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class LoggingCallController:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def hold_caller(self, call_id: str) -> bool:
        return self._record("hold_caller", call_id)

    async def resume_caller(self, call_id: str) -> bool:
        return self._record("resume_caller", call_id)

    async def announce_to_victim(self, call_id: str, message: str) -> bool:
        return self._record("announce_to_victim", call_id, message)

    async def send_sms(self, to_number: str, message: str) -> bool:
        return self._record("send_sms", to_number, message)

    def _record(self, method: str, *args: str) -> bool:
        entry = (method, *args)
        self.calls.append(entry)
        logger.info("[twilio factice] %s", " ".join(entry))
        return True
