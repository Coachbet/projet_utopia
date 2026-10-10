"""État d'un appel en cours : audio Twilio → transcription → événements.

C'est la moitié Dev 1 du pipeline. Cette classe ne calcule aucun score : elle
décode l'audio, le pousse vers Transcribe, publie des événements `transcript`,
et expose à Dev 2 les deux seules choses dont son analyse a besoin — `signals`
(le dernier état connu des quatre signaux) et `window()` (la fenêtre glissante
de transcript à envoyer à Claude).

Règle de robustesse dans tout le fichier : perdre un signal est acceptable,
perdre l'appel ne l'est pas. Les pannes de transcription ou de détection sont
loguées et avalées, jamais remontées dans la boucle média.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from vocalguard.config import Settings
from vocalguard.domain.alerts import ActionKind, AlertLevel
from vocalguard.domain.events import Event, EventType, Speaker
from vocalguard.domain.models import SignalSet, TranscriptSegment
from vocalguard.infrastructure.telephony.media_stream import (
    MediaChunk,
    StreamStart,
    StreamStop,
)
from vocalguard.infrastructure.telephony.mulaw import ulaw_to_pcm16

if TYPE_CHECKING:
    from vocalguard.domain.ports import (
        AudioConsumer,
        EventSink,
        Transcriber,
        TranscriptionStream,
    )
    from vocalguard.infrastructure.telephony.media_stream import Frame

logger = logging.getLogger(__name__)

_SPEAKERS: tuple[Speaker, ...] = ("caller", "victim")

MAX_HISTORY = 400
"""Un appel qui s'éternise ne doit pas faire gonfler la mémoire : au-delà, on
oublie le début. La fenêtre envoyée à Claude ne fait que quelques tours de toute
façon."""


class CallSession:
    def __init__(
        self,
        *,
        call_id: str,
        transcriber: Transcriber,
        sink: EventSink,
        settings: Settings,
        caller_number: str = "",
        victim_number: str = "",
        audio_tap: AudioConsumer | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.call_id = call_id
        self.caller_number = caller_number
        self.victim_number = victim_number
        self.stream_sid: str | None = None
        self.history: list[TranscriptSegment] = []
        self.signals = SignalSet()
        self.acted_level = AlertLevel.NONE
        """Niveau d'alerte **entièrement** abouti : toutes ses actions ont réussi.

        Un palier dont une action a échoué laisse `acted_level` en arrière,
        exprès : c'est ce qui autorise un score suivant à repasser pour rejouer
        ce qui manque. Le détail de « ce qui manque » vit dans `actions_done`."""
        self.actions_done: dict[AlertLevel, set[ActionKind]] = {}
        """Actions déjà abouties, par palier : la mémoire fine de l'idempotence.

        Un booléen par palier ne suffirait pas. Il faut à la fois ne jamais
        rejouer une action réussie (sinon un score oscillant 66 → 70 → 67
        répéterait l'annonce dans l'oreille de la victime) et pouvoir rejouer
        celle qui a échoué (la victime décroche après l'appelant : la première
        annonce échoue parce qu'elle n'est pas encore dans la conférence).

        `InterventionRunner` y inscrit l'action **avant** de l'exécuter, et la
        retire si elle échoue : la réservation rend l'exécution unique même si
        deux événements `score` sont traités en parallèle, sans pour autant
        graver un échec dans le marbre."""
        self.caller_held = False
        self.contact_texted = False
        """Le SMS de vérification au vrai proche a abouti.

        Porté par l'appel et pas par le palier : `SMS_CONTACT` figure dans le
        plan d'`alert` **et** dans celui d'`intervene`, et le proche ne doit
        recevoir « Est-ce toi qui appelles ? » qu'une seule fois (cf.
        `plan_actions(contact_already_texted=...)`)."""
        self.audio_tap = audio_tap
        """Point de branchement de la détection de voix synthétique (Dev 2).
        Publique et modifiable après coup : le détecteur a parfois besoin de la
        session elle-même pour se construire."""

        self._transcriber = transcriber
        self._sink = sink
        self._settings = settings
        self._clock = clock
        self._started_at = clock()
        self._streams: dict[Speaker, TranscriptionStream] = {}
        self._closed = False

    def t(self) -> float:
        """Secondes depuis l'ouverture. Jamais négatif : `Event.t` l'interdit."""
        return max(0.0, self._clock() - self._started_at)

    async def start(self) -> None:
        """Ouvre une transcription par locuteur — les deux pistes Twilio sont
        séparées, c'est ce qui nous dit qui parle.

        **En parallèle**, et c'est un point de latence et pas une élégance :
        `start()` est appelé depuis `SessionRegistry.open()`, dans le chemin de
        réponse du webhook `/twilio/voice/inbound`. En série, le TwiML attendait
        la somme des deux ouvertures (mesuré : 3,01 s à 1,5 s l'ouverture) ; avec
        des identifiants expirés ou un réseau qui filtre AWS, les retries de
        Transcribe approchent le timeout webhook de Twilio (15 s) et l'appelant
        entend « an application error has occurred » avant que la conférence
        existe. En parallèle on attend la plus lente, pas les deux.

        Le `try/except` reste **dans** la coroutine par locuteur plutôt que de
        passer par `return_exceptions=True` : la panne se traite là où on sait
        de quel locuteur il s'agit, et `self._streams` ne reçoit que les pistes
        réellement ouvertes — un `gather` qui renverrait des exceptions mêlées à
        des streams obligerait à re-corréler les positions avec `_SPEAKERS`."""
        await asyncio.gather(*(self._open_stream(speaker) for speaker in _SPEAKERS))

    async def _open_stream(self, speaker: Speaker) -> None:
        """Une piste, une panne isolée : une transcription refusée ne doit jamais
        tuer l'appel ni empêcher l'autre piste de s'ouvrir."""
        try:
            stream = await self._transcriber.open(speaker=speaker, on_segment=self.on_segment)
        except Exception:
            # Transcribe peut être refusé par le rôle workshop : l'appel doit
            # quand même aboutir, les mots-clés et le reste prendront le relais.
            logger.exception("transcription indisponible pour %s", speaker)
            return
        if self._closed:
            # L'appel s'est terminé pendant l'ouverture (`StreamStop` très tôt,
            # ou un `open` lent) : `aclose()` a déjà vidé `_streams`, y déposer
            # ce stream laisserait une connexion Transcribe ouverte à vie et un
            # dictionnaire incohérent avec `_closed`.
            try:
                await stream.close()
            except Exception:
                logger.warning("fermeture de la transcription %s en échec", speaker, exc_info=True)
            return
        # Écriture après l'`await` et depuis une seule coroutine par clé : le
        # dictionnaire reste cohérent sans verrou.
        self._streams[speaker] = stream

    async def on_frame(self, frame: Frame) -> None:
        match frame:
            case StreamStart():
                self.stream_sid = frame.stream_sid
                logger.info("stream %s attaché à l'appel %s", frame.stream_sid, self.call_id)
            case MediaChunk():
                await self.on_media(frame)
            case StreamStop():
                await self.aclose()
            case _:
                # connected / mark / dtmf : rien à faire au niveau de la session.
                pass

    async def on_media(self, chunk: MediaChunk) -> None:
        speaker = chunk.speaker
        if speaker is None:
            logger.debug("piste inconnue %r ignorée", chunk.track)
            return

        pcm16 = ulaw_to_pcm16(chunk.ulaw)

        stream = self._streams.get(speaker)
        if stream is not None:
            try:
                await stream.send_audio(pcm16)
            except Exception:
                logger.warning("audio %s perdu pour la transcription", speaker, exc_info=True)

        # Seule la voix de l'appelant est testée pour la synthèse : c'est lui le
        # scammeur présumé, et analyser la victime doublerait le coût CPU pour rien.
        if self.audio_tap is not None and speaker == "caller":
            try:
                # `self.t()` et pas le timestamp Twilio : une seule base de temps
                # pour tous les événements, sinon la frise du dashboard se décale.
                await self.audio_tap.push(speaker=speaker, pcm16=pcm16, t=self.t())
            except Exception:
                logger.warning("détection de voix en échec sur un segment", exc_info=True)

        # Aucun audio n'est écrit sur disque (PERSIST_AUDIO) : c'est la réponse
        # vie privée du pitch. TODO si un jour on veut archiver : ici, et seulement
        # si `self._settings.persist_audio` est vrai.

    async def on_segment(self, segment: TranscriptSegment) -> None:
        if segment.is_final:
            self.history.append(segment)
            if len(self.history) > MAX_HISTORY:
                del self.history[: len(self.history) - MAX_HISTORY]
            # TODO si PERSIST_TRANSCRIPTS passe à true : archiver ici. Par défaut
            # rien n'est persisté, le transcript meurt avec le process.
        await self.emit(
            EventType.TRANSCRIPT,
            {"text": segment.text, "is_final": segment.is_final},
            speaker=segment.speaker,
            t=segment.t,
        )

    def window(self, turns: int | None = None) -> tuple[TranscriptSegment, ...]:
        """Les N derniers tours **finaux**.

        Jamais de partiels : Claude analyserait du texte encore instable et
        changerait d'avis d'un appel à l'autre.
        """
        count = self._settings.llm_transcript_window_turns if turns is None else turns
        if count <= 0:
            return ()
        return tuple(self.history[-count:])

    async def emit(
        self,
        type: EventType,
        data: dict[str, Any],
        *,
        speaker: Speaker | None = None,
        t: float | None = None,
    ) -> None:
        event = Event(
            call_id=self.call_id,
            t=self.t() if t is None else max(0.0, t),
            type=type,
            speaker=speaker,
            data=data,
        )
        try:
            await self._sink.emit(event)
        except Exception:
            logger.warning("événement %s non diffusé", type.value, exc_info=True)

    async def aclose(self) -> None:
        """Idempotent et silencieux : appelé à la fois par `stop` et par le
        callback de statut Twilio, qui arrivent dans un ordre imprévisible."""
        if self._closed:
            return
        self._closed = True
        for speaker, stream in self._streams.items():
            try:
                await stream.close()
            except Exception:
                logger.warning("fermeture de la transcription %s en échec", speaker, exc_info=True)
        self._streams.clear()


class SessionRegistry:
    """Les appels actifs, par `call_id`.

    Mémoire seulement : une démo de hackathon n'a pas besoin de survivre à un
    redémarrage, et ça évite une dépendance de plus à la base.

    `clock` est un point d'extension et pas un détail de test : le rejeu de
    scénario (`make replay`) doit pouvoir imposer le temps du scénario, parce que
    seul l'événement `transcript` porte un `t` explicite — tous les événements
    dérivés (`keyword`, `llm`, `reputation`, `score`, `action`) prennent celui de
    `CallSession.t()`. Avec l'horloge murale et `--no-wait`, ils s'empilent donc
    tous à ~0 s alors que les transcripts s'étalent sur la durée de l'appel : la
    frise du dashboard devient illisible et `make replay` ne répète plus la démo,
    il la ment.
    """

    def __init__(
        self,
        *,
        transcriber_factory: Callable[[], Transcriber],
        sink: EventSink,
        settings: Settings,
        audio_tap_factory: Callable[[CallSession], AudioConsumer | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transcriber_factory = transcriber_factory
        self._sink = sink
        self._settings = settings
        self._audio_tap_factory = audio_tap_factory
        self._clock = clock
        self._sessions: dict[str, CallSession] = {}

    async def open(
        self,
        call_id: str,
        *,
        caller_number: str = "",
        victim_number: str = "",
        clock: Callable[[], float] | None = None,
    ) -> CallSession:
        """Ouvre (ou retrouve) la session de `call_id`.

        `clock=None` signifie « celle du registre » : un appelant qui n'a rien à
        dire sur le temps n'a rien à passer, et le rejeu peut quand même imposer
        son horloge appel par appel.
        """
        existing = self._sessions.get(call_id)
        if existing is not None:
            # Twilio rejoue un webhook en cas de timeout : ouvrir deux fois la
            # même session dédoublerait transcript et score.
            logger.info("session %s déjà ouverte, réutilisée", call_id)
            return existing

        session = CallSession(
            call_id=call_id,
            transcriber=self._transcriber_factory(),
            sink=self._sink,
            settings=self._settings,
            caller_number=caller_number,
            victim_number=victim_number,
            clock=self._clock if clock is None else clock,
        )
        if self._audio_tap_factory is not None:
            session.audio_tap = self._audio_tap_factory(session)
        # Enregistrée AVANT `start()`, qui suspend désormais (les deux
        # transcriptions s'ouvrent en parallèle) : sans ça, deux webhooks Twilio
        # rejoués en concurrence passeraient tous les deux le test d'existence
        # ci-dessus et ouvriraient deux sessions pour le même appel. Le prix est
        # qu'une session peut être visible avant que ses pistes soient ouvertes —
        # l'audio reçu dans cette fenêtre de quelques millisecondes est ignoré,
        # ce qui vaut mieux que de perdre l'appel (ou de le dédoubler).
        self._sessions[call_id] = session
        await session.start()
        return session

    def get(self, call_id: str) -> CallSession | None:
        return self._sessions.get(call_id)

    def by_stream(self, stream_sid: str) -> CallSession | None:
        """Parcours linéaire assumé : il y a un, parfois deux appels en vol.
        Un index de plus serait un état de plus à invalider."""
        for session in self._sessions.values():
            if session.stream_sid == stream_sid:
                return session
        return None

    async def close(self, call_id: str) -> None:
        session = self._sessions.pop(call_id, None)
        if session is not None:
            await session.aclose()

    def active(self) -> list[CallSession]:
        return list(self._sessions.values())
