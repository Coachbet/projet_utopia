#!/usr/bin/env python3
"""Calibre le détecteur de voix synthétique sur de l'audio TÉLÉPHONIQUE.

Le modèle wav2vec2 a été entraîné sur du 16 kHz propre. Au téléphone (8 kHz,
μ-law), une vraie voix ne sort pas autour de 0 : elle sort à un plancher qui
dépend du modèle. Ce script mesure ce plancher et le niveau d'une voix clonée,
puis propose les deux réglages à mettre dans `.env` :

    VOICE_DETECTOR_CALIBRATION_LOW=…   (vraie voix → risque 0)
    VOICE_DETECTOR_CALIBRATION_HIGH=…  (voix clonée → risque 1)

Usage (après `make install-voice`) :

    uv run python scripts/calibrate_voice.py \
        --human enregistrements/vraie_*.wav \
        --fake  enregistrements/clone_*.wav

Chaque fichier WAV (PCM 16 bits, mono ou stéréo, n'importe quelle fréquence)
passe par la même chaîne qu'un vrai appel : ramené à 8 kHz, compressé en
μ-law comme sur la ligne Twilio, puis remonté à 16 kHz par `prepare_window`.
Enregistrez au moins 3 vraies voix et 3 voix clonées de 10 s ou plus.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vocalguard.config import get_settings
from vocalguard.infrastructure.voice.resample import _lowpass_kernel, prepare_window
from vocalguard.infrastructure.voice.synthetic_detector import (
    resolve_fake_index,
    shared_classifier,
)

PHONE_RATE = 8000


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise SystemExit(f"{path} : seul le PCM 16 bits est pris en charge")
        rate = wav.getframerate()
        frames = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        channels = wav.getnchannels()
    samples = frames.astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, rate


def to_phone(samples: np.ndarray, rate: int) -> bytes:
    """Ramène à 8 kHz (filtrage anti-repliement) puis applique le μ-law G.711."""
    if rate != PHONE_RATE:
        if rate % PHONE_RATE == 0:
            factor = rate // PHONE_RATE
            filtered = np.convolve(samples, _lowpass_kernel(0.46 / factor), mode="same")
            samples = filtered[::factor]
        else:
            positions = np.arange(0, samples.size, rate / PHONE_RATE)
            samples = np.interp(positions, np.arange(samples.size), samples)
    mu = 255.0
    companded = np.sign(samples) * np.log1p(mu * np.abs(samples)) / np.log1p(mu)
    quantized = np.round(companded * 127.0) / 127.0
    restored = np.sign(quantized) * ((1.0 + mu) ** np.abs(quantized) - 1.0) / mu
    return (np.clip(restored, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def score_file(path: Path, window_seconds: float, target_rate: int, model_id: str) -> list[float]:
    classifier = shared_classifier(model_id)
    fake_index = resolve_fake_index(classifier.id2label)
    if fake_index is None:
        raise SystemExit(f"Étiquettes du modèle non reconnues : {classifier.id2label}")
    samples, rate = read_wav(path)
    pcm = to_phone(samples, rate)
    window_bytes = int(window_seconds * PHONE_RATE) * 2
    scores = []
    for start in range(0, len(pcm) - window_bytes + 1, window_bytes):
        window = prepare_window(pcm[start : start + window_bytes], PHONE_RATE, target_rate)
        scores.append(float(classifier(window, target_rate)[fake_index]))
    return scores


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--human", nargs="+", type=Path, required=True, help="WAV de vraies voix")
    parser.add_argument("--fake", nargs="+", type=Path, required=True, help="WAV de voix clonées")
    args = parser.parse_args()

    settings = get_settings()
    model = settings.voice_detector_model_id
    print(f"Modèle : {model}\n")

    groups: dict[str, list[float]] = {"human": [], "fake": []}
    for group, paths in (("human", args.human), ("fake", args.fake)):
        for path in paths:
            scores = score_file(
                path,
                settings.voice_detector_window_seconds,
                settings.voice_detector_target_sample_rate,
                model,
            )
            if not scores:
                print(f"  {group:5}  {path.name:40}  trop court (< une fenêtre)")
                continue
            groups[group].extend(scores)
            mean = statistics.fmean(scores)
            print(f"  {group:5}  {path.name:40}  moyenne {mean:.3f}  ({len(scores)} fenêtres)")

    if not groups["human"] or not groups["fake"]:
        print("\nIl faut au moins une fenêtre de chaque groupe.")
        return 1
    low = float(np.percentile(groups["human"], 90))
    high = float(np.median(groups["fake"]))
    print(f"\nVraies voix  : médiane {np.median(groups['human']):.3f}, 90e centile {low:.3f}")
    print(f"Voix clonées : médiane {high:.3f}")
    if high <= low + 0.05:
        print(
            "\nATTENTION : le modèle ne sépare pas ces deux groupes au téléphone."
            "\nGardez le signal voix comme simple indice (il est déjà plafonné)."
        )
        return 2
    print("\nÀ mettre dans .env :")
    print(f"VOICE_DETECTOR_CALIBRATION_LOW={low:.3f}")
    print(f"VOICE_DETECTOR_CALIBRATION_HIGH={high:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
