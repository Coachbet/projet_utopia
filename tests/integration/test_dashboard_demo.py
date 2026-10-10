"""Le mode démo du dashboard doit dire la vérité.

`?demo=1` rejoue une séquence écrite en dur côté navigateur, que Biz 1 va
retoucher pour le pitch. Si ses scores ne sont plus ceux que le vrai moteur
calculerait, le dashboard affiche un chiffre que le produit ne produirait
jamais — et un juré qui recoupe avec le tableau des poids le voit.

On extrait le tableau avec node (la syntaxe est du JavaScript, pas du JSON) et
on le repasse dans le moteur de score réel.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from vocalguard.domain.models import (
    ContentSignal,
    KeywordHit,
    KeywordSignal,
    LineType,
    ReputationSignal,
    SignalSet,
    VoiceSignal,
)
from vocalguard.domain.scoring.engine import assess

DASHBOARD = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "vocalguard"
    / "interface"
    / "static"
    / "dashboard.html"
)


def _demo_events() -> list[dict]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node absent : extraction du tableau JavaScript impossible")

    html = DASHBOARD.read_text(encoding="utf-8")
    match = re.search(r"const DEMO_EVENTS = (\[.*?\n\]);", html, re.S)
    assert match, "tableau DEMO_EVENTS introuvable dans le dashboard"

    result = subprocess.run(
        [node, "-e", f"console.log(JSON.stringify({match.group(1)}))"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)


def _apply(signals: SignalSet, event: dict) -> None:
    d = event.get("data", {})
    match event["type"]:
        case "llm":
            signals.content = ContentSignal(
                risk=d["risk"],
                tactics=tuple(d.get("tactics", ())),
                claimed_identity=d.get("claimed_identity"),
                explanation=d.get("explanation", ""),
            )
        case "keyword":
            signals.keywords = KeywordSignal(
                risk=d["risk"],
                hits=tuple(
                    KeywordHit(term=t, weight=3, category="demo") for t in d.get("terms", ())
                ),
            )
        case "voice":
            signals.voice = VoiceSignal(
                risk=d["risk"], windows_analyzed=d.get("windows_analyzed", 0)
            )
        case "reputation":
            signals.reputation = ReputationSignal(
                risk=d["risk"],
                line_type=LineType(d.get("line_type", "unknown")),
                in_contacts=d.get("in_contacts", False),
                identity_mismatch=d.get("identity_mismatch", False),
                reasons=tuple(d.get("reasons", ())),
            )


def test_demo_scores_match_the_real_scoring_engine():
    events = _demo_events()
    signals = SignalSet()
    checked = 0

    for event in events:
        if event["type"] != "score":
            _apply(signals, event)
            continue

        expected = assess(signals)
        announced = event["data"]
        assert announced["score"] == expected.score, (
            f"t={event['t']} : la démo annonce {announced['score']}, "
            f"le moteur calcule {expected.score}"
        )
        assert announced["level"] == expected.level.wire
        checked += 1

    assert checked >= 4, "trop peu d'étapes de score dans la démo pour être convaincant"


def test_demo_ends_in_an_intervention():
    """C'est la chute de la démo : sans elle, il n'y a rien à montrer."""
    events = _demo_events()
    scores = [e for e in events if e["type"] == "score"]
    assert scores, "aucun score dans la séquence de démo"
    assert scores[-1]["data"]["level"] == "intervene"
    assert any(e["type"] == "action" for e in events), "aucune action visible"


def test_demo_events_respect_the_event_contract():
    valid = {"transcript", "keyword", "llm", "voice", "reputation", "score", "action"}
    previous_t = -1.0
    for event in _demo_events():
        assert event["type"] in valid, f"type hors contrat : {event['type']}"
        assert event.get("speaker") in {"caller", "victim", None}
        t = float(event["t"])
        assert t >= previous_t, f"horodatage non croissant à t={t}"
        previous_t = t
