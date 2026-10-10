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

import logging
import sys
from collections.abc import Sequence

import pytest

from vocalguard.application.scoring import ScoringPipeline, build_pipeline
from vocalguard.application.sessions import CallSession, SessionRegistry
from vocalguard.config import Settings
from vocalguard.domain.events import Event, EventType
from vocalguard.domain.models import (
    ContentSignal,
    KeywordHit,
    KeywordSignal,
    LineType,
    TranscriptSegment,
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
