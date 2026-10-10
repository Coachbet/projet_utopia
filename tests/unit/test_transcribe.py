"""Tests de l'adapter Transcribe, sans réseau : le client AWS est remplacé par
une doublure. Ce qu'on protège ici, c'est le comportement en démo — un flux qui
n'arrive pas à s'ouvrir ne doit pas faire tomber l'appel.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from amazon_transcribe.model import Alternative, Result, Transcript, TranscriptEvent

from vocalguard.config import Settings
from vocalguard.domain.models import TranscriptSegment
from vocalguard.infrastructure.aws import transcribe as mod


@pytest.fixture
def settings() -> Settings:
    # Valeurs explicites : les tests ne doivent pas dépendre du `.env` de la machine.
    return Settings(
        _env_file=None,
        transcribe_region="us-west-2",
        transcribe_language_code="fr-FR",
        transcribe_sample_rate=8000,
    )


def _event(text: str, *, is_partial: bool) -> TranscriptEvent:
    alternatives = [Alternative(text, [], None)] if text is not None else []
    return TranscriptEvent(Transcript([Result(is_partial=is_partial, alternatives=alternatives)]))


class _FakeInputStream:
    def __init__(self, *, fail_on_send: bool = False) -> None:
        self.chunks: list[bytes] = []
        self.end_calls = 0
        self._fail_on_send = fail_on_send

    async def send_audio_event(self, audio_chunk: bytes) -> None:
        if self._fail_on_send:
            raise RuntimeError("connexion AWS perdue")
        self.chunks.append(audio_chunk)

    async def end_stream(self) -> None:
        self.end_calls += 1


class _FakeOutputStream:
    """Mime `TranscriptResultStream` : `__aiter__` est un générateur asynchrone."""

    def __init__(self, events: list[Any]) -> None:
        self._events = events

    async def __aiter__(self):
        for event in self._events:
            yield event


class _FakeEventStream:
    def __init__(self, events: list[Any], *, fail_on_send: bool = False) -> None:
        self.input_stream = _FakeInputStream(fail_on_send=fail_on_send)
        self.output_stream = _FakeOutputStream(events)


class _FakeClient:
    def __init__(
        self,
        events: list[Any] | None = None,
        *,
        fail: bool = False,
        output_stream: Any | None = None,
        fail_on_send: bool = False,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._events = events or []
        self._fail = fail
        self._output_stream = output_stream
        self._fail_on_send = fail_on_send

    async def start_stream_transcription(self, **kwargs: Any) -> _FakeEventStream:
        self.calls.append(kwargs)
        if self._fail:
            raise RuntimeError("AccessDeniedException: not authorized to perform")
        stream = _FakeEventStream(self._events, fail_on_send=self._fail_on_send)
        if self._output_stream is not None:
            stream.output_stream = self._output_stream
        return stream


def _collector() -> tuple[list[TranscriptSegment], Any]:
    segments: list[TranscriptSegment] = []

    async def on_segment(segment: TranscriptSegment) -> None:
        segments.append(segment)

    return segments, on_segment


async def test_echec_ouverture_donne_un_flux_inerte(settings: Settings) -> None:
    segments, on_segment = _collector()
    transcriber = mod.AwsTranscriber(settings, client=_FakeClient(fail=True))

    stream = await transcriber.open(speaker="caller", on_segment=on_segment)

    assert stream.is_live is False
    # Ni l'envoi d'audio ni la fermeture (même répétée) ne doivent lever.
    await stream.send_audio(b"\x00\x01\x02\x03")
    await stream.close()
    await stream.close()
    assert segments == []


async def test_parametres_transmis_a_aws(settings: Settings) -> None:
    _, on_segment = _collector()
    client = _FakeClient()
    stream = await mod.AwsTranscriber(settings, client=client).open(
        speaker="victim", on_segment=on_segment
    )

    assert client.calls == [
        {
            "language_code": "fr-FR",
            "media_sample_rate_hz": 8000,
            "media_encoding": "pcm",
        }
    ]
    await stream.close()


async def test_resultats_convertis_en_segments(settings: Settings) -> None:
    events = [
        _event("bonjour", is_partial=True),
        _event("bonjour", is_partial=True),  # doublon : filtré
        _event("bonjour mamie", is_partial=True),
        _event("Bonjour mamie.", is_partial=False),
        _event("Bonjour mamie.", is_partial=False),  # final répété : jamais filtré
    ]
    segments, on_segment = _collector()
    stream = await mod.AwsTranscriber(settings, client=_FakeClient(events)).open(
        speaker="caller", on_segment=on_segment
    )
    await stream.reader_task

    assert [(s.text, s.is_final) for s in segments] == [
        ("bonjour", False),
        ("bonjour mamie", False),
        ("Bonjour mamie.", True),
        ("Bonjour mamie.", True),
    ]
    assert {s.speaker for s in segments} == {"caller"}
    times = [s.t for s in segments]
    assert times == sorted(times)
    assert times[0] >= 0


async def test_partiel_reemis_apres_un_final_passe(settings: Settings) -> None:
    """Un partiel identique à un partiel d'avant le final doit repasser : c'est
    un nouveau segment de parole, pas un doublon."""
    events = [
        _event("oui", is_partial=True),
        _event("oui", is_partial=False),
        _event("oui", is_partial=True),
    ]
    segments, on_segment = _collector()
    stream = await mod.AwsTranscriber(settings, client=_FakeClient(events)).open(
        speaker="victim", on_segment=on_segment
    )
    await stream.reader_task

    assert [(s.text, s.is_final) for s in segments] == [
        ("oui", False),
        ("oui", True),
        ("oui", False),
    ]


async def test_resultats_vides_ignores(settings: Settings) -> None:
    events = [
        _event("", is_partial=True),
        _event("   ", is_partial=False),
        TranscriptEvent(Transcript([Result(is_partial=False, alternatives=[])])),
        TranscriptEvent(Transcript([])),
    ]
    segments, on_segment = _collector()
    stream = await mod.AwsTranscriber(settings, client=_FakeClient(events)).open(
        speaker="caller", on_segment=on_segment
    )
    await stream.reader_task

    assert segments == []


async def test_audio_envoye_puis_flux_ferme(settings: Settings) -> None:
    _, on_segment = _collector()
    client = _FakeClient()
    stream = await mod.AwsTranscriber(settings, client=client).open(
        speaker="caller", on_segment=on_segment
    )
    input_stream = stream._stream.input_stream

    await stream.send_audio(b"\x01\x02")
    await stream.send_audio(b"")  # rien à transcrire : pas d'aller-retour AWS
    assert input_stream.chunks == [b"\x01\x02"]

    await stream.close()
    await stream.close()
    assert input_stream.end_calls == 1
    assert stream.is_live is False

    await stream.send_audio(b"\x03\x04")  # après fermeture : no-op
    assert input_stream.chunks == [b"\x01\x02"]


async def test_erreur_d_envoi_degrade_sans_lever(settings: Settings) -> None:
    _, on_segment = _collector()
    client = _FakeClient(fail_on_send=True)
    stream = await mod.AwsTranscriber(settings, client=client).open(
        speaker="caller", on_segment=on_segment
    )

    await stream.send_audio(b"\x01\x02")

    assert stream.is_live is False
    await stream.close()


async def test_erreur_de_lecture_loguee_pas_propagee(settings: Settings) -> None:
    class _ExplodingOutput:
        async def __aiter__(self):
            yield _event("allo", is_partial=False)
            raise RuntimeError("flux interrompu par AWS")

    segments, on_segment = _collector()
    client = _FakeClient(output_stream=_ExplodingOutput())
    stream = await mod.AwsTranscriber(settings, client=client).open(
        speaker="caller", on_segment=on_segment
    )

    await stream.reader_task  # ne doit pas lever
    assert [s.text for s in segments] == ["allo"]
    await stream.close()


async def test_close_coupe_une_lecture_qui_ne_termine_pas(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod, "CLOSE_GRACE_SECONDS", 0.05)

    class _HangingOutput:
        async def __aiter__(self):
            yield _event("allo", is_partial=True)
            await asyncio.sleep(3600)

    segments, on_segment = _collector()
    client = _FakeClient(output_stream=_HangingOutput())
    stream = await mod.AwsTranscriber(settings, client=client).open(
        speaker="caller", on_segment=on_segment
    )

    await stream.close()
    await asyncio.sleep(0)  # laisse l'annulation prendre effet

    assert stream.reader_task.done()
    assert [s.text for s in segments] == ["allo"]
