"""Fabrique de l'application FastAPI : c'est ici que tous les adapters sont câblés.

Un seul endroit décide quelle implémentation remplit chaque port, pour qu'on
puisse tourner en démo complète (Twilio + Transcribe) ou entièrement hors ligne
(`TRANSCRIBE_BACKEND=fake`, pas de credentials) sans toucher au code.

Point d'extension pour Dev 2 : `create_app(sink=..., audio_tap_factory=...)`.
Son bus d'événements remplace le sink console, et `audio_tap_factory` branche la
détection de voix synthétique sur l'audio décodé.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from vocalguard.application.history import HistoryRecorder
from vocalguard.application.intervention import InterventionRunner
from vocalguard.application.outgoing import OutgoingAuthorizations
from vocalguard.application.scoring import ScoringPipeline, build_pipeline
from vocalguard.application.sessions import CallSession, SessionRegistry
from vocalguard.application.verification import IdentityVerifier
from vocalguard.application.voicemail import VoicemailService
from vocalguard.config import Settings, get_settings
from vocalguard.domain.ports import AudioConsumer, EventSink, Transcriber
from vocalguard.infrastructure.contacts.book import JsonContactBook
from vocalguard.infrastructure.fakes.call_controller import LoggingCallController
from vocalguard.infrastructure.fakes.event_sink import ConsoleEventSink, FanOutSink
from vocalguard.infrastructure.fakes.transcriber import FakeTranscriber
from vocalguard.infrastructure.history.sqlite import SQLiteCallHistory
from vocalguard.infrastructure.history.voicemail import SQLiteVoicemailStore
from vocalguard.infrastructure.telephony.announcements import AnnouncementStore
from vocalguard.infrastructure.telephony.client import TwilioCallController
from vocalguard.infrastructure.telephony.recordings import TwilioRecordings
from vocalguard.interface.auth import PinGuard, protect_access_logs
from vocalguard.interface.event_hub import EventHub
from vocalguard.interface.http import control, phone_app, voicemail, webhooks
from vocalguard.interface.lifecycle import release_call
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
    protect_access_logs()

    # Le câblage est circulaire : la session émet dans le sink, et le pipeline de
    # score a besoin de la session pour retrouver l'appel. On ouvre donc le
    # fanout d'abord et on y abonne les deux consommateurs ensuite.
    fanout = FanOutSink(sink) if sink is not None else FanOutSink(ConsoleEventSink())
    hub = EventHub()
    fanout.add(hub)

    # La file d'annonces vit hors du contrôleur : le webhook `announce_url` doit
    # pouvoir y lire même quand les actions ne sont que tracées (démo sans Twilio).
    announcements = AnnouncementStore()
    controller = _controller(settings, announcements)

    pipeline: ScoringPipeline | None = None

    def _audio_tap(session: CallSession) -> AudioConsumer | None:
        """Priorité à la fabrique fournie par l'appelant (tests, replay)."""
        if audio_tap_factory is not None:
            return audio_tap_factory(session)
        return pipeline.audio_tap(session) if pipeline is not None else None

    # Le carnet est chargé ICI, une fois, et partagé. Trois consommateurs en ont
    # besoin — le routage liste blanche / analyse (`webhooks.py`), le choix du
    # destinataire du code de vérification (`application/verification.py`) et le
    # signal de réputation (`build_pipeline`) — et aucun ne doit relire le JSON
    # dans le chemin de réponse d'un webhook Twilio.
    contacts = JsonContactBook.from_path(settings.contacts_path)

    mailbox = VoicemailService(
        store=SQLiteVoicemailStore(
            settings.voicemail_path, retention_days=settings.voicemail_retention_days
        ),
        recordings=TwilioRecordings(settings),
        contacts=contacts,
        enabled=settings.voicemail_enabled and settings.twilio_configured,
    )
    configure_voicemail = getattr(controller, "configure_voicemail", None)
    if configure_voicemail is not None:
        configure_voicemail(mailbox)

    registry = SessionRegistry(
        transcriber_factory=transcriber_factory or _transcriber_factory(settings),
        sink=fanout,
        settings=settings,
        audio_tap_factory=_audio_tap,
    )
    history = SQLiteCallHistory(
        settings.history_path,
        enabled=settings.history_enabled,
        retention_days=settings.history_retention_days,
    )
    history_recorder = HistoryRecorder(
        store=history, registry=registry, contacts=contacts, thresholds=settings.thresholds()
    )
    fanout.add(history_recorder)
    intervention = InterventionRunner(
        controller=controller,
        sink=fanout,
        settings=settings,
        registry=registry,
    )
    pipeline = build_pipeline(settings, registry, contacts=contacts)

    # L'ordre compte : l'intervention avant le pipeline, pour qu'un `score` soit
    # traité avant que le segment suivant ne le remplace ; le pipeline en
    # dernier, pour que ses événements arrivent au dashboard après le
    # `transcript` qui les a provoqués.
    fanout.add(intervention)
    fanout.add(pipeline)

    # La vérification d'identité par code (Lot 2). Construite après le registre
    # et le pipeline, dont elle a besoin : elle écoute le bus pour entendre le
    # code sur la piste appelant, et réutilise `contact_replied` du pipeline
    # quand la vérification échoue, au lieu d'un second coupe-circuit.
    verifier = IdentityVerifier(
        registry=registry,
        controller=controller,
        contacts=contacts,
        settings=settings,
        pipeline=pipeline,
    )
    fanout.add(verifier)
    # Déclencheur complémentaire et NON éprouvé : il n'est pas vérifié que Twilio
    # remonte les touches avec un `<Stream>` unidirectionnel (Lot 0 §5).
    registry.dtmf_hook = verifier.on_dtmf
    sink = fanout

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            await history.initialize()
        except Exception:
            log.exception("Historique indisponible au démarrage ; les appels restent opérationnels")
        try:
            await mailbox.initialize()
        except Exception:
            mailbox.enabled = False
            log.warning("Messagerie indisponible au démarrage ; dépôt désactivé")
        maintenance = asyncio.create_task(mailbox.maintain()) if mailbox.enabled else None
        if not settings.public_hostname:
            log.warning(
                "PUBLIC_HOSTNAME est vide : Twilio ne pourra joindre ni le "
                "<Stream> ni les webhooks. Lancer `make tunnel` puis renseigner .env."
            )
        yield
        if maintenance is not None:
            maintenance.cancel()
            with suppress(asyncio.CancelledError):
                await maintenance
        for session in registry.active():
            await release_call(app.state, session.call_id)
        await pipeline.aclose()
        await verifier.aclose()

    app = FastAPI(title="VocalGuard", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def private_responses(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith(("/app", "/control")):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.state.settings = settings
    app.state.pin_guard = PinGuard()
    app.state.sink = sink
    app.state.registry = registry
    app.state.history = history
    app.state.history_recorder = history_recorder
    app.state.dashboard_hub = hub
    app.state.controller = controller
    app.state.contacts = contacts
    app.state.voicemail = mailbox
    app.state.outgoing = OutgoingAuthorizations()
    app.state.intervention = intervention
    app.state.pipeline = pipeline
    # Lu par `POST /control/calls/{id}/verify` et purgé par `lifecycle.release_call`.
    app.state.verifier = verifier
    # L'annonce vocale passe par cette file : le webhook `announce_url` y lit le
    # texte que le contrôleur a déposé (cf. infrastructure/telephony/client.py).
    app.state.announcements = getattr(controller, "announcements", announcements)

    app.include_router(webhooks.router)
    app.include_router(media.router)
    # Import direct, pas via `_mount_dashboard` : le panneau de contrôle fait
    # partie de la moitié Dev 1, son absence serait un bug à voir au démarrage.
    app.include_router(control.router)
    # L'app sur le téléphone de la personne protégée (page + jeton Voice).
    app.include_router(phone_app.router)
    app.include_router(voicemail.router)
    from vocalguard.interface.ws.dashboard import create_dashboard_router

    app.include_router(create_dashboard_router(hub))

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


def _subscribe_dashboard(fanout: FanOutSink) -> None:
    """Abonne le hub du dashboard au bus, s'il existe.

    Séparé de `_mount_dashboard` : monter la route sans abonner le hub
    donnerait une page qui se connecte et ne reçoit jamais rien.
    """
    try:
        from vocalguard.interface.ws.dashboard import HUB
    except ImportError:
        return
    fanout.add(HUB)
