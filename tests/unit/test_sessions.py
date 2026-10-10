"""La session d'appel : audio → transcription → événements, sans téléphone ni AWS."""

from __future__ import annotations

import asyncio
from typing import Any

from vocalguard.application.sessions import MAX_HISTORY, CallSession, SessionRegistry
from vocalguard.config import Settings
from vocalguard.domain.events import EventType
from vocalguard.domain.models import TranscriptSegment
from vocalguard.infrastructure.fakes.event_sink import InMemoryEventSink
from vocalguard.infrastructure.fakes.transcriber import FakeTranscriber
from vocalguard.infrastructure.telephony.media_stream import (
    MediaChunk,
    MediaFormat,
    StreamStart,
    StreamStop,
)

ULAW_FRAME = bytes(range(256))
"""256 échantillons μ-law, soit 512 octets une fois décodés en PCM 16 bits."""

MEDIA_FORMAT = MediaFormat(encoding="audio/x-mulaw", sample_rate=8000, channels=1)


def _settings(**overrides: Any) -> Settings:
    # `_env_file=None` : le test ne doit pas dépendre d'un .env présent sur la machine.
    return Settings(_env_file=None, **overrides)


def _chunk(track: str = "inbound", speaker: str | None = "caller") -> MediaChunk:
    return MediaChunk(
        stream_sid="MZ1",
        track=track,
        speaker=speaker,
        chunk=1,
        timestamp_ms=20,
        ulaw=ULAW_FRAME,
    )


def _session(
    *, settings: Settings | None = None, **kwargs: Any
) -> tuple[CallSession, FakeTranscriber, InMemoryEventSink]:
    transcriber = FakeTranscriber()
    sink = InMemoryEventSink()
    session = CallSession(
        call_id="CA1",
        transcriber=transcriber,
        sink=sink,
        settings=settings or _settings(),
        **kwargs,
    )
    return session, transcriber, sink


async def test_media_chunk_atteint_le_stream_du_bon_locuteur() -> None:
    session, transcriber, _ = _session()
    await session.start()

    await session.on_media(_chunk())

    assert transcriber.bytes_received["caller"] == 2 * len(ULAW_FRAME)
    assert transcriber.bytes_received["victim"] == 0


async def test_les_deux_transcriptions_s_ouvrent_en_parallele() -> None:
    """`start()` est dans le chemin de réponse du webhook `/twilio/voice/inbound`.

    En série, le TwiML attendait la somme des ouvertures ; avec des identifiants
    expirés les retries de Transcribe approchent le timeout Twilio de 15 s et
    l'appelant entend « an application error has occurred ». Le test mesure que
    la latence est celle de la plus lente, pas celle des deux.
    """
    barriere = asyncio.Barrier(2)

    class ConcurrentTranscriber(FakeTranscriber):
        async def open(self, *, speaker: Any, on_segment: Any) -> Any:
            # Ne se débloque que si les deux ouvertures sont en vol en même
            # temps : en série, ce `wait` expire.
            await asyncio.wait_for(barriere.wait(), timeout=1.0)
            return await super().open(speaker=speaker, on_segment=on_segment)

    transcriber = ConcurrentTranscriber()
    session = CallSession(
        call_id="CA1", transcriber=transcriber, sink=InMemoryEventSink(), settings=_settings()
    )

    await session.start()

    assert set(transcriber.streams) == {"caller", "victim"}


async def test_une_piste_qui_refuse_de_s_ouvrir_laisse_vivre_l_autre() -> None:
    """Une transcription refusée ne doit jamais tuer l'appel, et le passage en
    parallèle ne doit pas laisser `_streams` incohérent."""

    class VictimRefused(FakeTranscriber):
        async def open(self, *, speaker: Any, on_segment: Any) -> Any:
            if speaker == "victim":
                raise RuntimeError("AccessDeniedException : rôle workshop")
            return await super().open(speaker=speaker, on_segment=on_segment)

    transcriber = VictimRefused()
    session = CallSession(
        call_id="CA1", transcriber=transcriber, sink=InMemoryEventSink(), settings=_settings()
    )

    await session.start()

    assert set(transcriber.streams) == {"caller"}
    # L'audio de l'appelant passe quand même, celui de la victime est ignoré.
    await session.on_media(_chunk())
    await session.on_media(_chunk(track="outbound", speaker="victim"))
    assert transcriber.bytes_received["caller"] == 2 * len(ULAW_FRAME)
    await session.aclose()


async def test_un_appel_termine_pendant_l_ouverture_ne_laisse_pas_de_stream_ouvert() -> None:
    """`start()` n'est plus instantané : un `StreamStop` peut arriver pendant
    l'ouverture. Le stream qui atterrit après `aclose()` doit être refermé, pas
    déposé dans un dictionnaire déjà vidé."""
    lent = asyncio.Event()

    class SlowTranscriber(FakeTranscriber):
        async def open(self, *, speaker: Any, on_segment: Any) -> Any:
            await lent.wait()
            return await super().open(speaker=speaker, on_segment=on_segment)

    transcriber = SlowTranscriber()
    session = CallSession(
        call_id="CA1", transcriber=transcriber, sink=InMemoryEventSink(), settings=_settings()
    )

    ouverture = asyncio.create_task(session.start())
    await asyncio.sleep(0)
    await session.aclose()
    lent.set()
    await ouverture

    assert all(stream.closed for stream in transcriber.streams.values())


async def test_piste_inconnue_ignoree() -> None:
    session, transcriber, _ = _session()
    await session.start()

    await session.on_media(_chunk(track="mystere", speaker=None))

    assert transcriber.bytes_received == {"caller": 0, "victim": 0}


async def test_segment_final_emet_un_evenement_conforme_au_contrat() -> None:
    session, transcriber, sink = _session()
    await session.start()

    await transcriber.say("caller", "Mamie c'est Lucas", t=1.5)

    (event,) = sink.by_type(EventType.TRANSCRIPT)
    assert event.call_id == "CA1"
    assert event.speaker == "caller"
    assert event.t == 1.5
    assert event.data == {"text": "Mamie c'est Lucas", "is_final": True}
    assert [segment.text for segment in session.history] == ["Mamie c'est Lucas"]


async def test_les_partiels_sont_diffuses_mais_pas_memorises() -> None:
    session, transcriber, sink = _session()
    await session.start()

    await transcriber.say("caller", "Mamie c'est", t=1.0, is_final=False)

    assert session.history == []
    assert sink.by_type("transcript")[0].data["is_final"] is False


async def test_window_sans_partiels_et_bornee() -> None:
    session, transcriber, _ = _session()
    await session.start()
    for index in range(5):
        await transcriber.say("caller", f"tour {index}", t=float(index))
    await transcriber.say("caller", "texte instable", t=9.0, is_final=False)

    window = session.window(3)

    assert [segment.text for segment in window] == ["tour 2", "tour 3", "tour 4"]
    assert all(segment.is_final for segment in window)
    assert session.window(0) == ()


async def test_window_par_defaut_suit_la_config() -> None:
    session, transcriber, _ = _session(settings=_settings(llm_transcript_window_turns=2))
    await session.start()
    for index in range(4):
        await transcriber.say("caller", f"tour {index}", t=float(index))

    assert [segment.text for segment in session.window()] == ["tour 2", "tour 3"]


async def test_history_oublie_le_debut_plutot_que_de_gonfler() -> None:
    session, transcriber, _ = _session()
    await session.start()
    for index in range(MAX_HISTORY + 10):
        await transcriber.say("caller", f"tour {index}", t=float(index))

    assert len(session.history) == MAX_HISTORY
    assert session.history[0].text == "tour 10"


async def test_le_tap_audio_ne_recoit_que_l_appelant() -> None:
    class RecordingTap:
        def __init__(self) -> None:
            self.pushes: list[tuple[str, int, float]] = []

        async def push(self, *, speaker: str, pcm16: bytes, t: float) -> None:
            self.pushes.append((speaker, len(pcm16), t))

    tap = RecordingTap()
    session, _, _ = _session(audio_tap=tap)
    await session.start()

    await session.on_media(_chunk())
    await session.on_media(_chunk(track="outbound", speaker="victim"))

    assert [push[0] for push in tap.pushes] == ["caller"]
    assert tap.pushes[0][1] == 2 * len(ULAW_FRAME)


async def test_un_detecteur_qui_casse_ne_casse_pas_l_appel() -> None:
    class BrokenTap:
        async def push(self, **_: Any) -> None:
            raise RuntimeError("modèle de voix en vrac")

    session, transcriber, _ = _session(audio_tap=BrokenTap())
    await session.start()

    await session.on_media(_chunk())

    assert transcriber.bytes_received["caller"] == 2 * len(ULAW_FRAME)


async def test_un_sink_qui_casse_ne_casse_pas_la_transcription() -> None:
    class BrokenSink:
        async def emit(self, _: Any) -> None:
            raise RuntimeError("dashboard déconnecté")

    transcriber = FakeTranscriber()
    session = CallSession(
        call_id="CA1", transcriber=transcriber, sink=BrokenSink(), settings=_settings()
    )
    await session.start()

    await transcriber.say("caller", "texte", t=0.5)

    assert len(session.history) == 1


async def test_aclose_deux_fois_ne_leve_pas() -> None:
    session, transcriber, _ = _session()
    await session.start()

    await session.aclose()
    await session.aclose()

    assert all(stream.closed for stream in transcriber.streams.values())


async def test_closed_session_drops_late_transcripts_audio_dtmf_and_actions() -> None:
    session, transcriber, sink = _session()
    await session.start()
    await transcriber.say("caller", "Conversation avant fermeture", t=1)
    assert session.history
    previous_events = len(sink.events)
    digits = []

    async def dtmf(call_id: str, digit: str) -> None:
        digits.append(digit)

    session.dtmf_hook = dtmf
    await session.aclose()
    await transcriber.say("caller", "CODE_SECRET_FINAL_4821", t=2)
    await session.on_segment(TranscriptSegment("victim", "CODE_SECRET_4821", 2, True))
    await session.on_media(_chunk())
    await session._on_dtmf("1")
    await session.emit(EventType.ACTION, {"action": "verify_requested", "code": "4821"})
    assert session.history == []
    assert len(sink.events) == previous_events
    assert transcriber.bytes_received == {"caller": 0, "victim": 0}
    assert digits == []


async def test_la_frame_stop_ferme_la_session() -> None:
    session, transcriber, _ = _session()
    await session.start()

    await session.on_frame(StreamStop(stream_sid="MZ1", call_sid="CA1"))

    assert all(stream.closed for stream in transcriber.streams.values())


async def test_le_registre_retrouve_une_session_par_stream_sid() -> None:
    registry = SessionRegistry(
        transcriber_factory=FakeTranscriber, sink=InMemoryEventSink(), settings=_settings()
    )
    session = await registry.open("CA1", caller_number="+15550001")

    await session.on_frame(
        StreamStart(
            stream_sid="MZ9",
            call_sid="CA1",
            tracks=("inbound", "outbound"),
            media_format=MEDIA_FORMAT,
            custom_parameters={},
        )
    )

    assert session.stream_sid == "MZ9"
    assert registry.by_stream("MZ9") is session
    assert registry.by_stream("MZinconnu") is None
    # Twilio rejoue ses webhooks : la deuxième ouverture doit rendre la même session.
    assert await registry.open("CA1") is session

    await registry.close("CA1")
    assert registry.active() == []


async def test_t_est_monotone_et_jamais_negatif() -> None:
    ticks = iter([100.0, 100.5, 101.25, 99.0])
    session, _, _ = _session(clock=lambda: next(ticks))

    assert session.t() == 0.5
    assert session.t() == 1.25
    # Une horloge qui revient en arrière ne doit pas produire un `t` négatif :
    # le contrat d'événement l'interdit (ge=0) et la validation lèverait.
    assert session.t() == 0.0


class FakeClock:
    """L'horloge qu'un rejeu de scénario injecte : avancée à la main."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


async def test_le_registre_propage_son_horloge_a_la_session() -> None:
    """Sans ce passage de relais, le rejeu de scénario subit l'horloge murale et
    tous les événements dérivés s'empilent à 0 s."""
    clock = FakeClock()
    registry = SessionRegistry(
        transcriber_factory=FakeTranscriber,
        sink=InMemoryEventSink(),
        settings=_settings(),
        clock=clock,
    )

    session = await registry.open("CA1")

    assert session.t() == 0.0
    clock.now = 42.0
    assert session.t() == 42.0


async def test_l_horloge_de_open_prime_sur_celle_du_registre() -> None:
    registry = SessionRegistry(
        transcriber_factory=FakeTranscriber,
        sink=InMemoryEventSink(),
        settings=_settings(),
        clock=FakeClock(1000.0),
    )
    par_appel = FakeClock()

    session = await registry.open("CA1", clock=par_appel)

    par_appel.now = 7.5
    assert session.t() == 7.5


async def test_un_registre_sans_horloge_garde_le_temps_reel() -> None:
    """Non-régression : l'injection est un point d'extension, pas un changement
    de comportement par défaut."""
    registry = SessionRegistry(
        transcriber_factory=FakeTranscriber, sink=InMemoryEventSink(), settings=_settings()
    )

    session = await registry.open("CA1")

    # L'horloge murale avance toute seule : deux lectures ne rendent pas le même
    # instant figé, contrairement à une horloge injectée.
    premiere = session.t()
    seconde = session.t()
    assert 0.0 <= premiere < seconde


async def test_un_evenement_sans_t_explicite_suit_l_horloge_injectee() -> None:
    """Le vrai bug : `transcript` porte le `t` du segment, mais `keyword`, `llm`,
    `score` et `action` passent par `emit()` sans `t` — c'est donc à ce niveau
    que l'horloge du scénario doit se voir."""
    clock = FakeClock()
    sink = InMemoryEventSink()
    registry = SessionRegistry(
        transcriber_factory=FakeTranscriber, sink=sink, settings=_settings(), clock=clock
    )
    session = await registry.open("CA1")

    clock.now = 12.0
    await session.emit(EventType.SCORE, {"score": 80})

    (event,) = sink.by_type(EventType.SCORE)
    assert event.t == 12.0


async def test_une_horloge_qui_recule_n_emet_pas_de_t_negatif() -> None:
    clock = FakeClock(50.0)
    sink = InMemoryEventSink()
    registry = SessionRegistry(
        transcriber_factory=FakeTranscriber, sink=sink, settings=_settings(), clock=clock
    )
    session = await registry.open("CA1")

    clock.now = 10.0
    # `Event.t` est `ge=0` : sans le plancher, pydantic lèverait ici.
    await session.emit(EventType.KEYWORD, {"hits": []})

    (event,) = sink.by_type(EventType.KEYWORD)
    assert event.t == 0.0
