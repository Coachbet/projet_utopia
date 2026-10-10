"""Des transcripts vers un score : la moitié Dev 2 de la boucle.

Ce module ne téléphone pas et n'agit pas sur l'appel. Il s'abonne au bus
d'événements, écoute les `transcript` que publie la session (Dev 1), calcule les
quatre signaux et republie `keyword`, `llm`, `reputation` et `score`. Les actions
sont l'affaire de `application/intervention.py`, qui écoute ces `score` — c'est
le découpage figé par le contrat d'événement.

Deux chemins de vitesses différentes, délibérément :

  - chemin rapide, synchrone — les mots-clés tournent sur chaque segment final et
    le score est réévalué immédiatement. C'est le filet si Claude est lent ou en
    panne (cf. le coupe-circuit `keywords_alone_watch`).
  - chemin lent, en tâche de fond — Claude analyse la fenêtre glissante sans
    bloquer l'arrivée des segments suivants, puis déclenche une réévaluation à
    son retour.

Branchement dans `create_app` :

    pipeline = ScoringPipeline(registry=registry, ...)
    sink = FanOutSink(ConsoleEventSink(), hub, intervention, pipeline)
    #                                                        ^ en dernier : ses
    # propres événements arrivent ainsi au dashboard après le transcript qui les
    # a provoqués, et la frise reste lisible.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from vocalguard.application.sessions import CallSession, SessionRegistry
from vocalguard.config import Settings, get_settings
from vocalguard.domain.events import Event, EventType, Speaker
from vocalguard.domain.models import RiskAssessment
from vocalguard.domain.scoring.engine import assess

if TYPE_CHECKING:
    from vocalguard.domain.ports import (
        ContentAnalyzer,
        KeywordEngine,
        ReputationChecker,
        VoiceDetector,
    )

logger = logging.getLogger(__name__)


class _VoiceTap:
    """`AudioConsumer` d'une seule session.

    Un détecteur par appel : il accumule des fenêtres de 4 s, et mélanger deux
    appels dans le même tampon donnerait un score de voix absurde.
    """

    def __init__(
        self, session: CallSession, detector: VoiceDetector, sample_rate: int
    ) -> None:
        self._session = session
        self._detector = detector
        self._sample_rate = sample_rate

    async def push(self, *, speaker: Speaker, pcm16: bytes, t: float) -> None:
        signal = await self._detector.feed(speaker, pcm16, self._sample_rate)
        if signal is None:
            return
        self._session.signals.voice = signal
        await self._session.emit(
            EventType.VOICE,
            {"risk": signal.risk, "windows_analyzed": signal.windows_analyzed},
            speaker=speaker,
            t=t,
        )


class ScoringPipeline:
    """Implémente `EventSink` : on le branche dans le `FanOutSink`."""

    def __init__(
        self,
        *,
        registry: SessionRegistry,
        keyword_engine: KeywordEngine,
        content_analyzer: ContentAnalyzer | None = None,
        reputation_checker: ReputationChecker | None = None,
        voice_detector_factory: Callable[[], VoiceDetector] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._registry = registry
        self._keywords = keyword_engine
        self._content = content_analyzer
        self._reputation = reputation_checker
        self._voice_factory = voice_detector_factory
        self._settings = settings or get_settings()
        self._weights = self._settings.weights()
        self._thresholds = self._settings.thresholds()

        # État par appel que la session de Dev 1 ne porte pas. Indexé par
        # `call_id` et purgé par `forget`, pour ne pas fuir d'appel en appel.
        self._llm_in_flight: set[str] = set()
        self._claimed_identity: dict[str, str | None] = {}
        self._reputation_done: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    # --- Branchements ------------------------------------------------------

    def audio_tap(self, session: CallSession):
        """À passer en `audio_tap_factory` de `create_app`."""
        if self._voice_factory is None or not self._settings.voice_detector_enabled:
            return None
        return _VoiceTap(
            session, self._voice_factory(), self._settings.transcribe_sample_rate
        )

    async def emit(self, event: Event) -> None:
        """Port `EventSink`. Ne réagit qu'aux `transcript` finaux.

        Ses propres événements lui reviennent par le `FanOutSink` : les ignorer
        est ce qui empêche la boucle.
        """
        if event.type is not EventType.TRANSCRIPT:
            return
        # Les résultats partiels de Transcribe bougent encore : scorer dessus
        # ferait trembler le score et déclencherait des annonces sur du texte
        # qui n'existe déjà plus.
        if not event.data.get("is_final", True):
            return

        session = self._registry.get(event.call_id)
        if session is None:
            logger.warning("transcript pour un appel inconnu : %s", event.call_id)
            return

        try:
            await self._on_final_segment(session)
        except Exception:
            # Une erreur d'analyse ne doit jamais remonter dans le bus : elle
            # priverait le dashboard et l'intervention des événements suivants.
            logger.exception("analyse en échec sur %s", event.call_id)

    def forget(self, call_id: str) -> None:
        self._llm_in_flight.discard(call_id)
        self._reputation_done.discard(call_id)
        self._claimed_identity.pop(call_id, None)

    async def aclose(self) -> None:
        """Attend les analyses en vol. À appeler en fin de scénario ou d'appel."""
        pending = list(self._tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # --- Enchaînement ------------------------------------------------------

    async def _on_final_segment(self, session: CallSession) -> None:
        if session.call_id not in self._reputation_done:
            self._reputation_done.add(session.call_id)
            await self._check_reputation(session)

        await self._run_keywords(session)
        self._schedule_llm(session)
        await self._publish_score(session)

    async def _run_keywords(self, session: CallSession) -> None:
        signal = self._keywords.scan_call(session.history)
        session.signals.keywords = signal
        await session.emit(
            EventType.KEYWORD, {"risk": signal.risk, "terms": list(signal.terms)}
        )

    def _schedule_llm(self, session: CallSession) -> None:
        """Lance Claude en tâche de fond, un seul vol à la fois par appel.

        La cadence minimale est appliquée par l'analyzer (il renvoie `None` si on
        l'appelle trop tôt) ; ici on se contente de ne pas empiler les requêtes.
        """
        if self._content is None or session.call_id in self._llm_in_flight:
            return
        self._llm_in_flight.add(session.call_id)
        task = asyncio.create_task(self._run_llm(session))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_llm(self, session: CallSession) -> None:
        assert self._content is not None
        try:
            signal = await self._content.analyze(session.window())
        except Exception:
            # Bedrock indisponible ne tue pas l'appel : les mots-clés continuent
            # de porter la vigilance.
            logger.warning("analyse de contenu en échec", exc_info=True)
            return
        finally:
            self._llm_in_flight.discard(session.call_id)

        if signal is None:
            return

        session.signals.content = signal
        await session.emit(
            EventType.LLM,
            {
                "risk": signal.risk,
                "tactics": list(signal.tactics),
                "claimed_identity": signal.claimed_identity,
                "explanation": signal.explanation,
            },
        )

        # L'identité revendiquée n'arrive qu'en cours d'appel. Elle change la
        # réputation (numéro différent de celui du vrai Lucas), et c'est cette
        # conjonction qui arme le coupe-circuit le plus fort.
        if signal.claimed_identity != self._claimed_identity.get(session.call_id):
            self._claimed_identity[session.call_id] = signal.claimed_identity
            await self._check_reputation(session)

        await self._publish_score(session)

    async def _check_reputation(self, session: CallSession) -> None:
        if self._reputation is None or not session.caller_number:
            return
        try:
            signal = await self._reputation.check(
                session.caller_number,
                claimed_identity=self._claimed_identity.get(session.call_id),
            )
        except Exception:
            logger.warning("vérification de réputation en échec", exc_info=True)
            return
        session.signals.reputation = signal
        await session.emit(
            EventType.REPUTATION,
            {
                "risk": signal.risk,
                "line_type": signal.line_type.value,
                "in_contacts": signal.in_contacts,
                "identity_mismatch": signal.identity_mismatch,
                "reasons": list(signal.reasons),
            },
        )

    async def _publish_score(self, session: CallSession) -> RiskAssessment:
        assessment = assess(
            session.signals, weights=self._weights, thresholds=self._thresholds
        )
        await session.emit(
            EventType.SCORE,
            {
                "score": assessment.score,
                "level": assessment.level.wire,
                "reasons": list(assessment.reasons),
                "breakers_fired": list(assessment.breakers_fired),
                "explanation": assessment.explanation,
            },
        )
        return assessment
