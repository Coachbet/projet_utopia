"""Interfaces entre le domaine et le monde extérieur.

Des `Protocol`, pas des classes de base : une implémentation n'a rien à importer
d'ici pour être conforme. Dev 1 et Dev 2 branchent leurs adapters sans se
dépendre, et `infrastructure/fakes/` permet de tout faire tourner sans
téléphone ni AWS.

Qui implémente quoi :
  - Dev 1 : `Transcriber`, `CallController`
  - Dev 2 : `ContentAnalyzer`, `KeywordEngine`, `VoiceDetector`,
            `ReputationChecker`, `ContactBook`
  - partagé : `EventSink`
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol, runtime_checkable

from vocalguard.domain.events import Event, Speaker
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
    """Destination de tous les événements : dashboard, logs, tests."""

    async def publish(self, event: Event) -> None: ...


@runtime_checkable
class Transcriber(Protocol):
    """Audio -> segments de transcript. Amazon Transcribe en production."""

    async def stream(
        self, speaker: Speaker, audio: AsyncIterator[bytes], sample_rate: int
    ) -> AsyncIterator[TranscriptSegment]: ...


@runtime_checkable
class ContentAnalyzer(Protocol):
    """Fenêtre glissante de transcript -> scénario d'arnaque (Claude/Bedrock).

    `turns` est ordonné du plus ancien au plus récent. L'implémentation décide
    seule de sa cadence d'appel ; renvoyer `None` signifie « rien de nouveau,
    garde le signal précédent ».
    """

    async def analyze(self, turns: Sequence[TranscriptSegment]) -> ContentSignal | None: ...


@runtime_checkable
class KeywordEngine(Protocol):
    """Texte -> termes à risque pondérés. Synchrone : c'est le filet anti-latence.

    `cumulative` demande le signal pour tout l'appel depuis le début, pas pour
    le seul dernier segment.
    """

    def scan(self, text: str) -> KeywordSignal: ...

    def scan_call(self, turns: Sequence[TranscriptSegment]) -> KeywordSignal: ...


@runtime_checkable
class VoiceDetector(Protocol):
    """PCM 16 bits -> probabilité de voix synthétique.

    Appelé en flux : renvoie `None` tant qu'une fenêtre complète n'est pas
    accumulée, puis un signal lissé sur toutes les fenêtres vues.
    """

    async def feed(
        self, speaker: Speaker, pcm16: bytes, sample_rate: int
    ) -> VoiceSignal | None: ...


@runtime_checkable
class ReputationChecker(Protocol):
    """Numéro + identité revendiquée -> réputation de la ligne.

    `claimed_identity` vient de Claude et peut arriver après le début de
    l'appel : l'implémentation doit pouvoir être rappelée pour recalculer
    `identity_mismatch`.
    """

    async def check(
        self, phone_number: str, claimed_identity: str | None = None
    ) -> ReputationSignal: ...


@runtime_checkable
class ContactBook(Protocol):
    """Carnet d'adresses de la victime."""

    def by_number(self, phone_number: str) -> Contact | None: ...

    def by_identity(self, claimed_identity: str) -> Contact | None: ...

    def all(self) -> Sequence[Contact]: ...


@runtime_checkable
class CallController(Protocol):
    """Actions sur les participants de la conférence Twilio.

    Jamais de `hangup` : un faux positif sur un vrai appel d'urgence serait
    grave, la mise en attente protège autant et laisse la décision à la victime.
    """

    async def announce(self, target: str, message: str) -> bool: ...

    async def hold(self, target: str) -> bool: ...

    async def resume(self, target: str) -> bool: ...

    async def send_sms(self, to: str, message: str) -> bool: ...
