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

    def __init__(self, session: CallSession, detector: VoiceDetector, sample_rate: int) -> None:
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

        # Les tâches sont groupées par appel : oublier un appel ne doit pas
        # toucher aux analyses d'un autre, et deux appels simultanés arrivent
        # vraiment (cf. `SessionRegistry.by_stream`).
        self._tasks: dict[str, set[asyncio.Task[None]]] = {}

        # Appels encore scorés. Sert de garde après un `await` : une réponse
        # Bedrock tardive ne doit pas émettre sur un appel déjà raccroché.
        self._live: set[str] = set()

    # --- Branchements ------------------------------------------------------

    def audio_tap(self, session: CallSession):
        """À passer en `audio_tap_factory` de `create_app`."""
        if self._voice_factory is None or not self._settings.voice_detector_enabled:
            return None
        return _VoiceTap(session, self._voice_factory(), self._settings.transcribe_sample_rate)

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
        """Oublie un appel terminé, et annule l'analyse encore en vol.

        Sans l'annulation, une réponse Bedrock lente arrive après le raccrochage
        et émet un `score` pour un appel mort : la frise du dashboard affiche un
        score après la fin de l'appel, et `InterventionRunner` loggue un
        avertissement sur un appel introuvable — un symptôme qu'on aurait créé
        nous-mêmes.

        Les tâches ne sont pas retirées de `_tasks` ici : leurs rappels de fin
        s'en chargent, ce qui laisse `aclose()` les drainer.
        """
        self._live.discard(call_id)
        self._llm_in_flight.discard(call_id)
        self._reputation_done.discard(call_id)
        self._claimed_identity.pop(call_id, None)
        for task in tuple(self._tasks.get(call_id, ())):
            task.cancel()

    async def aclose(self) -> None:
        """Attend les analyses en vol, tous appels confondus.

        `scripts/replay_scenario.py` et les tests d'intégration s'en servent pour
        que l'appel à Claude se pose avant qu'ils ne lisent le score.

        `return_exceptions=True` renvoie bien les `CancelledError` dans les
        résultats au lieu de les lever, malgré leur statut de `BaseException`
        depuis Python 3.8 — vérifié sur 3.12.
        """
        pending = [task for tasks in self._tasks.values() for task in tasks]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # --- Enchaînement ------------------------------------------------------

    async def _on_final_segment(self, session: CallSession) -> None:
        # Un transcript qui arrive après un `forget` veut dire que l'appel est
        # bien vivant : on le réinscrit plutôt que de l'ignorer.
        self._live.add(session.call_id)

        if session.call_id not in self._reputation_done:
            self._reputation_done.add(session.call_id)
            await self._check_reputation(session)

        await self._run_keywords(session)
        self._schedule_llm(session)
        await self._publish_score(session)

    async def _run_keywords(self, session: CallSession) -> None:
        signal = self._keywords.scan_call(session.history)
        session.signals.keywords = signal
        await session.emit(EventType.KEYWORD, {"risk": signal.risk, "terms": list(signal.terms)})

    def _schedule_llm(self, session: CallSession) -> None:
        """Lance Claude en tâche de fond, un seul vol à la fois par appel.

        La cadence minimale est appliquée par l'analyzer (il renvoie `None` si on
        l'appelle trop tôt) ; ici on se contente de ne pas empiler les requêtes.
        """
        if self._content is None or session.call_id in self._llm_in_flight:
            return
        call_id = session.call_id
        self._llm_in_flight.add(call_id)
        task = asyncio.create_task(self._run_llm(session))
        self._tasks.setdefault(call_id, set()).add(task)
        task.add_done_callback(lambda done, cid=call_id: self._discard_task(cid, done))

    def _discard_task(self, call_id: str, task: asyncio.Task[None]) -> None:
        """Retire une tâche terminée sans ressusciter l'entrée d'un appel oublié."""
        tasks = self._tasks.get(call_id)
        if tasks is None:
            return
        tasks.discard(task)
        if not tasks:
            self._tasks.pop(call_id, None)

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

        # L'appel a pu se terminer pendant l'aller-retour Bedrock. L'annulation
        # de `forget` ne suffit pas : la tâche peut déjà être passée l'`await`.
        # La garde vient avant toute écriture d'état, sinon la ligne
        # `_claimed_identity[...]` plus bas ressusciterait l'appel oublié.
        if session.call_id not in self._live:
            logger.debug("analyse ignorée, appel %s déjà terminé", session.call_id)
            return

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
        if self._reputation is None:
            # Configuration assumée (rejeu, tests) : rien à signaler.
            return
        if not session.caller_number:
            # Arrive si le flux média démarre sans le webhook entrant qui porte
            # le `From`. Sans numéro, pas de signal réputation, donc le
            # coupe-circuit paiement + identité ne peut jamais s'armer — et rien
            # ne le dirait. D'où ce warning explicite plutôt qu'un retour muet.
            logger.warning(
                "appel %s sans numéro appelant : signal réputation désactivé, "
                "le coupe-circuit paiement + identité ne peut pas s'armer",
                session.call_id,
            )
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
        assessment = assess(session.signals, weights=self._weights, thresholds=self._thresholds)
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


def build_pipeline(settings: Settings, registry: SessionRegistry) -> ScoringPipeline:
    """Câble la moitié Dev 2 : les quatre signaux et le moteur de score.

    Vit ici et pas dans `interface/app.py` : ce sont les adapters de Dev 2, donc
    en ajouter un ne doit pas obliger à éditer le fichier de Dev 1.

    Imports tardifs : `torch` (installé seulement par `make install-voice`) et
    `boto3` (qui veut des credentials) ne doivent pas être exigés pour démarrer
    le serveur.
    """
    from vocalguard.infrastructure.contacts.book import JsonContactBook
    from vocalguard.infrastructure.keywords.detector import KeywordDetector
    from vocalguard.infrastructure.telephony.reputation import TwilioReputationChecker

    if settings.transcribe_backend == "fake":
        # Démo entièrement hors ligne : même interrupteur que la transcription,
        # pour ne pas avoir deux façons de dire « sans AWS ».
        from vocalguard.infrastructure.fakes.doubles import FakeContentAnalyzer

        analyzer: ContentAnalyzer = FakeContentAnalyzer()
    else:
        from vocalguard.infrastructure.aws.bedrock_analyzer import BedrockContentAnalyzer

        analyzer = BedrockContentAnalyzer()

    def voice_factory() -> VoiceDetector:
        from vocalguard.infrastructure.voice.synthetic_detector import (
            Wav2VecVoiceDetector,
        )

        # Une fabrique et pas une instance : le détecteur accumule des fenêtres
        # de 4 s, et en partager un entre deux appels donnerait un score absurde.
        return Wav2VecVoiceDetector()

    contacts = JsonContactBook.from_path(settings.contacts_path)
    return ScoringPipeline(
        registry=registry,
        keyword_engine=KeywordDetector(),
        content_analyzer=analyzer,
        reputation_checker=TwilioReputationChecker(contacts=contacts, settings=settings),
        voice_detector_factory=voice_factory,
        settings=settings,
    )
