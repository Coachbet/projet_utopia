"""Moteur de score : somme pondérée des quatre signaux, puis coupe-circuits."""

from __future__ import annotations

from vocalguard.domain.alerts import DEFAULT_THRESHOLDS, Thresholds
from vocalguard.domain.models import RiskAssessment, SignalSet
from vocalguard.domain.scoring import circuit_breakers
from vocalguard.domain.scoring.weights import DEFAULT_WEIGHTS, Weights


def _weighted(signals: SignalSet, weights: Weights) -> float:
    total = 0.0
    if signals.content is not None:
        total += weights.llm * signals.content.risk
    if signals.keywords is not None:
        total += weights.keywords * signals.keywords.risk
    if signals.voice is not None:
        total += weights.voice * signals.voice.risk
    if signals.reputation is not None:
        total += weights.reputation * signals.reputation.risk
    return total


def _collect_reasons(signals: SignalSet) -> tuple[str, ...]:
    """Raisons lisibles, reprises telles quelles dans le dashboard et l'alerte."""
    reasons: list[str] = []
    if signals.content is not None:
        reasons.extend(signals.content.tactics)
    if signals.keywords is not None:
        reasons.extend(signals.keywords.terms)
    if signals.reputation is not None:
        reasons.extend(signals.reputation.reasons)
    if signals.voice is not None and signals.voice.risk >= 0.5:
        reasons.append("possibly synthetic voice")

    seen: set[str] = set()
    unique = []
    for r in reasons:
        key = r.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return tuple(unique)


def assess(
    signals: SignalSet,
    *,
    weights: Weights = DEFAULT_WEIGHTS,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
) -> RiskAssessment:
    raw = _weighted(signals, weights)
    fired = circuit_breakers.evaluate(signals, raw, weights)

    score = raw
    for breaker in fired:
        if breaker.floor is not None:
            score = max(score, breaker.floor)
    for breaker in fired:
        if breaker.cap is not None:
            score = min(score, breaker.cap)

    final = max(0, min(100, round(score)))
    explanation = signals.content.explanation if signals.content is not None else ""

    return RiskAssessment(
        score=final,
        level=thresholds.level_for(final),
        reasons=_collect_reasons(signals),
        breakers_fired=tuple(b.name for b in fired),
        explanation=explanation,
    )
