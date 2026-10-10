"""Ports : les interfaces que le domaine attend, implémentées dans `infrastructure/`.

C'est la frontière entre les deux devs. Dev 1 fournit `Transcriber`,
`CallController` et l'audio ; Dev 2 fournit `ContentAnalyzer`, `KeywordScorer`,
`VoiceDetector`, `ReputationChecker`. Chacun peut avancer contre ces protocoles
avec une doublure (`infrastructure/fakes/`) tant que l'autre moitié n'existe pas.

Des `Protocol` et pas des classes de base : on n'hérite de rien, un adapter est
conforme s'il a les bonnes méthodes.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from vocalguard.domain.events import Event, Speaker
from vocalguard.domain.models import (
    ContentSignal,
    KeywordSignal,
    ReputationSignal,
    TranscriptSegment,
    VoiceSignal,
)


@runtime_checkable
class EventSink(Protocol):
    """Sortie unique du système : tout passe par là (dashboard, logs, pipeline).

    Implémentations : le bus d'événements de Dev 2, et `fakes.InMemoryEventSink`
    pour les tests.
    """

    async def emit(self, event: Event) -> None: ...


class SegmentCallback(Protocol):
    """Appelée par un `TranscriptionStream` dès qu'un segment sort."""

    async def __call__(self, segment: TranscriptSegment) -> None: ...


class TranscriptionStream(Protocol):
    """Une session de transcription, ouverte pour une piste audio (un locuteur)."""

    async def send_audio(self, pcm16: bytes) -> None:
        """PCM 16 bits signé, mono, à `transcribe_sample_rate` Hz."""
        ...

    async def close(self) -> None: ...


class Transcriber(Protocol):
    """Dev 1 — Amazon Transcribe streaming, ou une doublure."""

    async def open(
        self, *, speaker: Speaker, on_segment: SegmentCallback
    ) -> TranscriptionStream: ...


class AudioConsumer(Protocol):
    """Dérivation de l'audio brut vers un autre traitement que la transcription.

    C'est le point de branchement de la détection de voix synthétique (Dev 2) :
    la session d'appel pousse ici le PCM décodé de l'appelant.
    """

    async def push(self, *, speaker: Speaker, pcm16: bytes, t: float) -> None: ...


class CallController(Protocol):
    """Dev 1 — actions sur l'appel en cours, via l'API Twilio Participants.

    Jamais de `hangup` : cf. `domain.alerts.plan_actions`.
    """

    async def hold_caller(self, call_id: str) -> bool: ...
    async def resume_caller(self, call_id: str) -> bool: ...
    async def announce_to_victim(self, call_id: str, message: str) -> bool: ...
    async def send_sms(self, to_number: str, message: str) -> bool: ...


class ContentAnalyzer(Protocol):
    """Dev 2 — Claude sur Bedrock, sortie JSON imposée."""

    async def analyze(self, window: tuple[TranscriptSegment, ...]) -> ContentSignal: ...


class KeywordScorer(Protocol):
    """Dev 2 — regex pondérées, FR + EN. Synchrone : c'est le filet anti-latence."""

    def score(self, text: str) -> KeywordSignal: ...


class VoiceDetector(Protocol):
    """Dev 2 — wav2vec2. Renvoie `None` tant qu'une fenêtre n'est pas complète."""

    async def push(self, pcm16: bytes) -> VoiceSignal | None: ...


class ReputationChecker(Protocol):
    """Dev 2 — Twilio Lookup v2 + carnet d'adresses."""

    async def check(
        self, phone_number: str, *, claimed_identity: str | None = None
    ) -> ReputationSignal: ...
