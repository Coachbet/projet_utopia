"""Les deux moitiés branchées ensemble, par `create_app`.

C'est le seul test qui prouve que l'intégration tient. Les suites de Dev 1 et de
Dev 2 passaient toutes les deux alors que leurs contrats avaient divergé : chacune
testait sa moitié contre ses propres doublures. Ici on part de l'application
réelle et on vérifie la chaîne entière — un transcript entre, une action Twilio
sort.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vocalguard.config import Settings
from vocalguard.domain.events import EventType
from vocalguard.domain.models import TranscriptSegment
from vocalguard.infrastructure.fakes.event_sink import InMemoryEventSink
from vocalguard.interface.app import create_app

ROOT = Path(__file__).resolve().parents[2]


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        # `fake` coupe à la fois Transcribe et Bedrock : aucun appel réseau,
        # aucun crédit dépensé, résultat déterministe.
        transcribe_backend="fake",
        contacts_path=ROOT / "data" / "contacts.example.json",
        trusted_contact_phone_number="+33600000000",
        twilio_lookup_enabled=False,
    )


async def _play(scenario_name: str) -> tuple[InMemoryEventSink, list]:
    scenario = json.loads(
        (ROOT / "scenarios" / scenario_name).read_text(encoding="utf-8")
    )
    observed = InMemoryEventSink()
    app = create_app(_settings(), sink=observed)

    registry = app.state.registry
    pipeline = app.state.pipeline
    controller = app.state.controller

    session = await registry.open(
        scenario["call_id"], caller_number=scenario.get("caller_number", "")
    )
    for turn in scenario["turns"]:
        await session.on_segment(
            TranscriptSegment(
                speaker=turn["speaker"],
                text=turn["text"],
                t=float(turn["t"]),
                is_final=True,
            )
        )
        # L'analyse de contenu tourne en tâche de fond : sans ça on lirait le
        # score d'avant.
        await pipeline.aclose()

    await registry.close(scenario["call_id"])
    return observed, list(getattr(controller, "calls", []))


def _scores(sink: InMemoryEventSink) -> list[dict]:
    return [e.data for e in sink.by_type(EventType.SCORE)]


async def test_scam_call_escalates_to_intervention_and_acts():
    sink, controller_calls = await _play("scam_01_petit_fils.json")

    scores = _scores(sink)
    assert scores, "aucun score produit : les deux moitiés ne se parlent pas"
    assert max(s["score"] for s in scores) >= 85
    assert scores[-1]["level"] == "intervene"

    # Les quatre familles d'événements de Dev 2 doivent circuler.
    for type_ in (EventType.KEYWORD, EventType.LLM, EventType.REPUTATION):
        assert sink.by_type(type_), f"aucun événement {type_.value}"

    # Et Dev 1 doit avoir agi, dans l'ordre : le scammeur en attente AVANT
    # l'annonce, pour qu'il ne puisse pas l'entendre.
    kinds = [name for name, *_ in controller_calls]
    assert "hold_caller" in kinds
    assert "announce_to_victim" in kinds
    assert kinds.index("hold_caller") < kinds.index("announce_to_victim")
    assert "send_sms" in kinds


async def test_legitimate_call_triggers_nothing_audible():
    """Le faux positif est le risque qui tuerait le produit."""
    sink, controller_calls = await _play("legit_01_famille.json")

    scores = _scores(sink)
    assert scores
    assert max(s["score"] for s in scores) < 40
    assert scores[-1]["level"] == "none"
    assert sink.by_type(EventType.ACTION) == []
    assert controller_calls == []


async def test_identity_mismatch_is_detected_through_the_whole_chain():
    """Le coupe-circuit le plus fort dépend de deux moitiés à la fois : Claude
    fournit l'identité revendiquée, le carnet d'adresses fournit le numéro."""
    sink, _ = await _play("scam_01_petit_fils.json")

    reputations = [e.data for e in sink.by_type(EventType.REPUTATION)]
    assert any(r["identity_mismatch"] for r in reputations)

    scores = _scores(sink)
    assert any(
        "payment_request_with_identity_mismatch" in s.get("breakers_fired", [])
        for s in scores
    )


async def test_actions_fire_once_even_though_the_score_stays_high():
    """Le score reste à 85 sur les derniers tours : la victime ne doit pas
    entendre l'annonce à chaque segment."""
    _, controller_calls = await _play("scam_01_petit_fils.json")
    kinds = [name for name, *_ in controller_calls]
    assert kinds.count("announce_to_victim") == 1
    assert kinds.count("hold_caller") == 1
    assert kinds.count("send_sms") == 1


async def test_dashboard_hub_receives_the_whole_call():
    """Le hub est abonné par `_subscribe_dashboard` : sans ça la page se
    connecte et ne reçoit jamais rien."""
    from vocalguard.interface.ws.dashboard import HUB

    HUB.clear()
    await _play("scam_01_petit_fils.json")

    types = {e["type"] for e in HUB.replay()}
    assert {"transcript", "keyword", "score", "action"} <= types
    HUB.clear()


@pytest.mark.parametrize("backend", ["fake"])
async def test_app_boots_without_any_credential(backend: str):
    app = create_app(Settings(_env_file=None, transcribe_backend=backend))
    assert app.state.pipeline is not None
    assert app.state.intervention is not None
    # L'intervention et le pipeline doivent être abonnés, sinon rien ne se passe.
    subscribed = {type(s).__name__ for s in app.state.sink._sinks}
    assert "InterventionRunner" in subscribed
    assert "ScoringPipeline" in subscribed
