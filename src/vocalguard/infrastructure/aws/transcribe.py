"""Adapter Amazon Transcribe streaming — implémente les ports `Transcriber` /
`TranscriptionStream`.

Une session de transcription **par locuteur**, et non un seul flux à deux canaux :
les pistes Twilio `inbound` (appelant) et `outbound` (victime) arrivent déjà
séparées dans le `<Stream>`, et c'est cette séparation qui nous dit qui parle.
L'identification de canal de Transcribe fusionnerait les deux transcriptions dans
un seul flux de résultats, ce qui nous ferait perdre gratuitement l'information.

Le flux est en PCM 16 bits mono 8 kHz : le téléphone ne transporte pas mieux, et
le streaming Transcribe accepte 8 kHz avec `fr-FR`. Suréchantillonner ici
n'apporterait aucune information et coûterait du temps de calcul.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any

from amazon_transcribe.auth import StaticCredentialResolver
from amazon_transcribe.client import TranscribeStreamingClient
from amazon_transcribe.handlers import TranscriptResultStreamHandler

from vocalguard.domain.models import TranscriptSegment
from vocalguard.infrastructure.aws.credentials import AWSCredentialsError, aws_session

if TYPE_CHECKING:
    from collections.abc import Callable

    from vocalguard.config import Settings
    from vocalguard.domain.events import Speaker
    from vocalguard.domain.ports import SegmentCallback

logger = logging.getLogger(__name__)

REQUIRED_IAM_ACTION = "transcribe:StartStreamTranscription"
"""Permission à vérifier à H0 : le rôle du workshop ne l'accorde pas toujours.
Sans elle, l'ouverture du flux échoue et on bascule en mode dégradé."""

MEDIA_ENCODING = "pcm"

CLOSE_GRACE_SECONDS = 2.0
"""Délai laissé au SDK pour livrer les derniers résultats après `end_stream()`.
Borné : raccrocher ne doit jamais attendre AWS."""


class _SegmentEmitter:
    """Conversion des résultats Transcribe en `TranscriptSegment`, et filtrage
    des partiels répétés.

    Isolé du SDK pour être testable sans réseau : c'est ici que vit toute la
    logique, le reste n'est que du câblage.

    Transcribe réémet un résultat partiel à chaque mot reconnu, souvent à
    l'identique. On transmet quand même les partiels — le dashboard affiche le
    texte en train de se construire — mais on saute les doublons pour ne pas
    inonder le bus d'événements. Les segments finaux ne sont jamais filtrés :
    c'est sur eux que Dev 2 déclenche Claude, en perdre un coûterait une analyse.
    """

    def __init__(
        self,
        *,
        speaker: Speaker,
        on_segment: SegmentCallback,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._speaker = speaker
        self._on_segment = on_segment
        self._clock = clock
        # Origine de l'horloge : le dashboard et le rejeu de scénario ont besoin
        # d'un temps relatif au début de l'appel, pas d'un timestamp absolu.
        self._origin = clock()
        self._last_partial: str | None = None

    async def feed(self, transcript_event: Any) -> None:
        for result in transcript_event.transcript.results or ():
            await self._feed_result(result)

    async def _feed_result(self, result: Any) -> None:
        alternatives = result.alternatives or ()
        if not alternatives:
            return
        text = (alternatives[0].transcript or "").strip()
        if not text:
            return

        is_final = not result.is_partial
        if is_final:
            # Le segment est stabilisé : le prochain partiel repart de zéro.
            self._last_partial = None
        else:
            if text == self._last_partial:
                return
            self._last_partial = text

        await self._on_segment(
            TranscriptSegment(
                speaker=self._speaker,
                text=text,
                t=round(self._clock() - self._origin, 3),
                is_final=is_final,
            )
        )


class _StreamHandler(TranscriptResultStreamHandler):
    """Passerelle entre la boucle de lecture du SDK et notre émetteur."""

    def __init__(self, output_stream: Any, emitter: _SegmentEmitter) -> None:
        super().__init__(output_stream)
        self._emitter = emitter

    async def handle_transcript_event(self, transcript_event: Any) -> None:
        await self._emitter.feed(transcript_event)


class AwsTranscriptionStream:
    """Un flux de transcription ouvert pour un locuteur.

    Peut être *inerte* (`stream is None`) : l'appel doit survivre à une
    transcription indisponible, les mots-clés et Claude continuent de tourner sur
    ce que l'autre piste produit. Un flux inerte jette l'audio en silence.
    """

    def __init__(
        self,
        *,
        speaker: Speaker,
        stream: Any | None = None,
        reader_task: asyncio.Task[None] | None = None,
    ) -> None:
        self.speaker = speaker
        self.reader_task = reader_task
        self._stream = stream
        self._closed = False

    @property
    def is_live(self) -> bool:
        return self._stream is not None and not self._closed

    async def send_audio(self, pcm16: bytes) -> None:
        if not self.is_live or not pcm16:
            return
        try:
            await self._stream.input_stream.send_audio_event(audio_chunk=pcm16)
        except Exception as exc:
            # Une coupure côté AWS en pleine démo ne doit pas remonter jusqu'au
            # WebSocket Twilio : on abandonne la transcription, pas l'appel.
            logger.error(
                "Transcribe (%s) : envoi audio impossible, flux abandonné (%s)",
                self.speaker,
                type(exc).__name__,
            )
            self._stream = None

    async def close(self) -> None:
        """Idempotent et silencieux : appelé depuis le `finally` de la session
        d'appel, parfois deux fois (fin de stream Twilio puis fin d'appel)."""
        if self._closed:
            return
        self._closed = True

        stream, self._stream = self._stream, None
        if stream is not None:
            with contextlib.suppress(Exception):
                await stream.input_stream.end_stream()

        task = self.reader_task
        if task is not None and not task.done():
            # On laisse un court instant au SDK pour livrer les derniers
            # résultats, puis on coupe : la fin d'appel ne doit pas attendre AWS.
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), timeout=CLOSE_GRACE_SECONDS)
            if not task.done():
                task.cancel()


class AwsTranscriber:
    """Fabrique de flux Transcribe. Un instance par process suffit."""

    def __init__(self, settings: Settings, *, client: Any | None = None) -> None:
        self._settings = settings
        # Client construit à la demande : son initialisation démarre une boucle
        # d'événements CRT, inutile de la payer si on tourne sur un faux
        # transcriber ou si aucun appel n'arrive.
        self._client = client

    def _ensure_client(self) -> Any:
        if self._client is None:
            credentials = aws_session(self._settings).get_credentials()
            if credentials is None:
                raise AWSCredentialsError(
                    "Aucun identifiant AWS configuré (.env, exports ou profil)."
                )
            frozen = credentials.get_frozen_credentials()
            self._client = TranscribeStreamingClient(
                region=self._settings.transcribe_region,
                credential_resolver=StaticCredentialResolver(
                    frozen.access_key, frozen.secret_key, frozen.token
                ),
            )
        return self._client

    async def open(
        self, *, speaker: Speaker, on_segment: SegmentCallback
    ) -> AwsTranscriptionStream:
        emitter = _SegmentEmitter(speaker=speaker, on_segment=on_segment)
        settings = self._settings
        try:
            stream = await self._ensure_client().start_stream_transcription(
                language_code=settings.transcribe_language_code,
                media_sample_rate_hz=settings.transcribe_sample_rate,
                media_encoding=MEDIA_ENCODING,
            )
        except Exception as exc:
            logger.error(
                "Transcribe indisponible pour la piste %s (%s). "
                "Vérifier que le rôle AWS autorise %s dans la région %s, "
                "ou basculer TRANSCRIBE_BACKEND. L'appel continue sans transcription "
                "de cette piste.",
                speaker,
                type(exc).__name__,
                REQUIRED_IAM_ACTION,
                settings.transcribe_region,
            )
            return AwsTranscriptionStream(speaker=speaker)

        handler = _StreamHandler(stream.output_stream, emitter)
        reader_task = asyncio.create_task(
            self._drain(handler, speaker), name=f"transcribe-{speaker}"
        )
        logger.info(
            "Transcribe ouvert pour %s (%s, %d Hz)",
            speaker,
            settings.transcribe_language_code,
            settings.transcribe_sample_rate,
        )
        return AwsTranscriptionStream(speaker=speaker, stream=stream, reader_task=reader_task)

    @staticmethod
    async def _drain(handler: _StreamHandler, speaker: Speaker) -> None:
        """Boucle de lecture : toute erreur est loguée, jamais propagée — une
        tâche qui meurt en silence est plus facile à diagnostiquer qu'un appel
        qui tombe."""
        try:
            await handler.handle_events()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "Transcribe (%s) : flux de résultats interrompu (%s)", speaker, type(exc).__name__
            )
