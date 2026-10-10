"""Doublure du port `Transcriber` : du transcript sans AWS ni téléphone.

Sert deux usages : les tests unitaires, et le script de replay qui injecte le
texte d'un scénario écrit comme s'il sortait de Transcribe. L'audio reçu n'est
pas analysé mais il est compté, parce que la question qu'on se pose en debug est
« est-ce que l'audio arrive jusqu'au bon locuteur ? », pas « que dit-il ».
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from vocalguard.domain.events import Speaker
from vocalguard.domain.models import TranscriptSegment

if TYPE_CHECKING:
    from vocalguard.domain.ports import SegmentCallback

logger = logging.getLogger(__name__)


class FakeTranscriptionStream:
    """Avale l'audio et tient le compte des octets reçus."""

    def __init__(self, parent: FakeTranscriber, speaker: Speaker) -> None:
        self._parent = parent
        self._speaker = speaker
        self.closed = False

    async def send_audio(self, pcm16: bytes) -> None:
        self._parent.bytes_received[self._speaker] += len(pcm16)

    async def close(self) -> None:
        self.closed = True


class FakeTranscriber:
    def __init__(self) -> None:
        self.bytes_received: dict[Speaker, int] = {"caller": 0, "victim": 0}
        self.streams: dict[Speaker, FakeTranscriptionStream] = {}
        self._callbacks: dict[Speaker, SegmentCallback] = {}

    async def open(
        self, *, speaker: Speaker, on_segment: SegmentCallback
    ) -> FakeTranscriptionStream:
        stream = FakeTranscriptionStream(self, speaker)
        self.streams[speaker] = stream
        self._callbacks[speaker] = on_segment
        return stream

    async def say(self, speaker: Speaker, text: str, *, t: float, is_final: bool = True) -> None:
        """Injecte un segment comme s'il venait de Transcribe.

        Lève si aucun stream n'est ouvert : pendant un replay, du texte avalé en
        silence est le genre de panne qu'on met vingt minutes à comprendre.
        """
        callback = self._callbacks.get(speaker)
        if callback is None:
            raise RuntimeError(
                f"aucun stream de transcription ouvert pour {speaker!r} : "
                "appeler CallSession.start() avant d'injecter du texte"
            )
        await callback(TranscriptSegment(speaker=speaker, text=text, t=t, is_final=is_final))
