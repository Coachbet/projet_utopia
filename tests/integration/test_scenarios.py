"""Les scénarios écrits deviennent une suite de non-régression.

On câble les vrais composants hors-ligne — vraie session d'appel, vrai moteur de
mots-clés, vrai carnet d'adresses, vraie logique de réputation (Lookup coupé) — et
seule l'analyse de contenu est remplacée par une doublure, pour que le test soit
déterministe, gratuit et sans réseau.

Conséquence à garder en tête : les bornes `expect` des scénarios sont calibrées
sur cette doublure, pas sur Claude. Pour la calibration réelle :
`uv run python scripts/replay_scenario.py scenarios/*.json --real-llm`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vocalguard.application.scoring import ScoringPipeline
from vocalguard.application.sessions import SessionRegistry
from vocalguard.config import Settings
from vocalguard.domain.events import Event
from vocalguard.domain.models import TranscriptSegment
from vocalguard.infrastructure.contacts.book import JsonContactBook
from vocalguard.infrastructure.fakes.doubles import FakeContentAnalyzer
from vocalguard.infrastructure.fakes.event_sink import FanOutSink, InMemoryEventSink
from vocalguard.infrastructure.fakes.transcriber import FakeTranscriber
from vocalguard.infrastructure.keywords.detector import KeywordDetector
from vocalguard.infrastructure.telephony.reputation import TwilioReputationChecker

ROOT = Path(__file__).resolve().parents[2]

# Le carnet d'exemple, pas `data/contacts.json` : ce dernier est dans
# `.gitignore` (numéros réels), donc absent chez les coéquipiers et en CI.
CONTACTS = ROOT / "data" / "contacts.example.json"

# Chargés à l'import : aucune I/O dans les tests, et un scénario illisible fait
# échouer la collecte au lieu d'un test au hasard.
SCENARIOS = {
    path.stem: json.loads(path.read_text(encoding="utf-8"))
    for path in sorted((ROOT / "scenarios").glob("*.json"))
}


class DeferredSink:
    """Casse le cycle registre -> pipeline -> bus.

    Le registre exige son sink à la construction, le pipeline exige le registre,
    et le bus doit contenir le pipeline. En production `create_app` construit le
    `FanOutSink` en dernier et n'a pas ce souci.
    """

    def __init__(self) -> None:
        self.inner: FanOutSink | None = None

    async def emit(self, event: Event) -> None:
        assert self.inner is not None, "bus non câblé"
        await self.inner.emit(event)


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_scenario_matches_its_expectations(name: str):
    scenario = SCENARIOS[name]
    expect = scenario.get("expect")
    if not expect:
        pytest.skip(f"{name} ne déclare pas de bloc `expect`")

    settings = Settings(
        _env_file=None,
        twilio_lookup_enabled=False,
        contacts_path=str(CONTACTS),
    )
    contacts = JsonContactBook.from_path(settings.contacts_path)

    recorder = InMemoryEventSink()
    bus = DeferredSink()
    registry = SessionRegistry(transcriber_factory=FakeTranscriber, sink=bus, settings=settings)
    pipeline = ScoringPipeline(
        registry=registry,
        keyword_engine=KeywordDetector(),
        content_analyzer=FakeContentAnalyzer(),
        reputation_checker=TwilioReputationChecker(contacts=contacts, settings=settings),
        settings=settings,
    )
    # Le pipeline en dernier : ses événements arrivent après le transcript qui
    # les a provoqués, comme dans `create_app`.
    bus.inner = FanOutSink(recorder, pipeline)

    call_id = scenario.get("call_id", "CA_TEST")
    session = await registry.open(call_id, caller_number=scenario.get("caller_number", ""))

    for turn in scenario["turns"]:
        await session.on_segment(
            TranscriptSegment(
                speaker=turn.get("speaker", "caller"),
                text=turn["text"],
                t=float(turn["t"]),
                is_final=True,
            )
        )
        # Laisse l'analyse de contenu, qui tourne en tâche de fond, se poser
        # avant le tour suivant.
        await pipeline.aclose()
    await pipeline.aclose()

    scores = [e.data["score"] for e in recorder.by_type("score")]
    assert scores, "aucun événement de score produit"
    peak = max(scores)
    level = recorder.by_type("score")[-1].data["level"]

    if "min_score" in expect:
        assert peak >= expect["min_score"], f"pic {peak} trop bas"
    if "max_score" in expect:
        assert peak <= expect["max_score"], f"pic {peak} trop haut — faux positif"
    if "level" in expect:
        assert level == expect["level"]


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_scenario_file_is_well_formed(name: str):
    """Garde-fou pour Biz 1 : un scénario mal formé doit échouer ici, pas en démo."""
    scenario = SCENARIOS[name]
    assert scenario.get("turns"), "pas de tours de parole"
    previous = -1.0
    for i, turn in enumerate(scenario["turns"]):
        assert turn.get("text", "").strip(), f"tour {i} sans texte"
        assert turn.get("speaker") in {"caller", "victim"}, f"tour {i} : locuteur invalide"
        t = float(turn["t"])
        assert t >= previous, f"tour {i} : horodatage non croissant"
        previous = t


def test_a_legitimate_scenario_exists():
    """Sans test de faux positif, la calibration ne veut rien dire."""
    expectations = [s.get("expect", {}) for s in SCENARIOS.values()]
    assert any("max_score" in e for e in expectations), "aucun scénario légitime déclaré"


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_caller_number_matches_the_scenario_premise(name: str):
    """Un scénario légitime doit appeler depuis un numéro connu, un scénario
    d'arnaque depuis un numéro inconnu.

    Sans ce garde-fou, un appel « légitime » passe sous le seuil pour la
    mauvaise raison — on croit tester un faux positif alors qu'on teste un
    imposteur dont seul le contenu est anodin.
    """
    scenario = SCENARIOS[name]
    number = scenario.get("caller_number")
    if not number:
        pytest.skip(f"{name} ne déclare pas de numéro appelant")

    book = JsonContactBook.from_path(CONTACTS)
    known = book.by_number(number) is not None
    legitimate = "max_score" in scenario.get("expect", {})

    if legitimate:
        assert known, f"{name} est légitime mais {number} est absent du carnet"
    else:
        assert not known, f"{name} est une arnaque mais {number} est un contact connu"
