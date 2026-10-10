"""Garde-fou contre la dérive des ports — la panne qui a coûté la fusion d'hier.

Dev 1 et Dev 2 ont construit contre `domain/ports.py` chacun de son côté et les
signatures ont divergé : `EventSink` était `publish` d'un côté et `emit` de
l'autre, et `CallController` ne portait pas le `call_id` dont Twilio a besoin.
Les deux suites de tests passaient pendant tout ce temps, parce que chaque
moitié se testait contre ses propres doublures. Un `Protocol` n'est pas vérifié
à l'exécution : une dérive est donc silencieuse jusqu'à l'appel réel.

Ce fichier la rend bruyante, sur **deux niveaux** :

  1. `test_implementation_matches_its_port` — comparaison *générique* de chaque
     implémentation avec son port. Rien n'est codé en dur : les noms de méthodes
     viennent de `__protocol_attrs__` et les signatures du port lui-même. Ce
     niveau continue donc de fonctionner si on change légitimement un port — il
     n'échoue que sur un *écart entre le port et son implémentation*.

  2. `test_port_shape_is_still_the_agreed_one` — quelques ancres sur la forme
     des ports eux-mêmes. Celles-là sont volontairement codées en dur : elles
     consignent les décisions prises à la fusion. Si quelqu'un ramène
     silencieusement `publish`, ou retire le `call_id`, c'est ici que ça casse.

Contraintes : aucun réseau, aucun identifiant, aucun téléchargement de modèle,
et doit passer sans `torch` (installé seulement par `make install-voice`).
"""

from __future__ import annotations

import asyncio
import inspect
import typing
from collections.abc import Callable
from typing import Any, get_args

import pytest

from vocalguard.application.intervention import InterventionRunner
from vocalguard.application.scoring import ScoringPipeline
from vocalguard.application.sessions import SessionRegistry
from vocalguard.application.verification import IdentityVerifier
from vocalguard.config import Settings
from vocalguard.domain import ports
from vocalguard.infrastructure.aws.bedrock_analyzer import BedrockContentAnalyzer
from vocalguard.infrastructure.aws.transcribe import AwsTranscriber, AwsTranscriptionStream
from vocalguard.infrastructure.contacts.book import JsonContactBook
from vocalguard.infrastructure.fakes.call_controller import LoggingCallController
from vocalguard.infrastructure.fakes.doubles import (
    FakeContentAnalyzer,
    FakeReputationChecker,
    FakeVoiceDetector,
    StaticContactBook,
)
from vocalguard.infrastructure.fakes.event_sink import (
    ConsoleEventSink,
    FanOutSink,
    InMemoryEventSink,
)
from vocalguard.infrastructure.fakes.transcriber import (
    FakeTranscriber,
    FakeTranscriptionStream,
)
from vocalguard.infrastructure.keywords.detector import KeywordDetector
from vocalguard.infrastructure.telephony.client import TwilioCallController
from vocalguard.infrastructure.telephony.reputation import TwilioReputationChecker
from vocalguard.infrastructure.voice.synthetic_detector import Wav2VecVoiceDetector
from vocalguard.interface.event_hub import EventHub

# --- Construction des implémentations --------------------------------------
# Tout est instanciable sans identifiants, sans réseau et sans torch : on teste
# donc des *instances* et pas des classes. Un adapter qu'on ne saurait
# construire serait un adapter qu'on ne saurait pas vérifier.


def _settings() -> Settings:
    return Settings(_env_file=None, voice_detector_enabled=True)


def _registry(settings: Settings) -> SessionRegistry:
    return SessionRegistry(
        transcriber_factory=FakeTranscriber,
        sink=InMemoryEventSink(),
        settings=settings,
    )


def _pipeline(settings: Settings, registry: SessionRegistry) -> ScoringPipeline:
    return ScoringPipeline(
        registry=registry,
        keyword_engine=KeywordDetector(),
        voice_detector_factory=FakeVoiceDetector,
        settings=settings,
    )


def _voice_tap() -> Any:
    """`_VoiceTap` est privé et créé par la fabrique : on passe par elle."""
    settings = _settings()
    registry = _registry(settings)
    pipeline = _pipeline(settings, registry)

    async def build() -> Any:
        session = await registry.open("CA_PORTS", caller_number="+33700000000")
        return pipeline.audio_tap(session)

    tap = asyncio.run(build())
    assert tap is not None, (
        "audio_tap a renvoyé None alors que voice_detector_enabled=True et "
        "qu'une fabrique de détecteur est fournie : le branchement voix est mort"
    )
    return tap


def _implementations() -> list[tuple[str, type, Any]]:
    """(nom lisible, port, instance) pour chaque implémentation concrète."""
    settings = _settings()
    registry = _registry(settings)
    sink = InMemoryEventSink()
    fake_transcriber = FakeTranscriber()
    contacts = JsonContactBook()

    return [
        # EventSink — le port qui a dérivé (`publish` vs `emit`).
        ("EventHub", ports.EventSink, EventHub()),
        ("InMemoryEventSink", ports.EventSink, sink),
        ("ConsoleEventSink", ports.EventSink, ConsoleEventSink()),
        ("FanOutSink", ports.EventSink, FanOutSink()),
        ("ScoringPipeline", ports.EventSink, _pipeline(settings, registry)),
        # L'InterventionRunner a gagné un alias `emit` pour s'abonner au bus.
        (
            "InterventionRunner",
            ports.EventSink,
            InterventionRunner(
                controller=LoggingCallController(),
                sink=sink,
                settings=settings,
                registry=registry,
            ),
        ),
        # Le vérificateur d'identité s'abonne au bus pour deux raisons : entendre
        # le code sur la piste appelant, et la demande de vérification sur la
        # piste victime. Il est donc bien un `EventSink`, et sa dérive se paierait
        # au pire moment — un `emit` renommé le rendrait sourd en silence, le bus
        # avalant l'erreur pour ne pas priver les autres destinations.
        (
            "IdentityVerifier",
            ports.EventSink,
            IdentityVerifier(
                registry=registry,
                controller=LoggingCallController(),
                contacts=contacts,
                settings=settings,
            ),
        ),
        # CallController — le port dont la version Dev 2 oubliait le call_id.
        ("LoggingCallController", ports.CallController, LoggingCallController()),
        (
            "TwilioCallController",
            ports.CallController,
            TwilioCallController(settings, client=object()),
        ),
        # Transcription (Dev 1).
        ("FakeTranscriber", ports.Transcriber, fake_transcriber),
        ("AwsTranscriber", ports.Transcriber, AwsTranscriber(settings, client=object())),
        (
            "FakeTranscriptionStream",
            ports.TranscriptionStream,
            FakeTranscriptionStream(fake_transcriber, "caller"),
        ),
        (
            "AwsTranscriptionStream",
            ports.TranscriptionStream,
            AwsTranscriptionStream(speaker="caller"),
        ),
        # Audio dérivé vers la détection de voix.
        ("ScoringPipeline._VoiceTap", ports.AudioConsumer, _voice_tap()),
        # Signaux (Dev 2).
        ("KeywordDetector", ports.KeywordEngine, KeywordDetector()),
        ("JsonContactBook", ports.ContactBook, contacts),
        ("StaticContactBook", ports.ContactBook, StaticContactBook()),
        ("Wav2VecVoiceDetector", ports.VoiceDetector, Wav2VecVoiceDetector(enabled=False)),
        ("FakeVoiceDetector", ports.VoiceDetector, FakeVoiceDetector()),
        (
            "BedrockContentAnalyzer",
            ports.ContentAnalyzer,
            BedrockContentAnalyzer(client=object()),
        ),
        ("FakeContentAnalyzer", ports.ContentAnalyzer, FakeContentAnalyzer()),
        (
            "TwilioReputationChecker",
            ports.ReputationChecker,
            TwilioReputationChecker(contacts=contacts, settings=settings),
        ),
        ("FakeReputationChecker", ports.ReputationChecker, FakeReputationChecker()),
    ]


IMPLEMENTATIONS = _implementations()
IDS = [f"{name}->{port.__name__}" for name, port, _ in IMPLEMENTATIONS]


# --- Comparaison de signatures ---------------------------------------------


def _protocol_methods(port: type) -> frozenset[str]:
    """Les membres du protocole, sans les coder en dur."""
    attrs = getattr(port, "__protocol_attrs__", None)
    assert attrs, f"{port.__name__} n'expose pas __protocol_attrs__"
    return frozenset(attrs)


def _comparable(signature: inspect.Signature, *, drop_self: bool) -> list[tuple[Any, ...]]:
    """Réduit une signature à ce qui engage le contrat.

    On ignore les annotations : sous `from __future__ import annotations` elles
    sont des chaînes, donc `ContentSignal | None` et `Optional[ContentSignal]`
    différeraient à tort. Le type de retour est vérifié séparément, résolu.
    """
    parameters = list(signature.parameters.values())
    if drop_self and parameters and parameters[0].name == "self":
        parameters = parameters[1:]
    return [(p.name, p.kind, p.default is inspect.Parameter.empty) for p in parameters]


_POSITIONAL = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
_BY_KEYWORD = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)


def _substitutability_errors(
    port_signature: inspect.Signature, impl_signature: inspect.Signature
) -> list[str]:
    """Ce que l'implémentation refuse alors que le port le promet.

    On ne demande pas l'égalité des signatures : une implémentation *plus
    permissive* que son port reste substituable (accepter un argument en
    positionnel alors que le port l'exige en keyword ne casse aucun appelant).
    On ne signale donc que le sens dangereux — l'implémentation plus
    restrictive, ou un paramètre renommé, qui casse un appel légitime.
    """
    port_params = [p for p in port_signature.parameters.values() if p.name != "self"]
    impl_params = list(impl_signature.parameters.values())
    impl_by_name = {p.name: p for p in impl_params}
    kinds = {p.kind for p in impl_params}
    has_var_keyword = inspect.Parameter.VAR_KEYWORD in kinds
    has_var_positional = inspect.Parameter.VAR_POSITIONAL in kinds

    errors: list[str] = []
    for index, expected in enumerate(port_params):
        actual = impl_by_name.get(expected.name)
        if actual is None:
            if has_var_keyword:
                continue  # **kwargs absorbe le paramètre : rien à prouver.
            errors.append(
                f"paramètre « {expected.name} » absent — un appel "
                f"{expected.name}=… lèverait TypeError"
            )
            continue

        if expected.kind in _POSITIONAL and actual.kind not in _POSITIONAL:
            if not has_var_positional:
                errors.append(
                    f"« {expected.name} » n'est plus acceptable en positionnel "
                    f"({actual.kind.description})"
                )
        if expected.kind in _BY_KEYWORD and actual.kind not in _BY_KEYWORD:
            if not has_var_keyword:
                errors.append(
                    f"« {expected.name} » n'est plus acceptable en keyword "
                    f"({actual.kind.description})"
                )
        if (
            expected.default is not inspect.Parameter.empty
            and actual.default is inspect.Parameter.empty
        ):
            errors.append(
                f"« {expected.name} » est devenu obligatoire alors que le port lui donne un défaut"
            )
        if expected.kind in _POSITIONAL and actual.kind in _POSITIONAL:
            positional_impl = [p.name for p in impl_params if p.kind in _POSITIONAL]
            if positional_impl[: index + 1][-1:] != [expected.name]:
                errors.append(
                    f"« {expected.name} » n'est plus en position {index} "
                    f"(ordre réel : {positional_impl})"
                )

    extra_required = [
        p.name
        for p in impl_params
        if p.name not in {q.name for q in port_params}
        and p.default is inspect.Parameter.empty
        and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    ]
    if extra_required:
        errors.append(
            f"paramètre(s) obligatoire(s) en trop : {extra_required} — le port ne les fournit pas"
        )
    return errors


def _describe(signature: inspect.Signature, *, drop_self: bool) -> str:
    parts = []
    for name, kind, required in _comparable(signature, drop_self=drop_self):
        suffix = "" if required else "=..."
        if kind is inspect.Parameter.KEYWORD_ONLY:
            parts.append(f"*, {name}{suffix}")
        elif kind is inspect.Parameter.VAR_POSITIONAL:
            parts.append(f"*{name}")
        elif kind is inspect.Parameter.VAR_KEYWORD:
            parts.append(f"**{name}")
        else:
            parts.append(f"{name}{suffix}")
    return "(" + ", ".join(parts) + ")"


@pytest.mark.parametrize(("name", "port", "implementation"), IMPLEMENTATIONS, ids=IDS)
def test_implementation_matches_its_port(name: str, port: type, implementation: Any) -> None:
    """Chaque méthode du port existe sur l'implémentation, avec la même forme.

    `isinstance` contre un `Protocol` runtime_checkable ne compare que les *noms*
    de méthodes : un paramètre renommé ou une arité changée passe au travers et
    n'explose qu'à l'appel réel. C'est exactement ce qui nous est arrivé, donc on
    compare les signatures.
    """
    for method_name in sorted(_protocol_methods(port)):
        attribute = getattr(implementation, method_name, None)
        assert attribute is not None, (
            f"{name} ne satisfait pas {port.__name__} : méthode « {method_name} » "
            f"absente. C'est la dérive de port qui ne se voit qu'à l'appel réel."
        )
        assert callable(attribute), f"{name}.{method_name} n'est pas appelable"

        expected = inspect.signature(getattr(port, method_name))
        actual = inspect.signature(attribute)
        errors = _substitutability_errors(expected, actual)

        assert not errors, (
            f"{name}.{method_name} a dérivé de {port.__name__}.{method_name} :\n"
            + "".join(f"  - {problem}\n" for problem in errors)
            + f"  port             : {method_name}{_describe(expected, drop_self=True)}\n"
            f"  implémentation   : {method_name}{_describe(actual, drop_self=False)}\n"
            f"Corrige l'un des deux, puis relance TOUTE la suite : les deux "
            f"moitiés du projet dépendent de ce port."
        )


@pytest.mark.parametrize(("name", "port", "implementation"), IMPLEMENTATIONS, ids=IDS)
def test_runtime_checkable_ports_accept_their_implementations(
    name: str, port: type, implementation: Any
) -> None:
    """Le contrôle faible, mais c'est celui que le code de câblage utilise."""
    if not getattr(port, "_is_runtime_protocol", False):
        pytest.skip(f"{port.__name__} n'est pas runtime_checkable")
    assert isinstance(implementation, port), (
        f"isinstance({name}, {port.__name__}) est faux : le câblage de "
        f"`create_app` refusera cette implémentation."
    )


def test_async_ports_are_implemented_with_coroutines() -> None:
    """Une méthode async du port implémentée en synchrone bloque la boucle média.

    Le tap voix tourne dans la boucle de Dev 1, à 50 trames/s par piste : une
    inférence synchrone y décrocherait la transcription.
    """
    for name, port, implementation in IMPLEMENTATIONS:
        for method_name in sorted(_protocol_methods(port)):
            port_method = getattr(port, method_name)
            if not inspect.iscoroutinefunction(port_method):
                continue
            actual = getattr(implementation, method_name)
            assert inspect.iscoroutinefunction(actual), (
                f"{name}.{method_name} est synchrone alors que "
                f"{port.__name__}.{method_name} est une coroutine : "
                f"appelé dans la boucle média, il la bloquerait."
            )


# --- Ancres : la forme des ports elle-même ---------------------------------
# Volontairement codées en dur, contrairement aux tests ci-dessus. Elles
# consignent les arbitrages de la fusion : si quelqu'un les change, il doit le
# faire sciemment et prévenir l'autre dev.


def _hints(func: Callable[..., Any]) -> dict[str, Any]:
    return typing.get_type_hints(func)


def test_port_shape_is_still_the_agreed_one() -> None:
    # EventSink : `emit`, et surtout pas `publish` — le renommage qui a tout cassé.
    assert _protocol_methods(ports.EventSink) == {"emit"}
    assert _comparable(inspect.signature(ports.EventSink.emit), drop_self=True) == [
        ("event", inspect.Parameter.POSITIONAL_OR_KEYWORD, True)
    ]

    # CallController : les trois actions sur l'appel portent le call_id. Sans lui,
    # impossible de viser le bon participant Twilio.
    for method_name in ("hold_caller", "resume_caller", "announce_to_victim"):
        names = [
            p
            for p, _, _ in _comparable(
                inspect.signature(getattr(ports.CallController, method_name)), drop_self=True
            )
        ]
        assert names[0] == "call_id", (
            f"CallController.{method_name} doit prendre call_id en premier ; "
            f"signature actuelle : {names}"
        )
    assert "hangup" not in _protocol_methods(ports.CallController), (
        "Pas de hangup : un faux positif sur un vrai appel d'urgence serait grave, "
        "la mise en attente protège autant et laisse la décision à l'humain."
    )

    # ReputationChecker : claimed_identity en keyword-only.
    check = _comparable(inspect.signature(ports.ReputationChecker.check), drop_self=True)
    assert check[0] == ("phone_number", inspect.Parameter.POSITIONAL_OR_KEYWORD, True)
    assert ("claimed_identity", inspect.Parameter.KEYWORD_ONLY, False) in check

    # AudioConsumer : tout en keyword-only, c'est ainsi que la boucle média appelle.
    push = _comparable(inspect.signature(ports.AudioConsumer.push), drop_self=True)
    assert {p for p, _, _ in push} == {"speaker", "pcm16", "t"}
    assert all(kind is inspect.Parameter.KEYWORD_ONLY for _, kind, _ in push)

    # KeywordEngine garde `scan_call` : le coupe-circuit juge l'accumulation sur
    # l'appel, pas une phrase isolée.
    assert _protocol_methods(ports.KeywordEngine) == {"scan", "scan_call"}

    # VoiceDetector : le speaker et la fréquence voyagent avec les octets.
    feed_signature = inspect.signature(ports.VoiceDetector.feed)
    feed = [p for p, _, _ in _comparable(feed_signature, drop_self=True)]
    assert feed == ["speaker", "pcm16", "sample_rate"]


def test_optional_returning_ports_still_allow_none() -> None:
    """`None` est porteur de sens sur trois ports, pas une négligence.

    - `ContentAnalyzer.analyze` : « rien de nouveau, garde le signal précédent ».
      C'est la grille de cadence de `LLM_MIN_INTERVAL_SECONDS` ; une version qui
      renvoie toujours un signal la désactiverait en silence et appellerait
      Bedrock à chaque segment.
    - `VoiceDetector.feed` : fenêtre incomplète, ou torch absent.
    - `ContactBook.by_number` / `by_identity` : numéro inconnu.
    """
    optional_returns = [
        (ports.ContentAnalyzer.analyze, "ContentAnalyzer.analyze"),
        (ports.VoiceDetector.feed, "VoiceDetector.feed"),
        (ports.ContactBook.by_number, "ContactBook.by_number"),
        (ports.ContactBook.by_identity, "ContactBook.by_identity"),
    ]
    for method, label in optional_returns:
        returned = _hints(method).get("return")
        assert type(None) in get_args(returned), (
            f"{label} doit pouvoir renvoyer None — le type de retour est "
            f"{returned!r}. Retirer cette possibilité casse un comportement dont "
            f"dépend l'autre moitié du projet."
        )


def test_every_port_is_guarded() -> None:
    """Un port ajouté sans implémentation vérifiée ici ne garde rien.

    Les ports purement structurels (callbacks) sont exclus nommément : ils n'ont
    pas d'implémentation en classe.
    """
    declared = {
        name
        for name, obj in vars(ports).items()
        if isinstance(obj, type) and issubclass(obj, typing.Protocol) and obj is not typing.Protocol
    }
    structural_only = {"SegmentCallback"}
    guarded = {port.__name__ for _, port, _ in IMPLEMENTATIONS}
    missing = declared - structural_only - guarded - {"Protocol"}
    assert not missing, (
        f"Ports sans implémentation gardée : {sorted(missing)}. "
        f"Ajoute-les à _implementations(), sinon leur dérive redevient silencieuse."
    )
