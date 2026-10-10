"""Fabrique de l'application FastAPI : c'est ici que tous les adapters sont câblés.

Un seul endroit décide quelle implémentation remplit chaque port, pour qu'on
puisse tourner en démo complète (Twilio + Transcribe) ou entièrement hors ligne
(`TRANSCRIBE_BACKEND=fake`, pas de credentials) sans toucher au code.

Point d'extension pour Dev 2 : `create_app(sink=..., audio_tap_factory=...)`.
Son bus d'événements remplace le sink console, et `audio_tap_factory` branche la
détection de voix synthétique sur l'audio décodé.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI

from vocalguard.application.intervention import InterventionRunner
from vocalguard.application.sessions import CallSession, SessionRegistry
from vocalguard.config import Settings, get_settings
from vocalguard.domain.ports import AudioConsumer, EventSink, Transcriber
from vocalguard.infrastructure.fakes.call_controller import LoggingCallController
from vocalguard.infrastructure.fakes.event_sink import ConsoleEventSink, FanOutSink
from vocalguard.infrastructure.fakes.transcriber import FakeTranscriber
from vocalguard.infrastructure.telephony.announcements import AnnouncementStore
from vocalguard.infrastructure.telephony.client import TwilioCallController
from vocalguard.interface.http import webhooks
from vocalguard.interface.ws import media

log = logging.getLogger(__name__)


def _transcriber_factory(settings: Settings) -> Callable[[], Transcriber]:
    """Choisit le moteur de transcription. Les deux replis du README sont ici :
    `fake` pour bosser sans AWS, `whisper_local` si le rôle workshop refuse
    `transcribe:StartStreamTranscription`."""
    backend = settings.transcribe_backend

    if backend == "aws":
        # Import tardif : sans credentials AWS, on veut quand même pouvoir démarrer.
        from vocalguard.infrastructure.aws.transcribe import AwsTranscriber

        return lambda: AwsTranscriber(settings)

    if backend == "whisper_local":
        log.warning(
            "TRANSCRIBE_BACKEND=whisper_local n'est pas encore implémenté — "
            "repli sur le transcripteur factice (aucune transcription réelle)."
        )

    return FakeTranscriber


def _controller(settings: Settings, announcements: AnnouncementStore):
    """Sans credentials Twilio, on trace les actions au lieu de les exécuter :
    le pipeline reste testable de bout en bout sans téléphone."""
    if settings.twilio_configured:
        return TwilioCallController(settings, announcements=announcements)
    log.warning("Twilio non configuré — les actions seront seulement tracées.")
    return LoggingCallController()


def create_app(
    settings: Settings | None = None,
    *,
    sink: EventSink | None = None,
    transcriber_factory: Callable[[], Transcriber] | None = None,
    audio_tap_factory: Callable[[CallSession], AudioConsumer | None] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level.upper())

    sink = sink or FanOutSink(ConsoleEventSink())
    # La file d'annonces vit hors du contrôleur : le webhook `announce_url` doit
    # pouvoir y lire même quand les actions ne sont que tracées (démo sans Twilio).
    announcements = AnnouncementStore()
    controller = _controller(settings, announcements)
    registry = SessionRegistry(
        transcriber_factory=transcriber_factory or _transcriber_factory(settings),
        sink=sink,
        settings=settings,
        audio_tap_factory=audio_tap_factory,
    )
    intervention = InterventionRunner(
        controller=controller,
        sink=sink,
        settings=settings,
        registry=registry,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not settings.public_hostname:
            log.warning(
                "PUBLIC_HOSTNAME est vide : Twilio ne pourra joindre ni le "
                "<Stream> ni les webhooks. Lancer `make tunnel` puis renseigner .env."
            )
        yield
        for session in registry.active():
            await registry.close(session.call_id)

    app = FastAPI(title="VocalGuard", version="0.1.0", lifespan=lifespan)

    app.state.settings = settings
    app.state.sink = sink
    app.state.registry = registry
    app.state.controller = controller
    app.state.intervention = intervention
    # L'annonce vocale passe par cette file : le webhook `announce_url` y lit le
    # texte que le contrôleur a déposé (cf. infrastructure/telephony/client.py).
    app.state.announcements = getattr(controller, "announcements", announcements)

    app.include_router(webhooks.router)
    app.include_router(media.router)
    _mount_dashboard(app)

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {
            "ok": True,
            "transcribe_backend": settings.transcribe_backend,
            "twilio_configured": settings.twilio_configured,
            "public_hostname": settings.public_hostname or None,
            "active_calls": [s.call_id for s in registry.active()],
        }

    return app


def _mount_dashboard(app: FastAPI) -> None:
    """Le dashboard (WebSocket + page HTML) est la part de Dev 2 et Biz 1.

    On le monte s'il existe, sans l'exiger : Dev 1 doit pouvoir démarrer le
    serveur avant que ces fichiers soient écrits.
    """
    try:
        from vocalguard.interface.ws import dashboard  # type: ignore[attr-defined]
    except ImportError:
        log.info("Pas de dashboard monté (interface/ws/dashboard.py absent).")
        return
    app.include_router(dashboard.router)
