"""Décodage G.711 μ-law → PCM 16 bits.

Twilio livre l'audio en μ-law 8 kHz base64 ; Transcribe veut du PCM 16 bits.
On ne passe pas par `audioop` : le module disparaît en Python 3.13, et on ne veut
pas que la démo dépende d'un paquet de compatibilité. numpy est déjà là.

Une table de 256 entrées suffit à décrire tout le codec : on la construit une
fois au chargement, puis chaque trame est décodée par indexation vectorielle.
À 50 trames par seconde et par piste, une boucle Python par échantillon serait
du gaspillage pur.
"""

from __future__ import annotations

import numpy as np

_BIAS = 0x84
"""Biais G.711. Ajouté avant le décalage, retiré après : c'est ce qui donne au
codec sa résolution fine près de zéro."""


def _build_table() -> np.ndarray:
    codes = np.arange(256, dtype=np.int32)
    # Le μ-law est stocké en complément : tous les bits sont inversés sur le fil.
    u = ~codes & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F

    magnitude = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    samples = np.where(sign != 0, -magnitude, magnitude)
    return samples.astype(np.int16)


ULAW_TO_PCM16: np.ndarray = _build_table()
"""Table de décodage, indexée par l'octet μ-law reçu. Lecture seule."""
ULAW_TO_PCM16.setflags(write=False)


def ulaw_to_array(payload: bytes) -> np.ndarray:
    """Échantillons PCM 16 bits signés, pour les traitements numpy (voix, mesures)."""
    return ULAW_TO_PCM16[np.frombuffer(payload, dtype=np.uint8)]


def ulaw_to_pcm16(payload: bytes) -> bytes:
    """PCM 16 bits little-endian : la forme attendue par Transcribe streaming."""
    return ulaw_to_array(payload).astype("<i2").tobytes()


def pcm16_duration_seconds(pcm16: bytes, sample_rate: int = 8000) -> float:
    """Durée d'un bloc PCM. Sert à horodater les segments sans dépendre de
    l'horloge murale, qui dérive du flux quand le réseau hoquette."""
    return len(pcm16) / 2 / sample_rate
