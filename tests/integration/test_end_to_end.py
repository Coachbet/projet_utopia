"""Les deux moitiés branchées ensemble, par `create_app`.

C'est le seul test qui prouve que l'intégration tient. Les suites de Dev 1 et de
Dev 2 passaient toutes les deux alors que leurs contrats avaient divergé : chacune
testait sa moitié contre ses propres doublures. Ici on part de l'application
réelle et on vérifie la chaîne entière — un transcript entre, une action Twilio
sort.

Deux niveaux volontairement distincts :

  - un balayage paramétré sur tous les scénarios qui déclarent un bloc `expect`,
    pour qu'un scénario ajouté demain ne reste pas silencieusement hors couverture ;
  - des tests nommés, qui vérifient ce qu'un balayage ne peut pas voir — quel
    coupe-circuit s'est armé, et dans quel ORDRE les actions sont parties.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vocalguard.config import Settings
from vocalguard.domain.events import EventType
from vocalguard.domain.models import ContentSignal, TranscriptSegment
from vocalguard.infrastructure.fakes import doubles
from vocalguard.infrastructure.fakes.event_sink import InMemoryEventSink
from vocalguard.interface.app import create_app

ROOT = Path(__file__).resolve().parents[2]

# Chargés à l'import : un scénario illisible casse la collecte, et non un test
# au hasard plus tard.
SCENARIOS = {
    path.name: json.loads(path.read_text(encoding="utf-8"))
    for path in sorted((ROOT / "scenarios").glob("*.json"))
}

# `_smoke_technique.json` ne déclare pas d'`expect` : il sert à vérifier la
# plomberie, pas le calibrage, donc il n'a rien à affirmer ici.
WITH_EXPECT = sorted(name for name, s in SCENARIOS.items() if s.get("expect"))


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


async def _drive(app, scenario: dict) -> list:
    """Joue un scénario dans une application déjà câblée.

    Séparé de `_play` pour que les tests qui doivent substituer un adapter
    puissent construire leur propre `create_app` sans dupliquer la boucle.
    """
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
    return list(getattr(controller, "calls", []))


async def _play(scenario_name: str) -> tuple[InMemoryEventSink, list]:
    observed = InMemoryEventSink()
    app = create_app(_settings(), sink=observed)
    controller_calls = await _drive(app, SCENARIOS[scenario_name])
    return observed, controller_calls


def _scores(sink: InMemoryEventSink) -> list[dict]:
    return [e.data for e in sink.by_type(EventType.SCORE)]


def _kinds(controller_calls: list) -> list[str]:
    return [name for name, *_ in controller_calls]


def _breakers(sink: InMemoryEventSink) -> set[str]:
    fired: set[str] = set()
    for score in _scores(sink):
        fired.update(score.get("breakers_fired", []))
    return fired


# --- Balayage : aucun scénario ne reste hors couverture ---------------------


@pytest.mark.parametrize("name", WITH_EXPECT)
async def test_scenario_expectations_hold_through_the_real_app(name: str):
    """Le même contrat que `test_scenarios.py`, mais via `create_app`.

    L'autre suite câble le pipeline à la main ; ici on passe par le vrai
    assemblage, `InterventionRunner` compris. Un écart entre les deux voudrait
    dire que le câblage ajoute ou perd quelque chose.
    """
    expect = SCENARIOS[name]["expect"]
    sink, _ = await _play(name)

    scores = _scores(sink)
    assert scores, "aucun score produit : les deux moitiés ne se parlent pas"
    peak = max(s["score"] for s in scores)

    if "min_score" in expect:
        assert peak >= expect["min_score"], f"{name} : pic {peak} trop bas"
    if "max_score" in expect:
        assert peak <= expect["max_score"], f"{name} : pic {peak} trop haut — faux positif"
    if "level" in expect:
        assert scores[-1]["level"] == expect["level"]


def test_every_calibration_scenario_declares_expectations():
    """Garde-fou : un scénario sans `expect` n'affirme rien et passerait inaperçu."""
    silent = sorted(set(SCENARIOS) - set(WITH_EXPECT) - {"_smoke_technique.json"})
    assert not silent, f"scénarios sans bloc `expect` : {silent}"


# --- Arnaque au faux petit-fils --------------------------------------------


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
    kinds = _kinds(controller_calls)
    assert "hold_caller" in kinds
    assert "announce_to_victim" in kinds
    assert kinds.index("hold_caller") < kinds.index("announce_to_victim")
    assert "send_sms" in kinds


async def test_identity_mismatch_is_detected_through_the_whole_chain():
    """Le coupe-circuit le plus fort dépend de deux moitiés à la fois : Claude
    fournit l'identité revendiquée, le carnet d'adresses fournit le numéro."""
    sink, _ = await _play("scam_01_petit_fils.json")

    reputations = [e.data for e in sink.by_type(EventType.REPUTATION)]
    assert any(r["identity_mismatch"] for r in reputations)
    assert "payment_request_with_identity_mismatch" in _breakers(sink)


async def test_actions_fire_once_even_though_the_score_stays_high():
    """Le score reste à 85 sur les derniers tours : la victime ne doit pas
    entendre l'annonce à chaque segment."""
    _, controller_calls = await _play("scam_01_petit_fils.json")
    kinds = _kinds(controller_calls)
    assert kinds.count("announce_to_victim") == 1
    assert kinds.count("hold_caller") == 1
    assert kinds.count("send_sms") == 1


# --- Arnaque au faux conseiller bancaire -----------------------------------


async def test_bank_scam_reaches_intervention_by_the_institutional_route():
    """Même issue que le faux petit-fils, par un chemin différent.

    L'identité revendiquée est une institution : « Banque Crédit Mutuel » est au
    carnet d'adresses, donc `identity_mismatch` s'arme en reconnaissant une
    RELATION (« banque », « conseiller bancaire ») et non un prénom. C'est le
    seul test qui prouve que l'identité institutionnelle traverse toute la
    chaîne, et pas seulement le moteur de score.
    """
    sink, controller_calls = await _play("scam_02_faux_conseiller_bancaire.json")

    scores = _scores(sink)
    assert max(s["score"] for s in scores) >= 85
    assert scores[-1]["level"] == "intervene"
    assert "payment_request_with_identity_mismatch" in _breakers(sink)

    reputations = [e.data for e in sink.by_type(EventType.REPUTATION)]
    assert any(r["identity_mismatch"] for r in reputations)
    assert any(
        "Banque Crédit Mutuel" in reason for r in reputations for reason in r.get("reasons", [])
    ), "le rapprochement n'a pas désigné la banque : l'identité institutionnelle a échoué"

    kinds = _kinds(controller_calls)
    assert kinds.index("hold_caller") < kinds.index("announce_to_victim")
    assert "send_sms" in kinds


# --- Faux positifs ---------------------------------------------------------


async def test_legitimate_call_triggers_nothing_audible():
    """Le faux positif est le risque qui tuerait le produit."""
    sink, controller_calls = await _play("legit_01_famille.json")

    scores = _scores(sink)
    assert scores
    assert max(s["score"] for s in scores) < 40
    assert scores[-1]["level"] == "none"
    assert sink.by_type(EventType.ACTION) == []
    assert controller_calls == []


async def test_hardest_legitimate_call_stays_silent():
    """La vraie fille, depuis son vrai numéro, avec une urgence réelle.

    Hôpital, larmes, argent déjà avancé, « tout de suite » : tout ce qu'un
    moteur de mots-clés déteste. C'est l'assertion la plus précieuse du fichier,
    celle qui échoue bruyamment si quelqu'un élargit le lexique ou remonte les
    poids un peu trop loin.
    """
    sink, controller_calls = await _play("legit_02_fille_urgence_reelle.json")

    scores = _scores(sink)
    assert scores
    assert max(s["score"] for s in scores) < 40
    assert scores[-1]["level"] == "none"
    assert _breakers(sink) == set()
    assert sink.by_type(EventType.ACTION) == []
    assert controller_calls == [], "un vrai appel de famille a déclenché une action"


# --- Fausse autorité -------------------------------------------------------


async def test_false_authority_scam_offline_only_reaches_watch():
    """Mesure HORS LIGNE, et c'est volontaire : elle diverge du réel.

    La doublure `FakeContentAnalyzer` ne donne que 0,66 de risque sur ce
    scénario, sous le seuil de confiance de 0,85 qu'exige
    `false_authority_with_payment`. Le pic reste donc à 52 / « vigilance », sans
    aucun coupe-circuit ni action audible.

    Avec le vrai Claude le même scénario atteint 85 / « intervention » (risque
    0,97, vérifié à la main) :

        scripts/replay_scenario.py scenarios/scam_03_fausse_autorite.json --real-llm

    Affirmer 85 ici serait vert grâce à la doublure et faux sur le fond. Le
    coupe-circuit est couvert de bout en bout par le test suivant.
    """
    sink, controller_calls = await _play("scam_03_fausse_autorite.json")

    scores = _scores(sink)
    assert scores[-1]["level"] == "watch"
    assert max(s["score"] for s in scores) < 85
    assert "false_authority_with_payment" not in _breakers(sink)
    assert controller_calls == []


async def test_false_authority_breaker_fires_end_to_end(monkeypatch):
    """Le coupe-circuit le plus récent, dans l'application réelle.

    On rejoue ce que le vrai Claude a répondu sur ce scénario (risque 0,97,
    « fausse autorité » + « paiement inhabituel ») en substituant l'analyseur de
    contenu que `build_pipeline` choisit en mode hors ligne. Tout le reste est le
    vrai câblage : `InterventionRunner`, le contrôleur, le bus.

    Sans ce test, notre coupe-circuit le moins éprouvé n'aurait aucune
    couverture d'intégration — l'archétype gendarmerie / impôts ne peut pas
    l'atteindre hors ligne.
    """
    observed_claude = ContentSignal(
        risk=0.97,
        tactics=(
            "urgence",
            "secret",
            "paiement inhabituel",
            "fausse autorité",
            "identité revendiquée",
        ),
        claimed_identity="Brigadier Mercier, de la gendarmerie nationale",
        explanation=("L'appelant se fait passer pour un gendarme et réclame un virement urgent."),
    )

    class _ReplayClaude:
        """`ContentAnalyzer` qui rejoue une réponse mesurée."""

        async def analyze(self, window):
            # `None` sur une fenêtre vide : c'est le contrat du port, et le
            # respecter évite de tester une sémantique qu'on n'a pas.
            return observed_claude if window else None

    # `build_pipeline` importe la doublure à l'appel, pas à l'import du module :
    # remplacer l'attribut suffit, sans toucher aux internes du pipeline.
    monkeypatch.setattr(doubles, "FakeContentAnalyzer", _ReplayClaude)

    observed = InMemoryEventSink()
    app = create_app(_settings(), sink=observed)
    controller_calls = await _drive(app, SCENARIOS["scam_03_fausse_autorite.json"])

    scores = _scores(observed)
    assert "false_authority_with_payment" in _breakers(observed), (
        "le coupe-circuit fausse autorité ne s'est pas armé malgré la conjonction"
    )
    assert max(s["score"] for s in scores) >= 85
    assert scores[-1]["level"] == "intervene"

    kinds = _kinds(controller_calls)
    assert "hold_caller" in kinds
    assert kinds.index("hold_caller") < kinds.index("announce_to_victim")


# --- Câblage ---------------------------------------------------------------


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
