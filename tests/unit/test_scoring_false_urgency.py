"""Contrastes de sécurité : urgence réelle, identité incertaine et paiement suspect."""

import pytest

from vocalguard.domain.models import ContentSignal, KeywordSignal, ReputationSignal, SignalSet
from vocalguard.domain.scoring.engine import assess


def test_legitimate_emergency_on_borrowed_phone_never_forces_intervention() -> None:
    # L'appelant est Camille depuis un téléphone prêté, avec remboursement de
    # pharmacie. Même si un modèle conserve un libellé de paiement ambigu, son
    # jugement global bas doit empêcher la mise en attente automatique.
    assessment = assess(
        SignalSet(
            content=ContentSignal(
                0.12, tactics=("urgence", "paiement inhabituel"), claimed_identity="Camille, fille"
            ),
            keywords=KeywordSignal(0.95),
            reputation=ReputationSignal(0.9, identity_mismatch=True),
        )
    )
    assert assessment.score < 65
    assert "payment_request_with_identity_mismatch" not in assessment.breakers_fired


@pytest.mark.parametrize("risk", [0.66, 0.97])
def test_real_fraud_with_confident_payment_and_identity_mismatch_still_intervenes(risk) -> None:
    assessment = assess(
        SignalSet(
            content=ContentSignal(
                risk,
                tactics=("urgence", "secret", "paiement inhabituel"),
                claimed_identity="Camille, fille",
            ),
            keywords=KeywordSignal(0.9),
            reputation=ReputationSignal(0.9, identity_mismatch=True),
        )
    )
    assert assessment.score >= 85
    assert "payment_request_with_identity_mismatch" in assessment.breakers_fired


def test_keyword_alarm_during_real_urgency_is_vigilance_without_identity_verdict() -> None:
    assessment = assess(
        SignalSet(
            keywords=KeywordSignal(0.95),
            reputation=ReputationSignal(0.35),
        )
    )
    assert 40 <= assessment.score < 65


def test_human_denial_remains_decisive_even_when_content_model_is_uncertain() -> None:
    assessment = assess(
        SignalSet(
            content=ContentSignal(0.12),
            reputation=ReputationSignal(1, contact_denied=True),
        )
    )
    assert assessment.score >= 85
    assert "trusted_contact_denied" in assessment.breakers_fired
