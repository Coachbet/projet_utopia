"""Préparation de l'audio pour le détecteur de voix synthétique.

Numpy uniquement : torch est un extra optionnel (~2 Go) et ce module doit
rester importable sans lui, parce que c'est la partie qui doit continuer à
fonctionner quand le modèle, lui, n'est pas installé.

L'interpolation linéaire suffit pour le 8 -> 16 kHz. Le facteur limitant n'est
pas la qualité du rééchantillonnage mais la généralisation du modèle sur de
l'audio téléphonique : plusieurs fiches de modèles wav2vec2 reconnaissent
mal généraliser hors de leur corpus 16 kHz propre. Un filtre polyphase coûterait
scipy en dépendance pour un gain noyé dans cette incertitude.
"""

from __future__ import annotations

import numpy as np

_INT16_SCALE = 32768.0
"""2^15 : mappe le int16 dans [-1, 1[. Le plein positif vaut 32767/32768."""

PCM16_BYTES_PER_SAMPLE = 2

_LE_INT16 = np.dtype("<i2")
"""Little-endian explicite : le PCM qui sort de la téléphonie l'est toujours,
ne pas dépendre de l'ordre d'octets de la machine."""


def pcm16_to_float32(pcm16: bytes) -> np.ndarray:
    """PCM 16 bits signé, mono, little-endian -> float32 dans [-1, 1[."""
    usable = len(pcm16) - (len(pcm16) % PCM16_BYTES_PER_SAMPLE)
    if usable <= 0:
        return np.zeros(0, dtype=np.float32)
    samples = np.frombuffer(pcm16, dtype=_LE_INT16, count=usable // PCM16_BYTES_PER_SAMPLE)
    return (samples.astype(np.float32) / _INT16_SCALE).astype(np.float32)


def resample_linear(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Rééchantillonne par interpolation linéaire.

    Pour un rapport entier (8 -> 16 kHz), les échantillons d'origine sont
    conservés tels quels aux indices pairs : l'interpolation n'ajoute donc
    aucune distorsion sur le signal existant, elle ne fait que l'intercaler.
    """
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError(f"fréquences invalides : {src_rate} -> {dst_rate}")
    samples = np.asarray(samples, dtype=np.float32)
    if src_rate == dst_rate or samples.size == 0:
        return samples
    if samples.size == 1:
        return np.repeat(samples, max(1, round(dst_rate / src_rate)))

    out_len = round(samples.size * dst_rate / src_rate)
    positions = np.arange(out_len, dtype=np.float64) * (src_rate / dst_rate)
    # Interpolation bornée au dernier échantillon : pas d'extrapolation en fin
    # de fenêtre, qui inventerait de l'amplitude absente du signal.
    np.clip(positions, 0.0, samples.size - 1, out=positions)
    source_index = np.arange(samples.size, dtype=np.float64)
    return np.interp(positions, source_index, samples).astype(np.float32)


def prepare_window(pcm16: bytes, src_rate: int, dst_rate: int) -> np.ndarray:
    """Chaîne complète : octets PCM téléphoniques -> float32 à `dst_rate`."""
    return resample_linear(pcm16_to_float32(pcm16), src_rate, dst_rate)
