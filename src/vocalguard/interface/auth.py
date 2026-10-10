"""PIN partagé entre l'app, les contrôles et le flux du dashboard.

Le blocage porte sur le code du serveur, pas sur un en-tête IP contrôlable par
un client. Changer de route ou d'adresse ne remet donc pas les essais à zéro.
L'état est local au processus : le MVP s'exécute avec un seul worker.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request

log = logging.getLogger(__name__)
PIN_HEADER = "X-App-Pin"


class PinGuard:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._failures = 0
        self._blocked_until = 0.0

    def check(self, expected: str, received: str) -> None:
        if not re.fullmatch(r"[0-9]{6,}", expected):
            raise HTTPException(
                503, "APP_PIN must contain at least 6 digits to turn on protection."
            )
        now = self._clock()
        if self._blocked_until > now:
            self._locked(now)
        if self._blocked_until:
            self._failures = 0
            self._blocked_until = 0.0
        if secrets.compare_digest(expected.encode(), received.encode()):
            self._failures = 0
            return
        self._failures += 1
        log.warning("Code d'accès refusé (valeurs non journalisées).")
        if self._failures >= 5:
            self._blocked_until = now + 600
            self._locked(now)
        raise HTTPException(403, "Incorrect code.")

    def _locked(self, now: float) -> None:
        seconds = max(1, int(self._blocked_until - now + 0.999))
        raise HTTPException(
            429,
            "Too many attempts. Try again in 10 minutes.",
            headers={"Retry-After": str(seconds)},
        )


def require_pin(state: Any, pin: str) -> None:
    guard = getattr(state, "pin_guard", None)
    if guard is None:
        guard = PinGuard()
        state.pin_guard = guard
    guard.check(state.settings.app_pin, pin)


async def require_control_pin(request: Request) -> None:
    require_pin(request.app.state, request.headers.get(PIN_HEADER, ""))


class PinLogFilter(logging.Filter):
    """Le PIN de connexion WebSocket n'apparaît jamais dans les logs Uvicorn."""

    def filter(self, record: logging.LogRecord) -> bool:
        def redact(value: object) -> object:
            if isinstance(value, str):
                value = re.sub(r"([^\s\"']+\?)[^\s\"']*", r"\1[masqué]", value)
                return re.sub(r"([?&]pin=)[^&\s\"']*", r"\1[masqué]", value)
            return value

        record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: redact(value) for key, value in record.args.items()}
        return True


def protect_access_logs() -> None:
    for name in ("uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, PinLogFilter) for f in logger.filters):
            logger.addFilter(PinLogFilter())
