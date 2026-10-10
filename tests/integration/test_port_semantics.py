"""Contrats *sémantiques* des ports : ce que les signatures ne disent pas.

`test_port_conformance.py` compare les signatures. C'est utile, mais étroit :
trois de nos ports portent une promesse qui ne s'exprime pas dans une signature.
Une implémentation peut la violer avec la bonne arité, les bons noms de
paramètres, le bon type de retour — suite verte, comportement faux.

Les trois règles vérifiées ici :

1. `ContentAnalyzer.analyze` renvoie `None` pour dire « rien de nouveau, garde
   le signal précédent ». Renvoyer `ContentSignal(risk=0.0)` à la place ferait
   entrer un vrai zéro dans la somme pondérée et annulerait les 45 points du
   LLM, silencieusement.
2. `VoiceDetector.feed` renvoie `None` tant qu'une fenêtre n'est pas complète.
   Même piège : un `0.0` est une mesure, `None` est une abstention.
3. `CallController` renvoie `False` en cas d'échec et ne lève jamais.
   `application/intervention.py` exécute les actions en séquence pour qu'une
   mise en attente ratée laisse quand même passer l'annonce.

Ces tests complètent `test_end_to_end.py`, ils ne le remplacent pas : celui-ci
exerce la vraie frontière, celui-là la forme, celui-ci le sens.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Sequence

import pytest
from twilio.base.exceptions import TwilioRestException

from vocalguard.application.intervention import InterventionRunner
from vocalguard.application.scoring import ScoringPipeline
from vocalguard.application.sessions import SessionRegistry
from vocalguard.config import Settings
from vocalguard.domain.alerts import AlertLevel
from vocalguard.domain.events import Event, EventType
from vocalguard.domain.models import (
    ContentSignal,
    KeywordSignal,
    RiskAssessment,
    TranscriptSegment,
)
from vocalguard.infrastructure.aws.bedrock_analyzer import BedrockContentAnalyzer
from vocalguard.infrastructure.fakes.call_controller import LoggingCallController
from vocalguard.infrastructure.fakes.doubles import FakeContentAnalyzer, FakeVoiceDetector
from vocalguard.infrastructure.fakes.event_sink import InMemoryEventSink
from vocalguard.infrastructure.fakes.transcriber import FakeTranscriber
from vocalguard.infrastructure.telephony.client import TwilioCallController
from vocalguard.infrastructure.voice.synthetic_detector import Wav2VecVoiceDetector

CONTROLLER_METHODS = ("hold_caller", "resume_caller", "announce_to_victim", "send_sms")


def seg(text: str = "il faut faire un virement", *, final: bool = True) -> TranscriptSegment:
    return TranscriptSegment(speaker="caller", text=text, t=1.0, is_final=final)


def settings_for(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


# --- Faux client Bedrock ----------------------------------------------------


class _Body:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def read(self) -> bytes:
        return self._raw


class FakeBedrockClient:
    def __init__(self, raw: bytes | None = None, exc: Exception | None = None) -> None:
        self._raw = raw
        self._exc = exc
        self.calls = 0

    def invoke_model(self, **kwargs):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return {"body": _Body(self._raw or b"")}


def bedrock_payload(risk: float = 0.8) -> bytes:
    inner = json.dumps({"risk": risk, "tactics": ["urgence"], "explanation": "x"})
    return json.dumps({"content": [{"text": inner}]}).encode()


# ===========================================================================
# Règle 1 — ContentAnalyzer.analyze : None = abstention, pas un risque nul
# ===========================================================================

ABSTAIN_RULE = (
    "`None` veut dire « garde le signal précédent ». Renvoyer "
    "ContentSignal(risk=0.0) à la place fait entrer un vrai zéro dans la somme "
    "pondérée et annule les 45 points du LLM sans que rien ne le signale."
)


def content_analyzers() -> list[tuple[str, object]]:
    """Toutes les implémentations du port, des deux moitiés du projet."""
    return [
        ("FakeContentAnalyzer", FakeContentAnalyzer()),
        (
            "BedrockContentAnalyzer",
            BedrockContentAnalyzer(
                client=FakeBedrockClient(bedrock_payload()), min_interval_seconds=0.0
            ),
        ),
    ]


@pytest.mark.parametrize("name,analyzer", content_analyzers())
async def test_content_analyzer_abstains_on_empty_window(name: str, analyzer) -> None:
    assert await analyzer.analyze([]) is None, f"{name} : {ABSTAIN_RULE}"


@pytest.mark.parametrize("name,analyzer", content_analyzers())
async def test_content_analyzer_abstains_when_only_partials(name: str, analyzer) -> None:
    """Les partiels de Transcribe changent à chaque mot : rien à analyser."""
    assert await analyzer.analyze([seg(final=False)]) is None, f"{name} : {ABSTAIN_RULE}"


async def test_bedrock_abstains_when_called_too_soon() -> None:
    """La cadence (`LLM_MIN_INTERVAL_SECONDS`) s'exprime par une abstention.

    Si le deuxième appel renvoyait un signal nul au lieu de `None`, chaque
    segment entre deux fenêtres écraserait le verdict de Claude par un zéro.
    """
    client = FakeBedrockClient(bedrock_payload(0.9))
    analyzer = BedrockContentAnalyzer(client=client, min_interval_seconds=999.0)

    first = await analyzer.analyze([seg()])
    assert first is not None and first.risk == pytest.approx(0.9)

    second = await analyzer.analyze([seg()])
    assert second is None, f"BedrockContentAnalyzer (cadence) : {ABSTAIN_RULE}"
    assert client.calls == 1, "la cadence doit aussi éviter l'appel réseau, pas que le signal"


@pytest.mark.parametrize(
    "label,client",
    [
        ("corps illisible", FakeBedrockClient(b"ceci n'est pas du json")),
        ("JSON sans risk", FakeBedrockClient(json.dumps({"content": [{"text": "{}"}]}).encode())),
        ("client qui lève", FakeBedrockClient(exc=RuntimeError("bedrock injoignable"))),
        ("throttling", FakeBedrockClient(exc=Exception("ThrottlingException"))),
    ],
)
async def test_bedrock_degrades_to_abstention_on_failure(label: str, client) -> None:
    """Toute panne dégrade vers `None`, jamais vers un risque inventé.

    C'est ce qui permet au score de continuer à vivre sur les mots-clés
    (coupe-circuit `keywords_alone_watch`) au lieu de s'effondrer.
    """
    analyzer = BedrockContentAnalyzer(client=client, min_interval_seconds=0.0)
    assert await analyzer.analyze([seg()]) is None, f"Bedrock / {label} : {ABSTAIN_RULE}"


# ===========================================================================
# Règle 2 — VoiceDetector.feed : None = fenêtre incomplète
# ===========================================================================

VOICE_RULE = (
    "`None` veut dire « fenêtre incomplète », pas « risque nul ». Un 0.0 est "
    "une mesure qui entre dans la somme pondérée ; `None` est une abstention."
)


def voice_detectors() -> list[tuple[str, object]]:
    return [
        ("FakeVoiceDetector", FakeVoiceDetector(windows_before_signal=3)),
        (
            "Wav2VecVoiceDetector",
            Wav2VecVoiceDetector(enabled=True, window_seconds=0.1, target_sample_rate=16000),
        ),
    ]


@pytest.mark.parametrize("name,detector", voice_detectors())
async def test_voice_detector_abstains_on_victim_track(name: str, detector) -> None:
    """La victime n'est pas suspectée : sa piste ne produit aucun signal."""
    result = await detector.feed("victim", b"\x00\x00" * 5000, 8000)
    assert result is None, f"{name} : la piste victime ne doit rien produire. {VOICE_RULE}"


@pytest.mark.parametrize("name,detector", voice_detectors())
async def test_voice_detector_abstains_before_a_full_window(name: str, detector) -> None:
    result = await detector.feed("caller", b"\x00\x00" * 8, 8000)
    assert result is None, f"{name} : {VOICE_RULE}"


async def test_wav2vec_abstains_forever_without_torch() -> None:
    """torch n'est installé que par `make install-voice`.

    Sans lui le signal doit s'éteindre définitivement et rester `None` — jamais
    un risque fabriqué, qui serait indistinguable d'une vraie mesure.
    """
    assert "torch" not in sys.modules, "ce test n'a de sens que si torch est absent"

    detector = Wav2VecVoiceDetector(enabled=True, window_seconds=0.1, target_sample_rate=16000)
    full_window = b"\x00\x00" * 5000
    results = [await detector.feed("caller", full_window, 8000) for _ in range(4)]

    assert all(r is None for r in results), (
        f"Wav2VecVoiceDetector sans torch doit s'abstenir, a renvoyé {results}. {VOICE_RULE}"
    )
    assert "torch" not in sys.modules, "le détecteur ne doit pas importer torch quand il est mort"


# ===========================================================================
# Règle 3 — CallController : renvoie False, ne lève jamais
# ===========================================================================

CONTROLLER_RULE = (
    "`CallController` doit renvoyer False en cas d'échec et ne jamais lever. "
    "intervention.py exécute les actions en séquence : une mise en attente qui "
    "lève ne doit pas empêcher l'annonce d'arriver à la victime."
)


class RaisingTwilioClient:
    """Client dont tout accès d'attribut échoue, comme une panne réseau."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def __getattr__(self, name: str):
        raise self._exc


def twilio_settings() -> Settings:
    return settings_for(
        public_hostname="exemple.ngrok.app",
        twilio_account_sid="ACfake",
        twilio_auth_token="token",
        twilio_phone_number="+15550000001",
    )


async def call_method(controller, method: str):
    if method == "announce_to_victim":
        return await controller.announce_to_victim("CA1", "message")
    if method == "send_sms":
        return await controller.send_sms("+33600000000", "message")
    return await getattr(controller, method)("CA1")


@pytest.mark.parametrize("method", CONTROLLER_METHODS)
async def test_logging_controller_never_raises(method: str) -> None:
    controller = LoggingCallController()
    result = await call_method(controller, method)
    assert isinstance(result, bool), f"LoggingCallController.{method} : {CONTROLLER_RULE}"


@pytest.mark.parametrize("method", CONTROLLER_METHODS)
async def test_twilio_controller_returns_false_without_credentials(method: str) -> None:
    """Démarrage sans compte Twilio : les actions sont ignorées, pas fatales."""
    controller = TwilioCallController(settings_for())
    assert await call_method(controller, method) is False, (
        f"TwilioCallController.{method} sans credentials : {CONTROLLER_RULE}"
    )


@pytest.mark.parametrize("method", CONTROLLER_METHODS)
async def test_twilio_controller_returns_false_on_api_error(method: str) -> None:
    """Erreur de l'API Twilio : traitée, renvoie False."""
    client = RaisingTwilioClient(TwilioRestException(400, "/Participants", "refusé", 20001))
    controller = TwilioCallController(twilio_settings(), client=client)
    assert await call_method(controller, method) is False, (
        f"TwilioCallController.{method} sur TwilioRestException : {CONTROLLER_RULE}"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Violation connue, fichier de Dev 1. `infrastructure/telephony/client.py` "
        "ne rattrape que `TwilioRestException`, alors que sa propre docstring "
        "promet « renvoie False et ne lève pas ». Correctif : élargir à "
        "`except Exception`. Portée mesurée : `intervention.py` enveloppe chaque "
        "action séparément, donc les trois actions sont quand même tentées et "
        "rien ne remonte au bus — fragilité latente, pas un bug vivant. "
        "strict=True : ce test DOIT échouer en XPASS une fois corrigé, pour "
        "forcer la suppression de ce marqueur dans le même commit."
    ),
)
@pytest.mark.parametrize("method", CONTROLLER_METHODS)
@pytest.mark.parametrize(
    "exc", [ConnectionError("timeout réseau"), RuntimeError("panne inattendue")]
)
async def test_twilio_controller_returns_false_on_network_error(
    method: str, exc: Exception
) -> None:
    """Panne NON-Twilio : le SDK ne lève pas que des `TwilioRestException`.

    Un timeout réseau au niveau HTTP (wifi du lieu…) remonte en
    `ConnectionError`, qui n'est pas rattrapée par les `except
    TwilioRestException` de `client.py`.
    """
    controller = TwilioCallController(twilio_settings(), client=RaisingTwilioClient(exc))
    result = await call_method(controller, method)
    assert result is False, (
        f"TwilioCallController.{method} sur {type(exc).__name__} : {CONTROLLER_RULE}"
    )


async def test_intervention_contains_a_raising_controller() -> None:
    """Où s'arrête le rayon d'action de la règle 3.

    `intervention.py` enveloppe chaque action : même si un contrôleur lève, les
    actions suivantes sont tentées et un événement `action` part avec ok=False.
    C'est ce qui empêche la violation ci-dessus de coûter la démo.
    """

    class Raising:
        def __init__(self) -> None:
            self.attempts: list[str] = []

        async def hold_caller(self, call_id: str) -> bool:
            self.attempts.append("hold_caller")
            raise ConnectionError("timeout")

        async def resume_caller(self, call_id: str) -> bool:
            self.attempts.append("resume_caller")
            raise ConnectionError("timeout")

        async def announce_to_victim(self, call_id: str, message: str) -> bool:
            self.attempts.append("announce_to_victim")
            raise ConnectionError("timeout")

        async def send_sms(self, to_number: str, message: str) -> bool:
            self.attempts.append("send_sms")
            raise ConnectionError("timeout")

    settings = settings_for(trusted_contact_phone_number="+33600000000")
    sink = InMemoryEventSink()
    registry = SessionRegistry(transcriber_factory=FakeTranscriber, sink=sink, settings=settings)
    session = await registry.open("CA1", caller_number="+33755000000")
    controller = Raising()
    runner = InterventionRunner(
        controller=controller, sink=sink, settings=settings, registry=registry
    )

    logging.getLogger("vocalguard.application.intervention").setLevel(logging.CRITICAL)
    await runner.handle_score(session, RiskAssessment(score=90, level=AlertLevel.INTERVENE))

    assert "announce_to_victim" in controller.attempts, (
        "une mise en attente qui lève ne doit pas empêcher l'annonce : "
        "c'est exactement la séquence que la règle 3 protège"
    )
    assert all(e.data["ok"] is False for e in sink.by_type(EventType.ACTION))


# ===========================================================================
# Conséquence — ce que `None` promet réellement au pipeline
# ===========================================================================


class _Bus:
    """Sink dont la composition se fixe après coup, pour casser le cycle."""

    def __init__(self) -> None:
        self.sinks: tuple = ()

    async def emit(self, event: Event) -> None:
        for sink in self.sinks:
            await sink.emit(event)


class _Keywords:
    def scan(self, text: str) -> KeywordSignal:
        return KeywordSignal(risk=0.0)

    def scan_call(self, turns: Sequence[TranscriptSegment]) -> KeywordSignal:
        return self.scan("")


class ThenAbstains:
    """Un vrai signal au premier appel, `None` ensuite."""

    def __init__(self, signal: ContentSignal) -> None:
        self._signal = signal
        self.calls = 0

    async def analyze(self, window: Sequence[TranscriptSegment]) -> ContentSignal | None:
        self.calls += 1
        return self._signal if self.calls == 1 else None


async def test_abstention_preserves_the_previous_content_signal() -> None:
    """La promesse de `None` : « garde le signal précédent ».

    Sans cette garantie, un Bedrock lent ou throttlé effacerait le verdict de
    Claude à chaque segment et le score retomberait sous le seuil au pire
    moment — pendant que le scammeur demande le virement.
    """
    settings = settings_for()
    recorder = InMemoryEventSink()
    bus = _Bus()
    registry = SessionRegistry(transcriber_factory=FakeTranscriber, sink=bus, settings=settings)
    analyzer = ThenAbstains(
        ContentSignal(risk=0.9, tactics=("urgence",), explanation="Virement réclamé.")
    )
    pipeline = ScoringPipeline(
        registry=registry,
        keyword_engine=_Keywords(),
        content_analyzer=analyzer,
        settings=settings,
    )
    bus.sinks = (recorder, pipeline)

    session = await registry.open("CA1", caller_number="+33755000000")

    await session.on_segment(seg("mamie il faut un virement"))
    await pipeline.aclose()
    first_signal = session.signals.content
    score_after_signal = recorder.by_type(EventType.SCORE)[-1].data["score"]
    assert first_signal is not None and first_signal.risk == pytest.approx(0.9)

    await session.on_segment(seg("tout de suite s'il te plaît"))
    await pipeline.aclose()

    assert analyzer.calls >= 2, "le second tour doit bien avoir interrogé l'analyzer"
    assert session.signals.content is first_signal, (
        "une abstention a effacé le signal de contenu. `None` veut dire « garde "
        "le précédent » : l'effacer ferait perdre 45 points au milieu de l'appel."
    )
    assert recorder.by_type(EventType.SCORE)[-1].data["score"] >= score_after_signal, (
        "le score a baissé après une abstention, alors qu'aucune preuve nouvelle n'est arrivée"
    )
