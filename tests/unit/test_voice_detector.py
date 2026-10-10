"""Tests du signal « voix synthétique ».

Tournent sans torch ni transformers, et ne téléchargent jamais de modèle : le
classifieur est injecté, ou `_load_classifier` est remplacé.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

import numpy as np
import pytest

from vocalguard.domain.models import VoiceSignal
from vocalguard.infrastructure.voice import synthetic_detector as sd
from vocalguard.infrastructure.voice.resample import (
    pcm16_to_float32,
    prepare_window,
    resample_linear,
)
from vocalguard.infrastructure.voice.synthetic_detector import (
    Wav2VecVoiceDetector,
    resolve_fake_index,
)

SAMPLE_RATE = 8000
WINDOW_SECONDS = 0.01
WINDOW_SAMPLES = int(SAMPLE_RATE * WINDOW_SECONDS)  # 80
WINDOW_BYTES = WINDOW_SAMPLES * 2  # 160


def pcm_window(samples: int = WINDOW_SAMPLES, value: int = 1000) -> bytes:
    return np.full(samples, value, dtype="<i2").tobytes()


class FakeClassifier:
    """Classifieur injecté : renvoie des probabilités scriptées."""

    def __init__(
        self,
        probabilities: Sequence[Sequence[float]],
        labels: Mapping[int, str] | None = None,
    ) -> None:
        self._labels = dict(labels if labels is not None else {0: "real", 1: "fake"})
        self._probabilities = [list(p) for p in probabilities]
        self.calls = 0
        self.seen_rates: list[int] = []

    @property
    def id2label(self) -> Mapping[int, str]:
        return self._labels

    def __call__(self, samples: np.ndarray, sample_rate: int) -> Sequence[float]:
        self.seen_rates.append(sample_rate)
        index = min(self.calls, len(self._probabilities) - 1)
        self.calls += 1
        return self._probabilities[index]


def make_detector(classifier: FakeClassifier, **kwargs: object) -> Wav2VecVoiceDetector:
    options: dict[str, object] = {
        "classifier": classifier,
        "window_seconds": WINDOW_SECONDS,
        "target_sample_rate": 16000,
    }
    options.update(kwargs)
    return Wav2VecVoiceDetector(**options)  # type: ignore[arg-type]


# --- resample.py -----------------------------------------------------------


def test_pcm16_to_float32_stays_in_range():
    extremes = np.array([-32768, -1, 0, 1, 32767], dtype="<i2").tobytes()
    out = pcm16_to_float32(extremes)

    assert out.dtype == np.float32
    assert out.size == 5
    assert out.min() >= -1.0
    assert out.max() <= 1.0
    assert out[2] == pytest.approx(0.0)
    assert out[0] == pytest.approx(-1.0)


def test_pcm16_to_float32_tolerates_truncated_and_empty_input():
    # Une trame coupée en plein échantillon ne doit pas faire exploser le flux.
    assert pcm16_to_float32(b"\x01\x00\x02").size == 1
    assert pcm16_to_float32(b"").size == 0
    assert pcm16_to_float32(b"\x7f").size == 0


def test_resample_8k_to_16k_doubles_length():
    samples = np.zeros(800, dtype=np.float32)
    assert resample_linear(samples, 8000, 16000).size == 1600


def test_resample_preserves_amplitude_of_a_sine():
    t = np.arange(SAMPLE_RATE, dtype=np.float32) / SAMPLE_RATE
    sine = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)

    upsampled = resample_linear(sine, 8000, 16000)

    # L'interpolation linéaire est une combinaison convexe : elle ne peut pas
    # dépasser l'amplitude d'entrée.
    assert np.abs(upsampled).max() <= np.abs(sine).max() + 1e-6
    # Rapport entier : les échantillons d'origine sont conservés aux indices pairs.
    np.testing.assert_allclose(upsampled[::2], sine, atol=1e-6)


def test_resample_is_identity_when_rates_match():
    samples = np.array([0.1, -0.2, 0.3], dtype=np.float32)
    np.testing.assert_allclose(resample_linear(samples, 8000, 8000), samples)


def test_resample_rejects_invalid_rates():
    with pytest.raises(ValueError):
        resample_linear(np.zeros(4, dtype=np.float32), 0, 16000)


def test_prepare_window_chains_decode_and_resample():
    out = prepare_window(pcm_window(), SAMPLE_RATE, 16000)

    assert out.dtype == np.float32
    assert out.size == WINDOW_SAMPLES * 2
    assert np.abs(out).max() <= 1.0


# --- étiquettes ------------------------------------------------------------


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        ({0: "real", 1: "fake"}, 1),
        ({0: "fake", 1: "real"}, 0),
        ({0: "bonafide", 1: "spoof"}, 1),
        ({0: "SYNTHETIC", 1: "Human"}, 0),
        ({0: "authentic", 1: "AI-generated"}, 1),
        ({0: "original", 1: "cloned"}, 1),
    ],
)
def test_resolve_fake_index_handles_varied_label_spaces(labels, expected):
    assert resolve_fake_index(labels) == expected


@pytest.mark.parametrize(
    "labels",
    [
        {0: "LABEL_0", 1: "LABEL_1"},  # fiche de modèle sans étiquettes nommées
        {},
        {0: "speech", 1: "music"},
    ],
)
def test_resolve_fake_index_returns_none_rather_than_guessing(labels):
    assert resolve_fake_index(labels) is None


def test_resolve_fake_index_is_not_fooled_by_negated_labels():
    # « not_fake » contient « fake » : le prendre pour la classe synthétique
    # inverserait l'axe, ce qui est pire qu'un signal absent.
    assert resolve_fake_index({0: "not_fake", 1: "fake"}) == 1


# --- fenêtrage et piste ----------------------------------------------------


async def test_returns_none_until_a_full_window_is_buffered():
    classifier = FakeClassifier([[0.2, 0.8]])
    detector = make_detector(classifier)

    assert await detector.feed("caller", pcm_window(WINDOW_SAMPLES // 2), SAMPLE_RATE) is None
    assert classifier.calls == 0

    signal = await detector.feed("caller", pcm_window(WINDOW_SAMPLES // 2), SAMPLE_RATE)

    assert isinstance(signal, VoiceSignal)
    assert classifier.calls == 1


async def test_victim_track_is_never_analyzed():
    classifier = FakeClassifier([[0.1, 0.9]])
    detector = make_detector(classifier)

    for _ in range(4):
        assert await detector.feed("victim", pcm_window(), SAMPLE_RATE) is None

    assert classifier.calls == 0


async def test_audio_is_upsampled_to_the_model_rate():
    classifier = FakeClassifier([[0.5, 0.5]])
    detector = make_detector(classifier)

    await detector.feed("caller", pcm_window(), SAMPLE_RATE)

    assert classifier.seen_rates == [16000]


# --- lissage ---------------------------------------------------------------


async def test_risk_is_smoothed_across_windows():
    classifier = FakeClassifier([[0.1, 0.9], [0.9, 0.1], [0.5, 0.5]])
    detector = make_detector(classifier)

    first = await detector.feed("caller", pcm_window(), SAMPLE_RATE)
    second = await detector.feed("caller", pcm_window(), SAMPLE_RATE)
    third = await detector.feed("caller", pcm_window(), SAMPLE_RATE)

    assert first is not None and second is not None and third is not None
    assert first.risk == pytest.approx(0.9)
    # Une fenêtre aberrante ne doit pas faire de pic : on moyenne.
    assert second.risk == pytest.approx(0.5)
    assert third.risk == pytest.approx(0.5)
    assert (first.windows_analyzed, second.windows_analyzed, third.windows_analyzed) == (1, 2, 3)


async def test_smoothing_history_is_bounded():
    classifier = FakeClassifier([[0.0, 1.0], [1.0, 0.0]])
    detector = make_detector(classifier, smoothing_windows=2)

    await detector.feed("caller", pcm_window(), SAMPLE_RATE)  # 1.0
    await detector.feed("caller", pcm_window(), SAMPLE_RATE)  # 0.0
    third = await detector.feed("caller", pcm_window(), SAMPLE_RATE)  # 0.0

    assert third is not None
    # La première fenêtre est sortie de l'historique.
    assert third.risk == pytest.approx(0.0)
    assert third.windows_analyzed == 3


# --- dégradations ----------------------------------------------------------


async def test_missing_torch_degrades_to_no_signal(monkeypatch, caplog):
    def explode(model_id: str):
        raise ImportError("No module named 'torch'")

    monkeypatch.setattr(sd, "_load_classifier", explode)
    detector = Wav2VecVoiceDetector(window_seconds=WINDOW_SECONDS, enabled=True)

    with caplog.at_level(logging.WARNING):
        assert await detector.feed("caller", pcm_window(), SAMPLE_RATE) is None
        # Toujours rien, et sans retenter : le signal est éteint pour l'appel.
        assert await detector.feed("caller", pcm_window(), SAMPLE_RATE) is None

    assert len(caplog.records) == 1


async def test_model_download_failure_degrades_to_no_signal(monkeypatch):
    def explode(model_id: str):
        raise OSError("réseau du lieu : huggingface.co injoignable")

    monkeypatch.setattr(sd, "_load_classifier", explode)
    detector = Wav2VecVoiceDetector(window_seconds=WINDOW_SECONDS, enabled=True)

    assert await detector.feed("caller", pcm_window(), SAMPLE_RATE) is None


async def test_unreadable_labels_degrade_to_no_signal(monkeypatch):
    classifier = FakeClassifier([[0.1, 0.9]], labels={0: "LABEL_0", 1: "LABEL_1"})
    monkeypatch.setattr(sd, "_load_classifier", lambda model_id: classifier)
    detector = Wav2VecVoiceDetector(window_seconds=WINDOW_SECONDS, enabled=True)

    assert await detector.feed("caller", pcm_window(), SAMPLE_RATE) is None
    # On ne devine pas un indice : aucune inférence n'est tentée.
    assert classifier.calls == 0


async def test_injected_classifier_with_unreadable_labels_is_disabled_at_construction():
    classifier = FakeClassifier([[0.1, 0.9]], labels={0: "speech", 1: "music"})
    detector = make_detector(classifier)

    assert await detector.feed("caller", pcm_window(), SAMPLE_RATE) is None
    assert classifier.calls == 0


async def test_disabled_by_configuration_never_analyzes():
    classifier = FakeClassifier([[0.1, 0.9]])
    detector = make_detector(classifier, enabled=False)

    assert await detector.feed("caller", pcm_window(), SAMPLE_RATE) is None
    assert classifier.calls == 0


async def test_inference_failure_degrades_to_no_signal():
    class Exploding(FakeClassifier):
        def __call__(self, samples, sample_rate):
            raise RuntimeError("tensor shape mismatch")

    classifier = Exploding([[0.1, 0.9]])
    detector = make_detector(classifier)

    assert await detector.feed("caller", pcm_window(), SAMPLE_RATE) is None
    assert await detector.feed("caller", pcm_window(), SAMPLE_RATE) is None


async def test_model_is_loaded_only_once(monkeypatch):
    classifier = FakeClassifier([[0.2, 0.8]])
    loads = []

    def load(model_id: str):
        loads.append(model_id)
        return classifier

    monkeypatch.setattr(sd, "_load_classifier", load)
    detector = Wav2VecVoiceDetector(
        window_seconds=WINDOW_SECONDS, enabled=True, model_id="some/model"
    )

    for _ in range(3):
        await detector.feed("caller", pcm_window(), SAMPLE_RATE)

    assert loads == ["some/model"]
    assert classifier.calls == 3
