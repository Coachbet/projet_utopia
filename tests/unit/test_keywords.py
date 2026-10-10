"""Tests du moteur de mots-clés.

L'enjeu principal est la calibration : `scan_call` >= 0,6 déclenche le
coupe-circuit `keywords_alone_watch`, qui force le score à 40 quand Claude n'a
pas encore répondu. Un faux positif ici allume le dashboard sur un vrai appel.
"""

from __future__ import annotations

import json

import pytest

from vocalguard.domain.events import Speaker
from vocalguard.domain.models import KeywordHit, TranscriptSegment
from vocalguard.domain.scoring.circuit_breakers import KEYWORDS_ALONE_TRIGGER
from vocalguard.infrastructure.keywords.detector import KeywordDetector, normalize
from vocalguard.infrastructure.keywords.lexicon import (
    CATEGORIES,
    LEXICON,
    load_lexicon,
)


@pytest.fixture
def detector() -> KeywordDetector:
    return KeywordDetector()


def turn(text: str, *, speaker: Speaker = "caller", t: float = 0.0, final: bool = True):
    return TranscriptSegment(speaker=speaker, text=text, t=t, is_final=final)


# --- Lexique ---------------------------------------------------------------


def test_lexicon_is_well_formed():
    assert len(LEXICON) >= 70
    for hit in LEXICON:
        assert hit.term.strip() == hit.term
        assert 1 <= hit.weight <= 5
        assert hit.category in CATEGORIES


def test_lexicon_has_no_duplicate_terms():
    terms = [h.term.casefold() for h in LEXICON]
    assert len(terms) == len(set(terms))


# --- Normalisation : accents et casse --------------------------------------


def test_normalize_strips_accents_and_case():
    assert normalize("Ça") == "ca"
    assert normalize("PROBLÈME") == "probleme"
    assert normalize("d’accès") == "d'acces"  # noqa: RUF001 — apostrophe courbe voulue


@pytest.mark.parametrize(
    "text",
    [
        "carte cadeau",
        "CARTE CADEAU",
        "Carte Cadeau",
        "cartes cadeaux",  # pluriel français en -x
        "cartes cadeau",
    ],
)
def test_matching_is_case_accent_and_plural_insensitive(detector, text):
    assert detector.scan(text).terms == ("carte cadeau",)


@pytest.mark.parametrize("text", ["grand-mère", "grand mère", "grand-mere", "GRAND MERE"])
def test_hyphen_is_optional(detector, text):
    assert "grand-mère" in detector.scan(text).terms


def test_accented_term_matches_unaccented_asr_output(detector):
    """Transcribe laisse tomber des accents : « immediatement » doit matcher."""
    assert detector.scan("venez immediatement").terms == ("immédiatement",)


# --- Comptage : distinct, pas cumulatif ------------------------------------


def test_repeats_of_same_term_do_not_stack(detector):
    once = detector.scan("carte cadeau")
    thrice = detector.scan("carte cadeau, carte cadeau, encore une carte cadeau")
    assert once.risk == thrice.risk
    assert len(thrice.hits) == 1


def test_distinct_terms_accumulate(detector):
    one = detector.scan("carte cadeau")
    two = detector.scan("carte cadeau et un virement urgent")
    assert two.risk > one.risk
    assert len(two.hits) == 2


def test_longest_term_wins_over_its_prefix(detector):
    """« carte bancaire » ne doit pas aussi compter « carte cadeau » ni l'inverse."""
    signal = detector.scan("donnez-moi votre carte bancaire")
    assert signal.terms == ("carte bancaire",)


def test_word_boundaries_prevent_substring_matches(detector):
    # « rib » est un terme ; « ribambelle » n'en est pas un.
    assert detector.scan("une ribambelle d'enfants").terms == ()
    # « crypto » ne doit pas matcher à l'intérieur de « cryptogramme ».
    assert detector.scan("le cryptogramme").terms == ("cryptogramme",)


# --- Saturation ------------------------------------------------------------


def test_risk_is_bounded(detector):
    assert detector.scan("").risk == 0.0
    assert detector.scan("bonjour comment vas-tu").risk == 0.0
    everything = " . ".join(h.term for h in LEXICON)
    assert 0.0 <= detector.scan(everything).risk <= 1.0


def test_one_decisive_term_is_already_alarming(detector):
    """Un terme de poids 5 vaut ~0,63 : au-dessus du seuil de vigilance."""
    assert detector.scan("carte cadeau").risk == pytest.approx(0.632, abs=0.01)


def test_many_weak_terms_do_not_reach_the_trigger():
    """La gravité domine la quantité : dix termes de poids 1 restent du contexte.

    C'est ce qui empêche un appel de famille bavard d'allumer le dashboard.
    """
    weak = tuple(
        KeywordHit(term=f"terme{i}", weight=1, category="claimed_identity") for i in range(10)
    )
    detector = KeywordDetector(lexicon=weak)
    signal = detector.scan(" ".join(h.term for h in weak))
    assert len(signal.hits) == 10
    assert signal.risk < KEYWORDS_ALONE_TRIGGER


def test_severity_outranks_breadth():
    lexicon = (
        KeywordHit(term="decisif", weight=5, category="unusual_payment"),
        *(KeywordHit(term=f"faible{i}", weight=1, category="urgency") for i in range(5)),
    )
    detector = KeywordDetector(lexicon=lexicon)
    severe = detector.scan("decisif")
    broad = detector.scan("faible0 faible1 faible2 faible3 faible4")
    assert severe.risk > broad.risk


# --- scan_call -------------------------------------------------------------


def test_scan_call_ignores_victim_turns(detector):
    """La victime qui répète les mots de l'appelant n'est pas une preuve."""
    turns = [
        turn("bonjour", speaker="caller"),
        turn("vous voulez mon code de la carte et une carte cadeau ?", speaker="victim"),
    ]
    assert detector.scan_call(turns).risk == 0.0


def test_scan_call_ignores_partial_segments(detector):
    """Claude et le score ne travaillent que sur des segments stabilisés."""
    turns = [turn("carte cadeau", final=False)]
    assert detector.scan_call(turns).risk == 0.0


def test_scan_call_accumulates_across_turns(detector):
    turns = [
        turn("il me faut de l'argent", t=1.0),
        turn("achète une carte cadeau", t=5.0),
    ]
    signal = detector.scan_call(turns)
    assert set(signal.terms) == {"il me faut de l'argent", "carte cadeau"}


def test_scan_call_does_not_match_across_segment_boundary(detector):
    """Deux segments recollés ne doivent pas fabriquer une expression."""
    turns = [turn("surtout ne dis", t=1.0), turn("rien à personne", t=2.0)]
    assert "ne dis rien à" not in detector.scan_call(turns).terms


def test_scan_call_empty(detector):
    assert detector.scan_call([]).risk == 0.0


# --- Calibration sur les scénarios -----------------------------------------


def test_grandchild_scam_fires_the_trigger(detector):
    """Scénario « faux petit-fils » : doit déclencher la vigilance seul."""
    turns = [
        turn("Mamie c'est Lucas, j'ai changé de numéro", t=2.0),
        turn("J'ai eu un accident, j'ai un gros problème", t=8.0),
        turn("Il me faut de l'argent tout de suite", t=14.0),
        turn("Tu peux acheter des cartes cadeaux, un coursier va passer", t=20.0),
        turn("Surtout ne dis rien à papa, ça reste entre nous", t=27.0),
    ]
    signal = detector.scan_call(turns)
    assert signal.risk >= KEYWORDS_ALONE_TRIGGER
    assert signal.risk >= 0.6


def test_fake_bank_scam_fires_the_trigger(detector):
    turns = [
        turn("Bonjour, je suis du service des fraudes de votre banque", t=3.0),
        turn("Il y a un virement urgent suspect sur votre compte", t=9.0),
        turn("Donnez-moi le code reçu par SMS immédiatement", t=15.0),
        turn("Ne raccrochez pas et n'appelez pas la banque", t=22.0),
    ]
    assert detector.scan_call(turns).risk >= 0.6


@pytest.mark.parametrize(
    "turns_text",
    [
        # Appel légitime 1 — visite familiale.
        [
            "Allô mamie, c'est Lucas",
            "Je passe te voir dimanche, j'apporte le gâteau",
            "Tu veux que je prenne du pain en arrivant ?",
        ],
        # Appel légitime 2 — rendez-vous médical, sujet d'argent anodin.
        [
            "Bonjour madame, c'est le cabinet du docteur Martin",
            "Je vous appelle pour déplacer votre rendez-vous de jeudi",
            "Vous n'avez rien à régler, c'est pris en charge",
        ],
    ],
)
def test_legitimate_calls_stay_low(detector, turns_text):
    """Les deux appels légitimes de Biz 1 doivent rester loin du seuil."""
    turns = [turn(text, t=float(i * 5)) for i, text in enumerate(turns_text)]
    signal = detector.scan_call(turns)
    assert signal.risk < 0.3, f"faux positif : {signal.terms}"


# --- Surcharge du lexique par Biz 1 ----------------------------------------


def test_load_lexicon_from_json(tmp_path):
    path = tmp_path / "lexique.json"
    path.write_text(
        json.dumps(
            [
                {"term": "mot décisif", "weight": 5, "category": "unusual_payment"},
                {"term": "Mot Décisif", "weight": 5, "category": "unusual_payment"},
                {"term": "petit mot", "weight": 1, "category": "urgency"},
            ]
        ),
        encoding="utf-8",
    )
    hits = load_lexicon(path)
    assert len(hits) == 2  # le doublon de casse est écarté
    assert KeywordDetector(lexicon=hits).scan("un MOT DECISIF").terms == ("mot décisif",)


def test_load_lexicon_accepts_wrapped_object(tmp_path):
    path = tmp_path / "lexique.json"
    path.write_text(
        json.dumps({"terms": [{"term": "abc", "weight": 2, "category": "urgency"}]}),
        encoding="utf-8",
    )
    assert load_lexicon(path)[0].term == "abc"


@pytest.mark.parametrize(
    "payload",
    [
        [{"term": "abc", "weight": 9, "category": "urgency"}],  # poids hors bornes
        [{"term": "", "weight": 3, "category": "urgency"}],  # terme vide
        [{"weight": 3}],  # terme manquant
        [],  # lexique vide
        {"nope": 1},  # structure inattendue
    ],
)
def test_load_lexicon_rejects_bad_input(tmp_path, payload):
    """Une faute de frappe de Biz 1 doit crier, pas déformer le score en silence."""
    path = tmp_path / "lexique.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        load_lexicon(path)


def test_empty_lexicon_is_rejected():
    with pytest.raises(ValueError):
        KeywordDetector(lexicon=())
