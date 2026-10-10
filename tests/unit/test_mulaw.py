"""Le décodeur μ-law est validé contre `audioop`, la référence CPython du codec.

`audioop` n'est utilisé QUE par ce test : le code de production décode en numpy,
parce que le module disparaît en Python 3.13. Si ces deux implémentations
divergent d'un seul bit, c'est le son envoyé à Transcribe qui est faux.
"""

from __future__ import annotations

import warnings

import numpy as np
import pytest

from vocalguard.infrastructure.telephony.mulaw import (
    ULAW_TO_PCM16,
    pcm16_duration_seconds,
    ulaw_to_array,
    ulaw_to_pcm16,
)

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    audioop = pytest.importorskip("audioop", reason="retiré en Python 3.13")

ALL_BYTES = bytes(range(256))


def test_table_identique_a_audioop_sur_les_256_octets():
    attendu = np.frombuffer(audioop.ulaw2lin(ALL_BYTES, 2), dtype="<i2")
    assert np.array_equal(ULAW_TO_PCM16, attendu)


def test_trame_realiste_de_20ms():
    # 160 octets = 20 ms à 8 kHz : exactement ce que Twilio envoie par message.
    trame = bytes((i * 7 + 13) % 256 for i in range(160))
    assert ulaw_to_pcm16(trame) == audioop.ulaw2lin(trame, 2)
    assert len(ulaw_to_pcm16(trame)) == 320
    assert pcm16_duration_seconds(ulaw_to_pcm16(trame)) == pytest.approx(0.02)


def test_silence_mulaw_decode_a_zero():
    # 0xFF est le silence sur le fil ; un décalage de signe le ferait saturer.
    assert ulaw_to_array(b"\xff" * 10).tolist() == [0] * 10


def test_signe_et_echelle():
    # 0x00 et 0x80 sont les extrêmes opposés du codec : ils valident le signe.
    assert ULAW_TO_PCM16[0x00] == -32124
    assert ULAW_TO_PCM16[0x80] == 32124
    assert ULAW_TO_PCM16.dtype == np.int16


def test_payload_vide():
    assert ulaw_to_pcm16(b"") == b""
    assert ulaw_to_array(b"").shape == (0,)
    assert pcm16_duration_seconds(b"") == 0.0


def test_duree_depend_du_taux_declare():
    pcm = ulaw_to_pcm16(b"\xff" * 8000)
    assert pcm16_duration_seconds(pcm, 8000) == pytest.approx(1.0)
    assert pcm16_duration_seconds(pcm, 16000) == pytest.approx(0.5)


def test_table_non_modifiable():
    # Partagée par tous les appels : une écriture accidentelle corromprait
    # le décodage de toutes les sessions.
    with pytest.raises(ValueError):
        ULAW_TO_PCM16[0] = 0
