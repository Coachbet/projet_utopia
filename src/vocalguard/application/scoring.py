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
from dataclasses import replace
from typing import TYPE_CHECKING

from vocalguard.application.sessions import CallSession, SessionRegistry
from vocalguard.config import Settings, get_settings
from vocalguard.domain.events import Event, EventType, Speaker
from vocalguard.domain.models import ReputationSignal, RiskAssessment
from vocalguard.domain.scoring.engine import assess
from vocalguard.infrastructure.telephony.reputation import KNOWN_CONTACT_RISK

if TYPE_CHECKING:
    from vocalguard.domain.ports import (
        ContactBook,
        ContentAnalyzer,
        KeywordEngine,
        ReputationChecker,
        VoiceDetector,
    )

logger = logging.getLogger(__name__)

CONTACT_DENIED_REASON = "the real contact says it is not them"
"""Repris mot pour mot par le dashboard : c'est la phrase la plus convaincante
que l'écran puisse afficher devant le jury."""

IDENTITY_VERIFIED_REASON = "verification code read correctly"
"""Le pendant exact de `CONTACT_DENIED_REASON`, et il doit s'afficher aussi
franchement : un appel qu'on vient de disculper mérite de dire pourquoi.

Le risque appliqué est `KNOWN_CONTACT_RISK`, importé de l'adapter de réputation
et non recopié. Ce module est déjà celui qui câble les adapters de Dev 2
(`build_pipeline`), et une seconde constante à 0,05 avec un commentaire disant
« la même valeur que là-bas » est précisément la liste parallèle que ce dépôt a
déjà payée une fois. Même valeur parce que c'est la même affirmation : avoir lu
le code prouve au moins autant que figurer au carnet."""


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

        # Démentis du proche de confiance. Ici et pas sur le signal :
        # `_check_reputation` remplace `signals.reputation` par un objet neuf à
        # chaque changement d'identité revendiquée, ce qui effacerait le drapeau
        # et désarmerait le coupe-circuit au pire moment. Un démenti est un fait
        # sur l'appel, pas sur le numéro.
        self._contact_denied: set[str] = set()

        # Vérifications d'identité réussies, ici pour exactement la même raison
        # que les démentis : `_check_reputation` remplace `signals.reputation`
        # par un objet neuf à chaque changement d'identité revendiquée, ce qui
        # effacerait la disculpation au premier retour de Claude. Avoir lu le bon
        # code est un fait sur l'appel, pas sur le numéro.
        self._identity_verified: set[str] = set()

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
        self._contact_denied.discard(call_id)
        self._identity_verified.discard(call_id)
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

    async def contact_replied(self, call_id: str, *, denied: bool) -> None:
        """Réponse du proche de confiance au SMS de vérification.

        Appelée par `/twilio/sms/inbound`. Idempotente : Twilio rejoue le webhook
        si notre réponse tarde, et un même « non » peut arriver en deux SMS.
        """
        if not denied:
            # Un « oui c'est bien moi » ne disculpe pas l'appel en cours : le
            # proche peut répondre à un SMS d'hier, ou se tromper d'appel. On
            # n'abaisse donc jamais le score sur cette base.
            return

        session = self._registry.get(call_id)
        if session is None:
            logger.warning("démenti du proche pour un appel inconnu : %s", call_id)
            return
        if call_id in self._contact_denied:
            return

        self._contact_denied.add(call_id)
        logger.info("appel %s : le proche de confiance dément être l'appelant", call_id)
        await self._apply_reputation(session, session.signals.reputation)
        await self._publish_score(session)

    async def contact_verified(self, call_id: str) -> None:
        """Le proche a lu le bon code : l'appelant est bien qui il prétend être.

        Le pendant exact de `contact_replied`, appelé par
        `application/verification.py` quand le code à 4 chiffres envoyé au numéro
        du CARNET est prononcé correctement dans l'appel. C'est une preuve forte
        et d'une nature que nos heuristiques n'atteignent pas : l'interlocuteur
        tient physiquement le téléphone du proche, ce qu'une usurpation de numéro
        ne donne pas.

        La disculpation passe par le **signal de réputation** et par lui seul,
        conformément au contrat : on abaisse son risque et on désarme
        `identity_mismatch`, donc le coupe-circuit « paiement + identité ». On ne
        touche NI aux coupe-circuits, NI au score directement, et surtout pas aux
        signaux de contenu. C'est volontaire et c'est la partie honnête : un vrai
        petit-fils vérifié qui réclame quand même un virement en urgence garde
        son risque de contenu, et le score peut rester élevé pour cette raison-là.
        Vérifier une identité ne blanchit pas un propos.

        Idempotente : l'app peut cliquer deux fois, et une phrase entendue peut
        déclencher la même vérification que la touche 1.
        """
        session = self._registry.get(call_id)
        if session is None:
            logger.warning("vérification d'identité pour un appel inconnu : %s", call_id)
            return
        if call_id in self._identity_verified:
            return
        if call_id in self._contact_denied:
            # Le démenti l'emporte, et on le dit fort. Les deux faits ne peuvent
            # pas être vrais ensemble : quelqu'un a lu le bon code sur le
            # téléphone d'un proche qui affirme ne pas appeler. Abaisser le score
            # ici désarmerait la protection sur le seul appel où elle a déjà été
            # réclamée explicitement.
            logger.warning(
                "appel %s : code correct APRÈS un démenti du proche — le démenti "
                "l'emporte, le score n'est pas abaissé",
                call_id,
            )
            return

        self._identity_verified.add(call_id)
        logger.info("appel %s : identité vérifiée par code, risque de réputation abaissé", call_id)
        await self._apply_reputation(session, session.signals.reputation)
        await self._publish_score(session)

    def _with_verdict(
        self, call_id: str, signal: ReputationSignal | None
    ) -> ReputationSignal | None:
        """Réinjecte le verdict humain dans un signal de réputation reconstruit.

        Le démenti est testé AVANT la vérification, et l'ordre est la règle :
        les deux ne peuvent pas être vrais ensemble, et entre « le proche dit que
        ce n'est pas lui » et « quelqu'un a su lire le code », c'est la personne
        qui parle en son nom qui tranche.
        """
        if call_id in self._contact_denied:
            if signal is None:
                # Le démenti peut arriver avant toute vérification de réputation :
                # on fabrique alors le signal, sinon le coupe-circuit n'a rien à lire.
                return ReputationSignal(
                    risk=1.0, contact_denied=True, reasons=(CONTACT_DENIED_REASON,)
                )
            reasons = signal.reasons
            if CONTACT_DENIED_REASON not in reasons:
                reasons = (*reasons, CONTACT_DENIED_REASON)
            # Risque maximal : aucune heuristique de numéro ne pèse plus que la
            # personne concernée disant que ce n'est pas elle.
            return replace(signal, risk=1.0, contact_denied=True, reasons=reasons)

        if call_id in self._identity_verified:
            if signal is None:
                return ReputationSignal(
                    risk=KNOWN_CONTACT_RISK,
                    in_contacts=True,
                    reasons=(IDENTITY_VERIFIED_REASON,),
                )
            # `identity_mismatch=False` est l'effet qui compte : c'est lui qui
            # désarme le coupe-circuit « paiement + identité », donc le plancher
            # à 85. Le numéro reste peut-être différent de celui du carnet — mais
            # on ne le déduit plus d'un écart de numéro, on le SAIT par le code.
            #
            # Les raisons précédentes sont REMPLACÉES, pas complétées. Toutes
            # celles que produit `telephony/reputation.py` sont des inférences
            # sur un numéro inconnu (« numéro inconnu », « ligne VoIP non fixe »,
            # « numéro différent de celui de Lucas Martin ») : le code lu les
            # périme toutes. Les garder ferait afficher au dashboard « numéro
            # différent de celui de Lucas Martin » juste au-dessus de « code de
            # vérification lu correctement », et l'opérateur, devant un jury,
            # ne saurait pas laquelle des deux croire.
            return replace(
                signal,
                risk=min(signal.risk, KNOWN_CONTACT_RISK),
                identity_mismatch=False,
                reasons=(IDENTITY_VERIFIED_REASON,),
            )

        return signal

    async def _apply_reputation(
        self, session: CallSession, signal: ReputationSignal | None
    ) -> None:
        """Pose le signal de réputation et le publie, verdict humain réinjecté."""
        signal = self._with_verdict(session.call_id, signal)
        if signal is None:
            return
        session.signals.reputation = signal
        await session.emit(
            EventType.REPUTATION,
            {
                "risk": signal.risk,
                "line_type": signal.line_type.value,
                "in_contacts": signal.in_contacts,
                "identity_mismatch": signal.identity_mismatch,
                "contact_denied": signal.contact_denied,
                "reasons": list(signal.reasons),
            },
        )

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
        await self._apply_reputation(session, signal)

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


def build_pipeline(
    settings: Settings,
    registry: SessionRegistry,
    *,
    contacts: ContactBook | None = None,
) -> ScoringPipeline:
    """Câble la moitié Dev 2 : les quatre signaux et le moteur de score.

    Vit ici et pas dans `interface/app.py` : ce sont les adapters de Dev 2, donc
    en ajouter un ne doit pas obliger à éditer le fichier de Dev 1.

    `contacts` est à passer quand l'appelant a déjà chargé le carnet — c'est le
    cas de `create_app`, qui le partage avec le routage liste blanche et la
    vérification d'identité. Omis, le carnet est relu depuis `contacts_path`,
    pour que les tests et `make replay` construisent un pipeline d'une ligne.

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

        analyzer = BedrockContentAnalyzer(settings=settings)

    def voice_factory() -> VoiceDetector:
        from vocalguard.infrastructure.voice.synthetic_detector import (
            Wav2VecVoiceDetector,
        )

        # Une fabrique et pas une instance : le détecteur accumule des fenêtres
        # de 4 s, et en partager un entre deux appels donnerait un score absurde.
        return Wav2VecVoiceDetector()

    if contacts is None:
        contacts = JsonContactBook.from_path(settings.contacts_path)
    return ScoringPipeline(
        registry=registry,
        keyword_engine=KeywordDetector(),
        content_analyzer=analyzer,
        reputation_checker=TwilioReputationChecker(contacts=contacts, settings=settings),
        voice_detector_factory=voice_factory,
        settings=settings,
    )
