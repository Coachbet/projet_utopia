"""Orchestration : des segments de transcript vers un score, puis vers des actions.

Deux chemins, volontairement de vitesses différentes :

  - chemin rapide, synchrone — les mots-clés tournent sur chaque segment final et
    le score est réévalué immédiatement. C'est le filet si Claude est lent ou en
    panne (cf. le coupe-circuit `keywords_alone_watch`).
  - chemin lent, en tâche de fond — Claude analyse la fenêtre glissante sans
    bloquer l'arrivée des segments suivants, puis déclenche une réévaluation à
    son retour.

Le pipeline ne connaît ni Twilio ni AWS : il ne parle qu'aux `Protocol` de
`domain/ports.py`, donc il tourne à l'identique avec les doublures de
`infrastructure/fakes/`.
"""

from __future__ import annotations

import asyncio
import logging

from vocalguard.application.session import CallSession, SessionRegistry
from vocalguard.domain.alerts import ActionKind, PlannedAction, Thresholds, plan_actions
from vocalguard.domain.events import Event, EventType, Speaker
from vocalguard.domain.models import RiskAssessment, TranscriptSegment
from vocalguard.domain.ports import (
    CallController,
    ContactBook,
    ContentAnalyzer,
    EventSink,
    KeywordEngine,
    ReputationChecker,
    VoiceDetector,
)
from vocalguard.domain.scoring.engine import assess
from vocalguard.domain.scoring.weights import DEFAULT_WEIGHTS, Weights

logger = logging.getLogger(__name__)


class ScoringPipeline:
    """Assemble les quatre signaux, score, et déclenche les actions."""

    def __init__(
        self,
        *,
        event_sink: EventSink,
        keyword_engine: KeywordEngine,
        content_analyzer: ContentAnalyzer | None = None,
        voice_detector: VoiceDetector | None = None,
        reputation_checker: ReputationChecker | None = None,
        contact_book: ContactBook | None = None,
        call_controller: CallController | None = None,
        trusted_contact_number: str = "",
        weights: Weights = DEFAULT_WEIGHTS,
        thresholds: Thresholds | None = None,
        registry: SessionRegistry | None = None,
    ) -> None:
        self._sink = event_sink
        self._keywords = keyword_engine
        self._content = content_analyzer
        self._voice = voice_detector
        self._reputation = reputation_checker
        self._contacts = contact_book
        self._controller = call_controller
        self._trusted_number = trusted_contact_number
        self._weights = weights
        self._thresholds = Thresholds() if thresholds is None else thresholds
        # `is None` et pas `or` : un registre vide est falsy (il définit
        # `__len__`), donc `registry or ...` jetait silencieusement l'injection.
        self._sessions = SessionRegistry() if registry is None else registry
        self._tasks: set[asyncio.Task[None]] = set()

    # --- Cycle de vie de l'appel ------------------------------------------

    async def start_call(self, call_id: str, *, caller_number: str = "") -> CallSession:
        session = self._sessions.get_or_create(call_id, caller_number=caller_number)
        if caller_number:
            await self._check_reputation(session)
            await self._reassess(session)
        return session

    async def end_call(self, call_id: str) -> None:
        self._sessions.drop(call_id)

    async def aclose(self) -> None:
        """Attend les analyses en vol. À appeler en fin de scénario ou d'appel."""
        pending = list(self._tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # --- Porte d'entrée pour Dev 1 ----------------------------------------

    async def handle_event(self, event: Event) -> None:
        """Consomme un événement du contrat commun.

        Dev 1 émet `transcript` ; le pipeline produit `keyword`, `llm`,
        `reputation`, `score` et `action`. Les événements produits ici sont
        ignorés pour qu'un sink rebranché en entrée ne crée pas de boucle.
        """
        if event.type is EventType.TRANSCRIPT:
            await self.on_transcript(
                event.call_id,
                TranscriptSegment(
                    speaker=event.speaker or "caller",
                    text=str(event.data.get("text", "")),
                    t=event.t,
                    is_final=bool(event.data.get("is_final", True)),
                ),
            )

    async def on_transcript(self, call_id: str, segment: TranscriptSegment) -> None:
        session = self._sessions.get_or_create(call_id)
        session.add_turn(segment)

        # Les résultats partiels de Transcribe bougent encore : on ne score que
        # sur des segments stabilisés, sinon le score tremble.
        if not segment.is_final:
            return

        await self._run_keywords(session)
        self._maybe_schedule_llm(session)
        await self._reassess(session)

    async def on_audio(
        self, call_id: str, speaker: Speaker, pcm16: bytes, sample_rate: int
    ) -> None:
        if self._voice is None:
            return
        session = self._sessions.get_or_create(call_id)
        signal = await self._voice.feed(speaker, pcm16, sample_rate)
        if signal is None:
            return
        session.signals.voice = signal
        await self._sink.publish(
            session.event(
                EventType.VOICE,
                {"risk": signal.risk, "windows_analyzed": signal.windows_analyzed},
                speaker=speaker,
            )
        )
        await self._reassess(session)

    # --- Les signaux -------------------------------------------------------

    async def _run_keywords(self, session: CallSession) -> None:
        signal = self._keywords.scan_call(session.final_turns())
        session.signals.keywords = signal
        await self._sink.publish(
            session.event(
                EventType.KEYWORD,
                {"risk": signal.risk, "terms": list(signal.terms)},
            )
        )

    def _maybe_schedule_llm(self, session: CallSession) -> None:
        """Lance Claude en tâche de fond. Un seul vol à la fois par appel.

        La cadence minimale est appliquée par l'analyzer lui-même (il renvoie
        `None` si on l'appelle trop tôt) ; ici on se contente de ne pas
        empiler les requêtes.
        """
        if self._content is None or session.llm_in_flight:
            return
        session.llm_in_flight = True
        task = asyncio.create_task(self._run_llm(session))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_llm(self, session: CallSession) -> None:
        try:
            signal = await self._content.analyze(session.final_turns())
        except Exception:
            # Bedrock indisponible ne doit jamais tuer l'appel : les mots-clés
            # continuent de porter la vigilance.
            logger.warning("analyse de contenu en échec", exc_info=True)
            return
        finally:
            session.llm_in_flight = False

        if signal is None:
            return

        session.signals.content = signal
        await self._sink.publish(
            session.event(
                EventType.LLM,
                {
                    "risk": signal.risk,
                    "tactics": list(signal.tactics),
                    "claimed_identity": signal.claimed_identity,
                    "explanation": signal.explanation,
                },
            )
        )

        # L'identité revendiquée n'arrive qu'en cours d'appel. Elle change la
        # réputation (numéro différent de celui du vrai Lucas) et c'est cette
        # conjonction qui arme le coupe-circuit le plus fort.
        if signal.claimed_identity != session.claimed_identity:
            session.claimed_identity = signal.claimed_identity
            await self._check_reputation(session)

        await self._reassess(session)

    async def _check_reputation(self, session: CallSession) -> None:
        if self._reputation is None or not session.caller_number:
            return
        try:
            signal = await self._reputation.check(session.caller_number, session.claimed_identity)
        except Exception:
            logger.warning("vérification de réputation en échec", exc_info=True)
            return
        session.signals.reputation = signal
        await self._sink.publish(
            session.event(
                EventType.REPUTATION,
                {
                    "risk": signal.risk,
                    "line_type": signal.line_type.value,
                    "in_contacts": signal.in_contacts,
                    "identity_mismatch": signal.identity_mismatch,
                    "reasons": list(signal.reasons),
                },
            )
        )

    # --- Score et actions --------------------------------------------------

    async def _reassess(self, session: CallSession) -> RiskAssessment:
        assessment = assess(session.signals, weights=self._weights, thresholds=self._thresholds)
        session.last_assessment = assessment
        await self._sink.publish(
            session.event(
                EventType.SCORE,
                {
                    "score": assessment.score,
                    "level": assessment.level.wire,
                    "reasons": list(assessment.reasons),
                    "breakers_fired": list(assessment.breakers_fired),
                    "explanation": assessment.explanation,
                },
            )
        )
        await self._apply_actions(session, assessment)
        return assessment

    async def _apply_actions(self, session: CallSession, assessment: RiskAssessment) -> None:
        """N'agit qu'à l'escalade.

        `acted_level` ne redescend pas : si le score oscille autour d'un seuil,
        la victime n'entend l'annonce qu'une fois. Et jamais de raccrochage —
        la mise en attente protège autant et laisse la décision à la victime.
        """
        if assessment.level <= session.acted_level:
            return
        session.acted_level = assessment.level

        contact_name = self._trusted_contact_name(session)
        planned = plan_actions(
            assessment.level,
            contact_name=contact_name,
            already_held=session.caller_on_hold,
        )
        for action in planned:
            if action.kind is ActionKind.SMS_CONTACT and session.sms_sent:
                continue
            await self._execute(session, action, assessment)

    def _trusted_contact_name(self, session: CallSession) -> str | None:
        """Nomme le vrai proche dans l'annonce : « nous avons prévenu Lucas »."""
        if self._contacts is None or not session.claimed_identity:
            return None
        contact = self._contacts.by_identity(session.claimed_identity)
        return contact.name if contact is not None else None

    async def _execute(
        self, session: CallSession, action: PlannedAction, assessment: RiskAssessment
    ) -> None:
        message = action.message
        if action.kind is ActionKind.ANNOUNCE_VICTIM and assessment.explanation:
            # L'explication de Claude est écrite pour être dite telle quelle.
            message = f"{message} {assessment.explanation}"

        ok = False
        if self._controller is not None:
            try:
                ok = await self._dispatch(action, message)
            except Exception:
                logger.warning("action %s en échec", action.kind, exc_info=True)
                ok = False

        if ok:
            if action.kind is ActionKind.HOLD_CALLER:
                session.caller_on_hold = True
            elif action.kind is ActionKind.RESUME_CALLER:
                session.caller_on_hold = False
            elif action.kind is ActionKind.SMS_CONTACT:
                session.sms_sent = True

        await self._sink.publish(
            session.event(
                EventType.ACTION,
                {
                    "action": action.kind.value,
                    "target": action.target,
                    "ok": ok,
                    "message": message,
                },
            )
        )

    async def _dispatch(self, action: PlannedAction, message: str) -> bool:
        assert self._controller is not None
        match action.kind:
            case ActionKind.ANNOUNCE_VICTIM:
                return await self._controller.announce(action.target, message)
            case ActionKind.HOLD_CALLER:
                return await self._controller.hold(action.target)
            case ActionKind.RESUME_CALLER:
                return await self._controller.resume(action.target)
            case ActionKind.SMS_CONTACT:
                if not self._trusted_number:
                    return False
                return await self._controller.send_sms(self._trusted_number, message)
            case ActionKind.NOTIFY_DASHBOARD:
                return True
        return False
