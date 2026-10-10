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


_FIR_HALF_TAPS = 24
"""Demi-longueur du filtre passe-bas (49 coefficients). Assez pour une coupure
nette à la fréquence de Nyquist téléphonique, assez court pour 4 s de signal
en quelques millisecondes."""


def _lowpass_kernel(cutoff: float, half_taps: int = _FIR_HALF_TAPS) -> np.ndarray:
    """Sinus cardinal fenêtré (Blackman), `cutoff` en fraction de la fréquence
    d'échantillonnage de sortie (0 < cutoff < 0,5)."""
    n = np.arange(-half_taps, half_taps + 1, dtype=np.float64)
    kernel = 2.0 * cutoff * np.sinc(2.0 * cutoff * n) * np.blackman(n.size)
    return kernel / kernel.sum()


def resample_bandlimited(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Suréchantillonnage à bande limitée, pour un rapport entier (8 -> 16 kHz).

    Pourquoi pas l'interpolation linéaire : elle laisse des IMAGES spectrales
    entre 4 et 8 kHz (le spectre téléphonique replié vers le haut). Un détecteur
    de voix synthétique entraîné sur du 16 kHz propre lit justement les hautes
    fréquences pour repérer les artefacts de vocodeur : ces images inventées le
    font osciller entre « tout est faux » et « rien n'est faux » selon la voix,
    sans rapport avec la voix elle-même. Ici on insère des zéros puis on coupe
    proprement au-dessus de la fréquence de Nyquist d'origine : la bande 4-8 kHz
    reste vide, identique pour une vraie voix et pour une voix clonée, et le
    modèle ne compare plus que ce que le téléphone a réellement transporté.

    Rapport non entier : repli sur l'interpolation linéaire.
    """
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError(f"fréquences invalides : {src_rate} -> {dst_rate}")
    samples = np.asarray(samples, dtype=np.float32)
    if src_rate == dst_rate or samples.size == 0:
        return samples
    if dst_rate % src_rate != 0 or samples.size < 2:
        return resample_linear(samples, src_rate, dst_rate)

    factor = dst_rate // src_rate
    stuffed = np.zeros(samples.size * factor, dtype=np.float64)
    stuffed[::factor] = samples
    # Coupure à 92 % de la Nyquist d'origine, gain `factor` pour compenser les zéros.
    kernel = _lowpass_kernel(0.46 / factor) * factor
    filtered = np.convolve(stuffed, kernel, mode="same")
    return np.clip(filtered, -1.0, 1.0).astype(np.float32)


def prepare_window(pcm16: bytes, src_rate: int, dst_rate: int) -> np.ndarray:
    """Chaîne complète : octets PCM téléphoniques -> float32 à `dst_rate`."""
    return resample_bandlimited(pcm16_to_float32(pcm16), src_rate, dst_rate)
