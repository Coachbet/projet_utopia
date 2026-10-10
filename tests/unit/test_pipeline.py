"""Le pipeline, bout en bout, sans téléphone ni AWS."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from vocalguard.application.pipeline import ScoringPipeline
from vocalguard.application.session import SessionRegistry
from vocalguard.domain.events import Event, EventType
from vocalguard.domain.models import (
    Contact,
    ContentSignal,
    KeywordHit,
    KeywordSignal,
    LineType,
    TranscriptSegment,
)
from vocalguard.infrastructure.fakes.doubles import (
    FakeContentAnalyzer,
    FakeReputationChecker,
    FakeVoiceDetector,
    InMemoryEventSink,
    RecordingCallController,
    StaticContactBook,
)


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


class ScriptedAnalyzer:
    """Renvoie un signal imposé, pour piloter le score depuis le test."""

    def __init__(self, signal: ContentSignal | None) -> None:
        self.signal = signal
        self.calls = 0

    async def analyze(self, turns: Sequence[TranscriptSegment]) -> ContentSignal | None:
        self.calls += 1
        return self.signal


def seg(text: str, *, speaker: str = "caller", t: float = 1.0, final: bool = True):
    return TranscriptSegment(speaker=speaker, text=text, t=t, is_final=final)


def build(**kwargs) -> tuple[ScoringPipeline, InMemoryEventSink, RecordingCallController]:
    sink = InMemoryEventSink()
    controller = RecordingCallController()
    kwargs.setdefault("keyword_engine", StubKeywords())
    pipeline = ScoringPipeline(
        event_sink=sink,
        call_controller=controller,
        trusted_contact_number="+33600000000",
        **kwargs,
    )
    return pipeline, sink, controller


async def test_final_segment_emits_keyword_and_score():
    pipeline, sink, _ = build(keyword_engine=StubKeywords(0.3, ("virement",)))
    await pipeline.on_transcript("CA1", seg("il faut faire un virement"))

    assert [e.type for e in sink.events] == [EventType.KEYWORD, EventType.SCORE]
    assert sink.of_type("keyword")[0].data["terms"] == ["virement"]
    assert sink.last_level == "none"


async def test_partial_segment_is_ignored():
    pipeline, sink, _ = build()
    await pipeline.on_transcript("CA1", seg("mamie c'est", final=False))
    assert sink.events == []


async def test_events_carry_call_id_and_elapsed_time():
    pipeline, sink, _ = build()
    await pipeline.on_transcript("CA42", seg("bonjour"))
    assert all(e.call_id == "CA42" for e in sink.events)
    assert all(e.t >= 0 for e in sink.events)


async def test_injected_registry_drives_event_timestamps():
    """Un registre vide est falsy : il doit quand même être utilisé.

    Régression : `registry or SessionRegistry()` jetait l'injection, et tous les
    événements repartaient de l'horloge monotone — timestamps faux sur le
    dashboard et dans les rejeux de scénario.
    """

    class ManualClock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    clock = ManualClock()
    sink = InMemoryEventSink()
    pipeline = ScoringPipeline(
        event_sink=sink,
        keyword_engine=StubKeywords(),
        registry=SessionRegistry(clock=clock),
    )
    await pipeline.start_call("CA1")
    clock.value = 7.0
    await pipeline.on_transcript("CA1", seg("virement urgent", t=7.0))

    assert [e.t for e in sink.events] == [7.0, 7.0]


async def test_llm_runs_in_background_and_rescores():
    analyzer = ScriptedAnalyzer(
        ContentSignal(
            risk=0.95,
            tactics=("urgence", "paiement inhabituel"),
            claimed_identity="Lucas, petit-fils",
            explanation="L'appelant réclame un virement immédiat.",
        )
    )
    pipeline, sink, controller = build(content_analyzer=analyzer)

    await pipeline.on_transcript("CA1", seg("mamie envoie un virement tout de suite"))
    # Le chemin rapide a scoré et rendu la main avant même que Claude démarre :
    # c'est la preuve que l'analyse de contenu ne bloque pas l'arrivée du flux.
    assert analyzer.calls == 0
    assert sink.of_type("score")

    await pipeline.aclose()
    assert analyzer.calls == 1

    llm_events = sink.of_type("llm")
    assert len(llm_events) == 1
    assert llm_events[0].data["claimed_identity"] == "Lucas, petit-fils"
    # Coupe-circuit « llm_high_confidence » : risque 0.95 -> au moins 65.
    assert sink.last_score >= 65
    assert sink.last_level in {"alert", "intervene"}
    assert any(kind == "announce" for kind, _, _ in controller.calls)


async def test_only_one_llm_call_in_flight():
    analyzer = ScriptedAnalyzer(None)
    pipeline, _, _ = build(content_analyzer=analyzer)
    for i in range(5):
        await pipeline.on_transcript("CA1", seg(f"phrase {i}", t=float(i)))
    await pipeline.aclose()
    assert analyzer.calls <= 5


async def test_intervention_holds_caller_then_announces():
    analyzer = ScriptedAnalyzer(
        ContentSignal(
            risk=0.95,
            tactics=("urgence", "paiement inhabituel"),
            claimed_identity="Lucas",
            explanation="Virement immédiat réclamé.",
        )
    )
    pipeline, sink, controller = build(
        content_analyzer=analyzer,
        reputation_checker=FakeReputationChecker(
            risk=0.9, identity_mismatch=True, reasons=("numéro inconnu",)
        ),
        contact_book=StaticContactBook(
            [Contact(name="Lucas", phone_number="+33611111111", relation="petit-fils")]
        ),
    )

    await pipeline.start_call("CA1", caller_number="+33755555555")
    await pipeline.on_transcript("CA1", seg("mamie il faut un virement tout de suite"))
    await pipeline.aclose()

    # Paiement + identité qui ne colle pas au numéro -> plancher à 85.
    assert sink.last_score >= 85
    assert sink.last_level == "intervene"
    kinds = [kind for kind, _, _ in controller.calls]
    assert kinds.index("hold") < kinds.index("announce")
    assert "sms" in kinds
    assert "Lucas" in next(m for k, _, m in controller.calls if k == "announce")


async def test_actions_do_not_repeat_when_score_oscillates():
    pipeline, _sink, controller = build(
        content_analyzer=ScriptedAnalyzer(
            ContentSignal(risk=0.95, tactics=("urgence",), explanation="x")
        )
    )
    for i in range(3):
        await pipeline.on_transcript("CA1", seg(f"urgence {i}", t=float(i)))
        await pipeline.aclose()

    announces = [k for k, _, _ in controller.calls if k == "announce"]
    sms = [k for k, _, _ in controller.calls if k == "sms"]
    assert len(announces) == 1
    assert len(sms) == 1


async def test_identity_discovery_triggers_reputation_recheck():
    class CountingReputation(FakeReputationChecker):
        def __init__(self) -> None:
            super().__init__(risk=0.5, identity_mismatch=True)
            self.identities: list[str | None] = []

        async def check(self, phone_number, claimed_identity=None):
            self.identities.append(claimed_identity)
            return await super().check(phone_number, claimed_identity)

    reputation = CountingReputation()
    pipeline, sink, _ = build(
        content_analyzer=ScriptedAnalyzer(
            ContentSignal(risk=0.5, tactics=("urgence",), claimed_identity="Lucas")
        ),
        reputation_checker=reputation,
    )

    await pipeline.start_call("CA1", caller_number="+33755555555")
    await pipeline.on_transcript("CA1", seg("mamie c'est Lucas"))
    await pipeline.aclose()

    # Une fois sans identité au décroché, une fois quand Claude la révèle.
    assert reputation.identities == [None, "Lucas"]
    assert sink.of_type("reputation")[-1].data["identity_mismatch"] is True


async def test_voice_alone_never_triggers_intervention():
    pipeline, sink, controller = build(voice_detector=FakeVoiceDetector(risk=1.0))
    await pipeline.on_audio("CA1", "caller", b"\x00\x00" * 100, 8000)

    assert sink.of_type("voice")[0].data["risk"] == 1.0
    assert sink.last_score <= 64
    assert sink.last_level != "intervene"
    assert controller.calls == []


async def test_victim_audio_is_not_analysed():
    pipeline, sink, _ = build(voice_detector=FakeVoiceDetector(risk=1.0))
    await pipeline.on_audio("CA1", "victim", b"\x00\x00" * 100, 8000)
    assert sink.events == []


async def test_handle_event_accepts_contract_transcript():
    pipeline, sink, _ = build(keyword_engine=StubKeywords(0.2, ("urgence",)))
    await pipeline.handle_event(
        Event(
            call_id="CA9",
            t=12.4,
            type=EventType.TRANSCRIPT,
            speaker="caller",
            data={"text": "il faut m'envoyer de l'argent", "is_final": True},
        )
    )
    assert sink.of_type("score")
    assert sink.of_type("keyword")


async def test_fake_analyzer_detects_the_demo_scenario():
    """La doublure doit produire les tactiques qui arment les coupe-circuits."""
    analyzer = FakeContentAnalyzer()
    signal = await analyzer.analyze(
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
    analyzer = FakeContentAnalyzer()
    signal = await analyzer.analyze([seg("je passe dimanche, j'apporte le gâteau")])
    assert signal is not None
    assert signal.risk == 0.0


@pytest.mark.parametrize("unknown_type", [EventType.SCORE, EventType.ACTION])
async def test_pipeline_ignores_its_own_events(unknown_type):
    """Rebrancher le sink en entrée ne doit pas créer de boucle."""
    pipeline, sink, _ = build()
    await pipeline.handle_event(Event(call_id="CA1", t=1.0, type=unknown_type, data={"score": 90}))
    assert sink.events == []


async def test_reputation_without_caller_number_is_skipped():
    reputation = FakeReputationChecker()
    pipeline, sink, _ = build(reputation_checker=reputation)
    await pipeline.start_call("CA1")
    assert sink.of_type("reputation") == []


async def test_line_type_is_published_for_the_dashboard():
    pipeline, sink, _ = build(
        reputation_checker=FakeReputationChecker(
            risk=0.6, line_type=LineType.NON_FIXED_VOIP, reasons=("ligne VoIP non fixe",)
        )
    )
    await pipeline.start_call("CA1", caller_number="+33755555555")
    data = sink.of_type("reputation")[0].data
    assert data["line_type"] == "nonFixedVoip"
    assert data["reasons"] == ["ligne VoIP non fixe"]
