"""`ScoringPipeline` : des transcripts vers un score, sans téléphone ni AWS.

Le pipeline est un `EventSink` branché dans le bus. On le pilote donc comme en
production — `CallSession.on_segment` alimente l'historique *puis* publie le
`transcript` sur le bus, et c'est ce transcript qui réveille le pipeline. Piloter
`pipeline.emit()` en direct laisserait `session.history` vide et les mots-clés
n'auraient rien à lire ; on ne le fait que pour les cas où l'historique n'entre
pas en jeu (appel inconnu, événement non-transcript).

Les actions (attente, annonce, SMS) ne sont plus ici : elles appartiennent à
`InterventionRunner` et sont couvertes par `tests/unit/test_intervention.py`.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Sequence

import pytest

from vocalguard.application.scoring import (
    CONTACT_DENIED_REASON,
    ScoringPipeline,
    build_pipeline,
)
from vocalguard.application.sessions import CallSession, SessionRegistry
from vocalguard.config import Settings
from vocalguard.domain.events import Event, EventType
from vocalguard.domain.models import (
    ContentSignal,
    KeywordHit,
    KeywordSignal,
    LineType,
    TranscriptSegment,
    VoiceSignal,
)
from vocalguard.domain.ports import (
    AudioConsumer,
    ContactBook,
    ContentAnalyzer,
    EventSink,
    KeywordEngine,
    ReputationChecker,
    VoiceDetector,
)
from vocalguard.infrastructure.fakes.doubles import (
    FakeContentAnalyzer,
    FakeReputationChecker,
    FakeVoiceDetector,
    StaticContactBook,
)
from vocalguard.infrastructure.fakes.event_sink import InMemoryEventSink
from vocalguard.infrastructure.fakes.transcriber import FakeTranscriber


class Bus:
    """`EventSink` dont la composition se fixe après coup.

    Le registre a besoin d'un sink, le pipeline a besoin du registre, et le bus a
    besoin du pipeline : il faut casser le cycle quelque part. En production
    `create_app` n'a pas ce souci, le `FanOutSink` y est construit en dernier.
    """

    def __init__(self) -> None:
        self.sinks: tuple[EventSink, ...] = ()

    async def emit(self, event: Event) -> None:
        for sink in self.sinks:
            await sink.emit(event)


class StubKeywords:
    """Moteur de mots-clés minimal : le vrai est testé séparément."""

    def __init__(self, risk: float = 0.0, terms: tuple[str, ...] = ()) -> None:
        self.risk = risk
        self.terms = terms

    def scan(self, text: str) -> KeywordSignal:
        hits = tuple(KeywordHit(term=t, weight=3, category="test") for t in self.terms)
        return KeywordSignal(risk=self.risk, hits=hits)

    def scan_call(self, turns: Sequence[TranscriptSegment]) -> KeywordSignal:
        return self.scan(" ".join(t.text for t in turns))


class ExplodingKeywords:
    def scan(self, text: str) -> KeywordSignal:
        raise RuntimeError("lexique illisible")

    def scan_call(self, turns: Sequence[TranscriptSegment]) -> KeywordSignal:
        raise RuntimeError("lexique illisible")


class ScriptedAnalyzer:
    """Renvoie un signal imposé, pour piloter le score depuis le test."""

    def __init__(self, signal: ContentSignal | None) -> None:
        self.signal = signal
        self.calls = 0

    async def analyze(self, window: Sequence[TranscriptSegment]) -> ContentSignal | None:
        self.calls += 1
        return self.signal


class ExplodingAnalyzer:
    def __init__(self) -> None:
        self.calls = 0

    async def analyze(self, window: Sequence[TranscriptSegment]) -> ContentSignal | None:
        self.calls += 1
        raise RuntimeError("bedrock injoignable")


RISKY = ContentSignal(risk=0.9, tactics=("urgence",), explanation="virement réclamé")


class GatedAnalyzer:
    """Ne répond que lorsque le test l'y autorise.

    Permet de placer un `forget` pendant l'aller-retour Bedrock sans dormir.
    """

    def __init__(self, signal: ContentSignal = RISKY) -> None:
        self.signal = signal
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.completed = 0

    async def analyze(self, window: Sequence[TranscriptSegment]) -> ContentSignal | None:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        self.completed += 1
        return self.signal


class ForgettingAnalyzer:
    """Oublie l'appel juste avant de répondre, sans jamais rendre la main.

    C'est le cas que l'annulation ne peut pas couvrir : la tâche est déjà passée
    son `await`, donc seule la garde de vivacité peut l'empêcher d'émettre.
    """

    def __init__(self, call_id: str, signal: ContentSignal = RISKY) -> None:
        self.call_id = call_id
        self.signal = signal
        self.pipeline: ScoringPipeline | None = None

    async def analyze(self, window: Sequence[TranscriptSegment]) -> ContentSignal | None:
        assert self.pipeline is not None
        self.pipeline.forget(self.call_id)
        return self.signal


class CountingReputation(FakeReputationChecker):
    """Garde la trace des identités avec lesquelles on l'a interrogée."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.identities: list[str | None] = []

    async def check(self, phone_number: str, *, claimed_identity: str | None = None):
        self.identities.append(claimed_identity)
        return await super().check(phone_number, claimed_identity=claimed_identity)


def settings_for(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


class Harness:
    def __init__(self, recorder: InMemoryEventSink, pipeline: ScoringPipeline) -> None:
        self.recorder = recorder
        self.pipeline = pipeline

    def of_type(self, type_: str) -> list[Event]:
        return self.recorder.by_type(type_)

    @property
    def scores(self) -> list[int]:
        return [e.data["score"] for e in self.of_type("score")]

    @property
    def last_score(self) -> int | None:
        return self.scores[-1] if self.scores else None

    @property
    def last_level(self) -> str | None:
        events = self.of_type("score")
        return events[-1].data["level"] if events else None


def build(
    *,
    keywords=None,
    analyzer=None,
    reputation=None,
    voice_factory=None,
    settings: Settings | None = None,
) -> tuple[Harness, SessionRegistry]:
    settings = settings or settings_for()
    recorder = InMemoryEventSink()
    bus = Bus()
    registry = SessionRegistry(transcriber_factory=FakeTranscriber, sink=bus, settings=settings)
    pipeline = ScoringPipeline(
        registry=registry,
        keyword_engine=keywords if keywords is not None else StubKeywords(),
        content_analyzer=analyzer,
        reputation_checker=reputation,
        voice_detector_factory=voice_factory,
        settings=settings,
    )
    # Le pipeline en dernier : ses événements arrivent ainsi après le transcript
    # qui les a provoqués, comme dans `create_app`.
    bus.sinks = (recorder, pipeline)
    return Harness(recorder, pipeline), registry


def seg(text: str, *, speaker: str = "caller", t: float = 1.0, final: bool = True):
    return TranscriptSegment(speaker=speaker, text=text, t=t, is_final=final)


# --- Conformité aux ports --------------------------------------------------


def test_the_pipeline_is_an_event_sink():
    harness, _ = build()
    assert isinstance(harness.pipeline, EventSink)


@pytest.mark.parametrize(
    ("double", "protocol"),
    [
        (FakeContentAnalyzer(), ContentAnalyzer),
        (FakeVoiceDetector(), VoiceDetector),
        (FakeReputationChecker(), ReputationChecker),
        (StaticContactBook(), ContactBook),
    ],
    ids=["content", "voice", "reputation", "contacts"],
)
def test_doubles_satisfy_their_ports(double, protocol):
    """Une doublure qui dérive du port rendrait les tests verts et la démo muette."""
    assert isinstance(double, protocol)


# --- Chemin rapide ---------------------------------------------------------


async def test_final_segment_emits_keyword_then_score():
    harness, registry = build(keywords=StubKeywords(0.3, ("virement",)))
    session = await registry.open("CA1")

    await session.on_segment(seg("il faut faire un virement"))

    kinds = [e.type for e in harness.recorder.events]
    assert kinds == [EventType.TRANSCRIPT, EventType.KEYWORD, EventType.SCORE]
    assert harness.of_type("keyword")[0].data["terms"] == ["virement"]
    assert harness.last_level == "none"


async def test_keywords_see_the_accumulated_history():
    """`scan_call` lit `session.history`, alimentée par `on_segment` avant l'émission."""
    harness, registry = build(keywords=StubKeywords(0.1, ("x",)))
    session = await registry.open("CA1")

    await session.on_segment(seg("premier tour"))
    await session.on_segment(seg("second tour", t=2.0))

    assert len(session.history) == 2
    assert len(harness.of_type("keyword")) == 2


async def test_partial_segment_produces_no_score():
    harness, registry = build()
    session = await registry.open("CA1")

    await session.on_segment(seg("mamie c'est", final=False))

    # Le transcript partiel est diffusé (le dashboard l'affiche), mais rien de plus.
    assert [e.type for e in harness.recorder.events] == [EventType.TRANSCRIPT]
    assert harness.of_type("score") == []


async def test_events_carry_the_call_id_and_a_positive_time():
    harness, registry = build()
    session = await registry.open("CA42")

    await session.on_segment(seg("bonjour"))

    assert harness.recorder.events
    assert all(e.call_id == "CA42" for e in harness.recorder.events)
    assert all(e.t >= 0 for e in harness.recorder.events)


# --- Chemin lent : Claude en tâche de fond ---------------------------------


async def test_claude_runs_in_the_background_and_rescores_after():
    """Le score tombe avant que Claude ne démarre : c'est tout le design.

    Si l'analyse de contenu bloquait l'arrivée des segments, un Bedrock lent
    gèlerait la protection pendant l'appel.
    """
    analyzer = ScriptedAnalyzer(
        ContentSignal(
            risk=0.95,
            tactics=("urgence", "paiement inhabituel"),
            claimed_identity="Lucas, petit-fils",
            explanation="L'appelant réclame un virement immédiat.",
        )
    )
    harness, registry = build(analyzer=analyzer)
    session = await registry.open("CA1")

    await session.on_segment(seg("mamie envoie un virement tout de suite"))

    assert analyzer.calls == 0, "Claude a été attendu alors qu'il doit tourner en fond"
    assert harness.of_type("score"), "le chemin rapide n'a pas scoré"

    await harness.pipeline.aclose()
    assert analyzer.calls == 1

    llm = harness.of_type("llm")
    assert len(llm) == 1
    assert llm[0].data["claimed_identity"] == "Lucas, petit-fils"
    # Coupe-circuit `llm_high_confidence` : risque 0.95 -> au moins 65.
    assert harness.last_score >= 65
    assert harness.last_level in {"alert", "intervene"}


async def test_only_one_llm_call_in_flight_per_call():
    analyzer = ScriptedAnalyzer(None)
    harness, registry = build(analyzer=analyzer)
    session = await registry.open("CA1")

    for i in range(5):
        await session.on_segment(seg(f"phrase {i}", t=float(i)))

    await harness.pipeline.aclose()
    assert analyzer.calls == 1, "les requêtes Claude se sont empilées"


async def test_an_analyzer_returning_none_leaves_the_signal_untouched():
    harness, registry = build(analyzer=ScriptedAnalyzer(None))
    session = await registry.open("CA1")

    await session.on_segment(seg("bonjour"))
    await harness.pipeline.aclose()

    assert harness.of_type("llm") == []
    assert session.signals.content is None


# --- Robustesse : rien ne remonte dans le bus ------------------------------


async def test_an_exploding_analyzer_does_not_propagate():
    """Un sink qui lève priverait le dashboard et l'intervention des événements."""
    analyzer = ExplodingAnalyzer()
    harness, registry = build(analyzer=analyzer)
    session = await registry.open("CA1")

    await session.on_segment(seg("mamie envoie un virement"))
    await harness.pipeline.aclose()

    assert analyzer.calls == 1
    assert harness.of_type("score"), "le chemin rapide doit survivre à Bedrock"
    assert harness.of_type("llm") == []


async def test_an_exploding_keyword_engine_does_not_propagate():
    """On appelle `pipeline.emit` en direct, et pas `session.on_segment`.

    `CallSession.emit` avale déjà les exceptions de ses sinks : passer par lui
    masquerait le garde-fou du pipeline au lieu de le tester.
    """
    harness, registry = build(keywords=ExplodingKeywords())
    await registry.open("CA1")

    await harness.pipeline.emit(
        Event(
            call_id="CA1",
            t=1.0,
            type=EventType.TRANSCRIPT,
            speaker="caller",
            data={"text": "il faut un virement", "is_final": True},
        )
    )

    assert harness.recorder.events == []


async def test_a_transcript_for_an_unknown_call_is_ignored():
    harness, _ = build()

    await harness.pipeline.emit(
        Event(
            call_id="CA_jamais_ouvert",
            t=1.0,
            type=EventType.TRANSCRIPT,
            speaker="caller",
            data={"text": "virement urgent", "is_final": True},
        )
    )

    assert harness.recorder.events == []


@pytest.mark.parametrize(
    "own_type", [EventType.SCORE, EventType.ACTION, EventType.KEYWORD, EventType.LLM]
)
async def test_the_pipeline_ignores_its_own_events(own_type):
    """Il est dans le `FanOutSink` : ses propres événements lui reviennent."""
    harness, registry = build()
    await registry.open("CA1")

    await harness.pipeline.emit(Event(call_id="CA1", t=1.0, type=own_type, data={"score": 90}))

    assert harness.recorder.events == []


# --- Réputation ------------------------------------------------------------


async def test_identity_discovery_triggers_a_reputation_recheck():
    reputation = CountingReputation(risk=0.5, identity_mismatch=True)
    harness, registry = build(
        analyzer=ScriptedAnalyzer(
            ContentSignal(risk=0.5, tactics=("urgence",), claimed_identity="Lucas")
        ),
        reputation=reputation,
    )
    session = await registry.open("CA1", caller_number="+33755555555")

    await session.on_segment(seg("mamie c'est Lucas"))
    await harness.pipeline.aclose()

    # Une fois au premier segment sans identité, une fois quand Claude la révèle.
    assert reputation.identities == [None, "Lucas"]
    assert harness.of_type("reputation")[-1].data["identity_mismatch"] is True


async def test_reputation_is_checked_once_before_the_identity_is_known():
    reputation = CountingReputation(risk=0.4)
    _harness, registry = build(reputation=reputation)
    session = await registry.open("CA1", caller_number="+33755555555")

    await session.on_segment(seg("premier"))
    await session.on_segment(seg("second", t=2.0))

    assert reputation.identities == [None], "la réputation est repayée à chaque segment"


async def test_reputation_without_a_caller_number_is_skipped():
    reputation = CountingReputation()
    harness, registry = build(reputation=reputation)
    session = await registry.open("CA1")

    await session.on_segment(seg("bonjour"))

    assert reputation.identities == []
    assert harness.of_type("reputation") == []


async def test_line_type_is_published_for_the_dashboard():
    harness, registry = build(
        reputation=FakeReputationChecker(
            risk=0.6, line_type=LineType.NON_FIXED_VOIP, reasons=("ligne VoIP non fixe",)
        )
    )
    session = await registry.open("CA1", caller_number="+33755555555")

    await session.on_segment(seg("bonjour"))

    data = harness.of_type("reputation")[0].data
    assert data["line_type"] == "nonFixedVoip"
    assert data["reasons"] == ["ligne VoIP non fixe"]


# --- Voix synthétique ------------------------------------------------------


def voice_settings() -> Settings:
    return settings_for(voice_detector_enabled=True)


async def test_no_audio_tap_when_the_detector_is_disabled():
    """Par défaut torch n'est pas installé : pas de tap, pas de coût CPU."""
    harness, registry = build(voice_factory=lambda: FakeVoiceDetector())
    session = await registry.open("CA1")

    assert harness.pipeline.audio_tap(session) is None


async def test_the_audio_tap_satisfies_the_audio_consumer_port():
    harness, registry = build(voice_factory=lambda: FakeVoiceDetector(), settings=voice_settings())
    session = await registry.open("CA1")

    assert isinstance(harness.pipeline.audio_tap(session), AudioConsumer)


async def test_voice_alone_is_capped_and_never_intervenes():
    harness, registry = build(
        voice_factory=lambda: FakeVoiceDetector(risk=1.0), settings=voice_settings()
    )
    session = await registry.open("CA1")
    tap = harness.pipeline.audio_tap(session)

    await tap.push(speaker="caller", pcm16=b"\x00\x00" * 100, t=1.0)
    assert harness.of_type("voice")[0].data["risk"] == 1.0

    # Le score n'est republié qu'au segment suivant : la voix seule ne décide rien.
    await session.on_segment(seg("bonjour mamie"))

    score = harness.of_type("score")[-1].data
    assert "voice_alone_capped" in score["breakers_fired"]
    assert score["score"] <= 64
    assert score["level"] != "intervene"


async def test_victim_audio_is_not_analysed():
    harness, registry = build(
        voice_factory=lambda: FakeVoiceDetector(risk=1.0), settings=voice_settings()
    )
    session = await registry.open("CA1")
    tap = harness.pipeline.audio_tap(session)

    await tap.push(speaker="victim", pcm16=b"\x00\x00" * 100, t=1.0)

    assert harness.of_type("voice") == []
    assert session.signals.voice is None


async def test_each_call_gets_its_own_voice_detector():
    """Deux appels partageant un tampon de 4 s donneraient un score absurde."""
    harness, registry = build(
        voice_factory=lambda: FakeVoiceDetector(risk=0.5, windows_before_signal=2),
        settings=voice_settings(),
    )
    first = await registry.open("CA1")
    second = await registry.open("CA2")

    tap_a = harness.pipeline.audio_tap(first)
    tap_b = harness.pipeline.audio_tap(second)
    await tap_a.push(speaker="caller", pcm16=b"\x00\x00", t=1.0)
    await tap_b.push(speaker="caller", pcm16=b"\x00\x00", t=1.0)

    # Une fenêtre chacun, donc aucun des deux n'atteint le seuil de deux.
    assert harness.of_type("voice") == []


# --- Hygiène d'état --------------------------------------------------------


async def test_forget_clears_the_per_call_state():
    reputation = CountingReputation(risk=0.4)
    harness, registry = build(reputation=reputation)
    session = await registry.open("CA1", caller_number="+33755555555")

    await session.on_segment(seg("premier"))
    harness.pipeline.forget("CA1")
    await session.on_segment(seg("second", t=2.0))

    # L'appel est reparti de zéro : la réputation est réinterrogée.
    assert reputation.identities == [None, None]


async def test_forget_on_an_unknown_call_is_a_no_op():
    harness, _ = build(analyzer=ScriptedAnalyzer(RISKY))
    harness.pipeline.forget("CA_JAMAIS_VUE")
    assert harness.recorder.events == []


async def test_an_analysis_cancelled_by_forget_emits_nothing():
    """Réponse Bedrock lente + raccrochage : aucun score pour un appel mort.

    Sinon la frise du dashboard affiche un score après la fin de l'appel, et
    `InterventionRunner` loggue un appel introuvable qu'on a créé nous-mêmes.
    """
    analyzer = GatedAnalyzer()
    harness, registry = build(analyzer=analyzer)
    session = await registry.open("CA1")

    await session.on_segment(seg("mamie c'est Lucas, il faut un virement"))
    await analyzer.started.wait()
    scores_before = len(harness.of_type("score"))

    harness.pipeline.forget("CA1")
    analyzer.release.set()
    await harness.pipeline.aclose()

    assert harness.of_type("llm") == []
    assert len(harness.of_type("score")) == scores_before
    # La tâche a été annulée avant de répondre.
    assert analyzer.completed == 0


async def test_an_analysis_that_outruns_the_cancellation_still_emits_nothing():
    """La garde de vivacité, et non l'annulation, est ce qui attrape ce cas."""
    analyzer = ForgettingAnalyzer("CA1")
    harness, registry = build(analyzer=analyzer)
    analyzer.pipeline = harness.pipeline
    session = await registry.open("CA1")

    await session.on_segment(seg("mamie c'est Lucas, il faut un virement"))
    scores_before = len(harness.of_type("score"))
    await harness.pipeline.aclose()

    assert harness.of_type("llm") == []
    assert len(harness.of_type("score")) == scores_before
    # Et l'état de l'appel oublié n'a pas été ressuscité.
    assert session.signals.content is None


async def test_forgetting_one_call_leaves_another_running():
    """Deux appels simultanés arrivent vraiment (cf. `SessionRegistry.by_stream`)."""
    analyzer = GatedAnalyzer()
    harness, registry = build(analyzer=analyzer)
    kept = await registry.open("CA_GARDE")
    dropped = await registry.open("CA_OUBLIEE")

    await kept.on_segment(seg("premier appel"))
    await dropped.on_segment(seg("second appel"))
    await analyzer.started.wait()

    harness.pipeline.forget("CA_OUBLIEE")
    analyzer.release.set()
    await harness.pipeline.aclose()

    assert {e.call_id for e in harness.of_type("llm")} == {"CA_GARDE"}


async def test_aclose_does_not_raise_when_a_task_was_cancelled():
    """`CancelledError` est une `BaseException` : `gather` doit quand même la
    renvoyer dans ses résultats plutôt que la lever.

    Le `wait_for` est délibéré : ce test ne débloque jamais la porte, donc c'est
    l'annulation qui doit libérer la tâche. Sans elle, `aclose()` attendrait pour
    toujours — et un test qui pend est bien pire qu'un test qui échoue.
    """
    analyzer = GatedAnalyzer()
    harness, registry = build(analyzer=analyzer)
    session = await registry.open("CA1")

    await session.on_segment(seg("mamie"))
    await analyzer.started.wait()
    harness.pipeline.forget("CA1")

    await asyncio.wait_for(harness.pipeline.aclose(), timeout=2.0)


async def test_task_bookkeeping_does_not_leak_between_calls():
    analyzer = ScriptedAnalyzer(RISKY)
    harness, registry = build(analyzer=analyzer)
    for call_id in ("CA1", "CA2", "CA3"):
        session = await registry.open(call_id)
        await session.on_segment(seg("mamie", t=1.0))
    await harness.pipeline.aclose()

    assert harness.pipeline._tasks == {}


# --- La doublure de Claude -------------------------------------------------


async def test_fake_analyzer_detects_the_demo_scenario():
    """La doublure doit produire les tactiques qui arment les coupe-circuits."""
    signal = await FakeContentAnalyzer().analyze(
        [
            seg("Mamie c'est Lucas, j'ai eu un accident"),
            seg("il faut faire un virement tout de suite, ne dis rien à maman"),
        ]
    )

    assert signal is not None
    assert any("paiement" in t for t in signal.tactics)
    assert "urgence" in signal.tactics
    assert "secret" in signal.tactics
    assert signal.claimed_identity == "Lucas"


async def test_fake_analyzer_stays_quiet_on_a_legitimate_call():
    signal = await FakeContentAnalyzer().analyze([seg("je passe dimanche, j'apporte le gâteau")])

    assert signal is not None
    assert signal.risk == 0.0


async def test_fake_analyzer_ignores_the_victims_words():
    """La victime qui répète « virement » n'est pas une preuve contre elle."""
    signal = await FakeContentAnalyzer().analyze(
        [seg("un virement tout de suite ?", speaker="victim")]
    )

    assert signal is None


# --- Une session construite à la main -------------------------------------


async def test_an_injected_clock_drives_the_event_timestamps():
    """Les horodatages viennent de l'horloge de la session, pas du temps réel :
    c'est ce qui rend un rejeu de scénario reproductible."""

    class ManualClock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    clock = ManualClock()
    settings = settings_for()
    recorder = InMemoryEventSink()
    bus = Bus()
    registry = SessionRegistry(transcriber_factory=FakeTranscriber, sink=bus, settings=settings)
    pipeline = ScoringPipeline(registry=registry, keyword_engine=StubKeywords(), settings=settings)
    bus.sinks = (recorder, pipeline)

    session = CallSession(
        call_id="CA1",
        transcriber=FakeTranscriber(),
        sink=bus,
        settings=settings,
        clock=clock,
    )
    await session.start()
    registry._sessions["CA1"] = session

    clock.value = 7.0
    await session.on_segment(seg("virement urgent", t=7.0))

    assert [e.t for e in recorder.events] == [7.0, 7.0, 7.0]


# --- build_pipeline : le câblage vit côté Dev 2 ----------------------------


def _registry_for(settings: Settings) -> SessionRegistry:
    return SessionRegistry(
        transcriber_factory=FakeTranscriber, sink=InMemoryEventSink(), settings=settings
    )


def test_build_pipeline_wires_the_four_signals():
    """`build_pipeline` vit dans scoring.py et non dans `interface/app.py` :
    ajouter un adapter de Dev 2 ne doit pas obliger à éditer le fichier de Dev 1."""
    settings = settings_for(transcribe_backend="fake")
    pipeline = build_pipeline(settings, _registry_for(settings))

    assert isinstance(pipeline, ScoringPipeline)
    assert isinstance(pipeline, EventSink)

    assert isinstance(pipeline._keywords, KeywordEngine)
    assert isinstance(pipeline._content, ContentAnalyzer)
    assert isinstance(pipeline._reputation, ReputationChecker)
    # La voix est une fabrique, pas une instance : un détecteur par appel, sinon
    # les fenêtres de 4 s de deux appels se mélangent.
    assert callable(pipeline._voice_factory)


def test_build_pipeline_stays_offline_with_the_fake_backend():
    """`transcribe_backend=fake` est l'unique interrupteur « sans AWS » : il doit
    aussi écarter Bedrock, sinon une démo hors ligne appelle quand même Claude."""
    settings = settings_for(transcribe_backend="fake")
    pipeline = build_pipeline(settings, _registry_for(settings))
    assert type(pipeline._content).__name__ == "FakeContentAnalyzer"


def test_build_pipeline_does_not_load_the_voice_stack():
    """La fabrique voix ne doit pas être appelée au câblage.

    `torch` n'est installé que par `make install-voice` (~2 Go) : construire le
    pipeline ne doit même pas charger le module du détecteur. On vérifie
    l'absence du module et pas seulement celle de `torch`, sinon le test
    passerait tout seul sur une machine où torch n'est pas installé.
    """
    voice_module = "vocalguard.infrastructure.voice.synthetic_detector"
    sys.modules.pop(voice_module, None)

    settings = settings_for(transcribe_backend="fake")
    build_pipeline(settings, _registry_for(settings))

    assert voice_module not in sys.modules
    assert "torch" not in sys.modules


# --- Le numéro appelant manquant ne doit pas échouer en silence ------------


async def test_an_empty_caller_number_is_warned_about(caplog):
    """Si le flux média démarre sans le webhook qui porte le `From`, le signal
    réputation ne part jamais et le coupe-circuit à 85 ne peut pas s'armer.
    Sans log, on chercherait le bug dans le moteur de score."""
    harness, registry = build(reputation=CountingReputation())
    session = await registry.open("CA_SANS_NUMERO")

    with caplog.at_level(logging.WARNING, logger="vocalguard.application.scoring"):
        await session.on_segment(seg("bonjour"))

    assert harness.of_type("reputation") == []
    assert "CA_SANS_NUMERO" in caplog.text
    assert "réputation" in caplog.text


async def test_a_valid_caller_number_warns_about_nothing(caplog):
    harness, registry = build(reputation=FakeReputationChecker())
    session = await registry.open("CA1", caller_number="+33755555555")

    with caplog.at_level(logging.WARNING, logger="vocalguard.application.scoring"):
        await session.on_segment(seg("bonjour"))

    assert harness.of_type("reputation")
    assert caplog.text == ""


async def test_no_reputation_checker_configured_is_not_a_warning(caplog):
    """Rejeu et tests tournent sans vérificateur : c'est assumé, pas un défaut."""
    _, registry = build(reputation=None)
    session = await registry.open("CA1")

    with caplog.at_level(logging.WARNING, logger="vocalguard.application.scoring"):
        await session.on_segment(seg("bonjour"))

    assert caplog.text == ""


# --- Démenti du proche de confiance ----------------------------------------
# `/twilio/sms/inbound` appelle `contact_replied(call_id, denied=True)`. Le
# démenti est le signal le plus fort du système : la seule personne qui puisse
# trancher l'a tranché.


async def test_denial_floors_the_score_at_intervention():
    harness, registry = build(reputation=FakeReputationChecker())
    session = await registry.open("CA1", caller_number="+33755555555")
    await session.on_segment(seg("bonjour mamie"))

    await harness.pipeline.contact_replied("CA1", denied=True)

    assert harness.last_score >= 85
    assert harness.last_level == "intervene"
    assert "trusted_contact_denied" in harness.of_type("score")[-1].data["breakers_fired"]


async def test_denial_survives_a_reputation_rebuild():
    """La régression qui compte.

    `_check_reputation` remplace `signals.reputation` par un objet neuf à chaque
    changement d'identité revendiquée. Un drapeau posé sur cet objet serait
    effacé, et le coupe-circuit se désarmerait au pire moment.
    """
    harness, registry = build(
        analyzer=ScriptedAnalyzer(
            ContentSignal(risk=0.4, tactics=("urgence",), claimed_identity="Lucas")
        ),
        reputation=CountingReputation(),
    )
    session = await registry.open("CA1", caller_number="+33755555555")
    await session.on_segment(seg("mamie c'est Lucas"))

    await harness.pipeline.contact_replied("CA1", denied=True)
    assert harness.last_score >= 85

    # L'analyse de fond révèle l'identité -> nouvelle vérification de réputation,
    # donc un signal reconstruit de zéro.
    await harness.pipeline.aclose()
    assert len(harness.of_type("reputation")) >= 2, "la reconstruction n'a pas eu lieu"

    assert session.signals.reputation is not None
    assert session.signals.reputation.contact_denied is True
    assert harness.last_score >= 85
    assert harness.last_level == "intervene"


async def test_denial_is_published_with_a_french_reason():
    harness, registry = build(reputation=FakeReputationChecker())
    session = await registry.open("CA1", caller_number="+33755555555")
    await session.on_segment(seg("bonjour"))

    await harness.pipeline.contact_replied("CA1", denied=True)

    data = harness.of_type("reputation")[-1].data
    assert data["contact_denied"] is True
    # Le dashboard reprend ces chaînes mot pour mot.
    assert CONTACT_DENIED_REASON in data["reasons"]


async def test_denial_before_any_reputation_check_still_arms():
    """Le SMS peut revenir avant toute vérification de réputation."""
    harness, registry = build(reputation=None)
    session = await registry.open("CA1")

    await harness.pipeline.contact_replied("CA1", denied=True)

    assert session.signals.reputation is not None
    assert session.signals.reputation.contact_denied is True
    assert harness.last_score >= 85


async def test_a_reassuring_reply_changes_nothing():
    """« Oui c'est bien moi » ne disculpe pas : le proche peut se tromper d'appel."""
    harness, registry = build(reputation=FakeReputationChecker())
    session = await registry.open("CA1", caller_number="+33755555555")
    await session.on_segment(seg("bonjour"))
    before = harness.last_score

    await harness.pipeline.contact_replied("CA1", denied=False)

    assert harness.last_score == before
    assert session.signals.reputation is not None
    assert session.signals.reputation.contact_denied is False


async def test_denial_is_idempotent():
    """Twilio rejoue le webhook si notre réponse tarde."""
    harness, registry = build(reputation=FakeReputationChecker())
    session = await registry.open("CA1", caller_number="+33755555555")
    await session.on_segment(seg("bonjour"))
    reputations = len(harness.of_type("reputation"))

    await harness.pipeline.contact_replied("CA1", denied=True)
    after_first = len(harness.of_type("reputation"))
    await harness.pipeline.contact_replied("CA1", denied=True)

    assert after_first == reputations + 1
    assert len(harness.of_type("reputation")) == after_first
    assert harness.last_score >= 85


async def test_denial_for_an_unknown_call_is_a_no_op():
    harness, _ = build(reputation=FakeReputationChecker())
    await harness.pipeline.contact_replied("CA_INCONNU", denied=True)
    assert harness.of_type("score") == []


async def test_forget_purges_the_denial():
    """Sans purge, un appel suivant sur le même identifiant naîtrait condamné."""
    harness, registry = build(reputation=FakeReputationChecker())
    session = await registry.open("CA1", caller_number="+33755555555")
    await session.on_segment(seg("bonjour"))
    await harness.pipeline.contact_replied("CA1", denied=True)
    assert harness.last_score >= 85

    harness.pipeline.forget("CA1")
    await registry.close("CA1")

    session2 = await registry.open("CA1", caller_number="+33755555555")
    await session2.on_segment(seg("bonjour"))

    assert session2.signals.reputation is not None
    assert session2.signals.reputation.contact_denied is False
    assert harness.last_score < 85


async def test_denial_beats_the_voice_alone_cap():
    """Les plafonds s'appliquent APRÈS les planchers, donc un plafond gagne.

    Sans exception, le plafond « voix seule » écraserait le plancher du démenti
    et un « non » du vrai Lucas retomberait à 64, sous le seuil d'intervention.

    Le poids de réputation est abaissé exprès : à 15 (défaut), la réputation
    pèse pile `VOICE_ALONE_MARGIN` et le plafond ne se déclenche pas de
    toute façon — le test ne prouverait rien. Les poids sont réglables par
    `.env`, donc la garde doit tenir sans dépendre de cette coïncidence.
    """
    harness, registry = build(reputation=None, settings=settings_for(score_weight_reputation=10))
    session = await registry.open("CA1")
    session.signals.voice = VoiceSignal(risk=1.0, windows_analyzed=2)

    await harness.pipeline.contact_replied("CA1", denied=True)

    fired = harness.of_type("score")[-1].data["breakers_fired"]
    assert "trusted_contact_denied" in fired
    assert "voice_alone_capped" not in fired, (
        "le plafond voix a écrasé le démenti du proche : le score retombe sous 85"
    )
    assert harness.last_score >= 85
