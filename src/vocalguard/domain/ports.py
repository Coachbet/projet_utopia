"""Ports : les interfaces que le domaine attend, implémentées dans `infrastructure/`.

C'est la frontière entre les deux devs. Dev 1 fournit `Transcriber`,
`CallController` et l'audio ; Dev 2 fournit `ContentAnalyzer`, `KeywordEngine`,
`VoiceDetector`, `ReputationChecker`, `ContactBook`. Chacun avance contre ces
protocoles avec une doublure (`infrastructure/fakes/`) tant que l'autre moitié
n'existe pas.

Des `Protocol` et pas des classes de base : on n'hérite de rien, un adapter est
conforme s'il a les bonnes méthodes.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from vocalguard.domain.events import Event, Speaker
from vocalguard.domain.history import CallRecord, HistoryPage
from vocalguard.domain.models import (
    Contact,
    ContentSignal,
    KeywordSignal,
    ReputationSignal,
    TranscriptSegment,
    VoiceSignal,
)


@runtime_checkable
class EventSink(Protocol):
    """Sortie unique du système : tout passe par là (dashboard, logs, pipeline).

    Implémentations : le hub du dashboard, `FanOutSink` pour en brancher
    plusieurs, et les doublures de `infrastructure/fakes/`.
    """

    async def emit(self, event: Event) -> None: ...


@runtime_checkable
class CallHistory(Protocol):
    """Historique local de métadonnées seulement, jamais de contenu d'appel."""

    enabled: bool

    async def initialize(self) -> None: ...

    async def save(self, record: CallRecord) -> None: ...

    async def list_calls(self, *, offset: int = 0, limit: int = 50) -> HistoryPage: ...

    async def get_call(self, call_id: str) -> CallRecord | None: ...

    async def clear(self) -> int: ...


@runtime_checkable
class VoicemailStore(Protocol):
    """Métadonnées et autorisations de dépôt ; aucun fichier audio local."""

    async def initialize(self) -> None: ...
    async def expect(self, call_id: str, number: str, name: str) -> None: ...
    async def capture(self, call_id: str, recording_sid: str, duration_s: int) -> bool: ...
    async def list_messages(self, *, offset: int = 0, limit: int = 50) -> dict[str, object]: ...
    async def get(self, message_id: str) -> dict[str, object] | None: ...
    async def mark_heard(self, message_id: str) -> None: ...
    async def mark_deleted(self, message_id: str) -> None: ...
    async def expired(self) -> list[dict[str, object]]: ...
    async def purge_metadata(self) -> None: ...


@runtime_checkable
class RecordingFiles(Protocol):
    """Messages vocaux distants, accessibles uniquement côté serveur."""

    async def audio(self, sid: str) -> bytes: ...
    async def delete(self, sid: str) -> None: ...


# --- Dev 1 : téléphonie et temps réel --------------------------------------


class SegmentCallback(Protocol):
    """Appelée par un `TranscriptionStream` dès qu'un segment sort.

    Seul port volontairement non `runtime_checkable` : c'est un protocole
    d'appelable, et `isinstance` contre `__call__` est trompeur — toute fonction,
    toute méthode liée, tout objet appelable le satisfait, y compris une fonction
    synchrone qui bloquerait la boucle média. Le contrôle utile est celui de
    `test_port_conformance.py`, qui compare les signatures.
    """

    async def __call__(self, segment: TranscriptSegment) -> None: ...


@runtime_checkable
class TranscriptionStream(Protocol):
    """Une session de transcription, ouverte pour une piste audio (un locuteur)."""

    async def send_audio(self, pcm16: bytes) -> None:
        """PCM 16 bits signé, mono, à `transcribe_sample_rate` Hz."""
        ...

    async def close(self) -> None: ...


@runtime_checkable
class Transcriber(Protocol):
    """Dev 1 — Amazon Transcribe streaming, ou une doublure."""

    async def open(
        self, *, speaker: Speaker, on_segment: SegmentCallback
    ) -> TranscriptionStream: ...


@runtime_checkable
class AudioConsumer(Protocol):
    """Dérivation de l'audio brut vers un autre traitement que la transcription.

    C'est le point de branchement de la détection de voix synthétique (Dev 2) :
    la session d'appel pousse ici le PCM décodé de l'appelant.
    """

    async def push(self, *, speaker: Speaker, pcm16: bytes, t: float) -> None: ...


@runtime_checkable
class CallController(Protocol):
    """Dev 1 — actions sur l'appel en cours, via l'API Twilio Participants.

    Les méthodes portent le `call_id` : Twilio a besoin du CallSid pour viser le
    bon participant, et un contrôleur ne garde aucun état d'appel.

    Jamais de `hangup` : cf. `domain.alerts.plan_actions`.
    """

    async def hold_caller(self, call_id: str) -> bool: ...
    async def resume_caller(self, call_id: str) -> bool: ...
    async def announce_to_victim(self, call_id: str, message: str) -> bool: ...
    async def send_sms(self, to_number: str, message: str) -> bool: ...


# --- Dev 2 : intelligence et score -----------------------------------------


@runtime_checkable
class ContentAnalyzer(Protocol):
    """Dev 2 — Claude sur Bedrock, sortie JSON imposée.

    `window` est ordonnée du plus ancien au plus récent. Renvoyer `None` signifie
    « rien de nouveau, garde le signal précédent » : c'est ce qui permet à
    l'implémentation d'imposer sa propre cadence (`LLM_MIN_INTERVAL_SECONDS`) et
    de dégrader en silence si Bedrock est injoignable.
    """

    async def analyze(self, window: Sequence[TranscriptSegment]) -> ContentSignal | None: ...


@runtime_checkable
class KeywordEngine(Protocol):
    """Dev 2 — regex pondérées, FR + EN. Synchrone : c'est le filet anti-latence.

    `scan_call` travaille sur tout l'appel et pas sur le dernier segment : le
    coupe-circuit `keywords_alone_watch` juge l'accumulation, pas une phrase.
    """

    def scan(self, text: str) -> KeywordSignal: ...

    def scan_call(self, turns: Sequence[TranscriptSegment]) -> KeywordSignal: ...


@runtime_checkable
class VoiceDetector(Protocol):
    """Dev 2 — wav2vec2. Renvoie `None` tant qu'une fenêtre n'est pas complète."""

    async def feed(
        self, speaker: Speaker, pcm16: bytes, sample_rate: int
    ) -> VoiceSignal | None: ...


@runtime_checkable
class ReputationChecker(Protocol):
    """Dev 2 — Twilio Lookup v2 + carnet d'adresses.

    `claimed_identity` vient de Claude et arrive en cours d'appel : on rappelle
    `check` pour recalculer `identity_mismatch`.
    """

    async def check(
        self, phone_number: str, *, claimed_identity: str | None = None
    ) -> ReputationSignal: ...


@runtime_checkable
class ContactBook(Protocol):
    """Dev 2 — carnet d'adresses de la victime."""

    def by_number(self, phone_number: str) -> Contact | None: ...

    def by_identity(self, claimed_identity: str) -> Contact | None: ...

    def all(self) -> Sequence[Contact]: ...
