"""Autorisations sortantes éphémères, liées au PIN et consommées une seule fois.

Le navigateur transmet uniquement un nonce à Twilio ; la destination et
l'identité autorisées restent sur le serveur. État local pour le worker unique
du MVP, aucune persistance de jeton ni de contenu d'appel.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OutgoingAuthorization:
    token: str
    identity: str
    number: str
    expires_at: float


class OutgoingAuthorizations:
    ttl_seconds = 60

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._pending: OutgoingAuthorization | None = None
        self._active_call: str | None = None
        self._active_child: str | None = None
        self._active_authorization: OutgoingAuthorization | None = None

    @property
    def busy(self) -> bool:
        return self._active_call is not None

    def issue(self, *, identity: str, number: str) -> OutgoingAuthorization | None:
        if self.busy:
            return None
        authorization = OutgoingAuthorization(
            token=secrets.token_urlsafe(32),
            identity=identity,
            number=number,
            expires_at=self._clock() + self.ttl_seconds,
        )
        # Une nouvelle demande invalide le nonce précédent, même s'il n'a pas
        # été consommé. Ni collection illimitée ni plusieurs appels concurrents.
        self._pending = authorization
        return authorization

    def consume(self, token: str, *, identity: str, call_id: str) -> str | None:
        active = self._active_authorization
        if self._active_call == call_id and active is not None:
            # Twilio peut re-demander le même TwiML après un timeout. Le nonce
            # reste utilisable pour cet exact appel seulement, jamais un autre.
            if identity == active.identity and secrets.compare_digest(
                token.encode(), active.token.encode()
            ):
                return active.number
            return None
        pending = self._pending
        if self.busy or pending is None or not call_id:
            return None
        if self._clock() >= pending.expires_at:
            self._pending = None
            return None
        if identity != pending.identity or not secrets.compare_digest(
            token.encode(), pending.token.encode()
        ):
            return None
        self._pending = None
        self._active_call = call_id
        self._active_child = None
        self._active_authorization = pending
        return pending.number

    def owns(self, call_id: str) -> bool:
        return self._active_call == call_id

    def bind_child(self, call_id: str, child_id: str) -> bool:
        if not self.owns(call_id) or not child_id:
            return False
        if self._active_child is not None and child_id != self._active_child:
            return False
        self._active_child = child_id
        return True

    def forget(self, call_id: str) -> None:
        if self.owns(call_id):
            self._active_call = None
            self._active_child = None
            self._active_authorization = None
