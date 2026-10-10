"""Du score aux actions Twilio : ordre, idempotence, et robustesse aux pannes."""

from __future__ import annotations

from typing import Any

from vocalguard.application.intervention import InterventionRunner
from vocalguard.application.sessions import CallSession, SessionRegistry
from vocalguard.config import Settings
from vocalguard.domain.alerts import ANNOUNCE_ALERT, SMS_VERIFY_CONTACT
from vocalguard.domain.events import Event, EventType
from vocalguard.domain.models import ContentSignal
from vocalguard.infrastructure.fakes.call_controller import LoggingCallController
from vocalguard.infrastructure.fakes.event_sink import InMemoryEventSink
from vocalguard.infrastructure.fakes.transcriber import FakeTranscriber

CONTACT = "+33600000000"


def _settings(**overrides: Any) -> Settings:
    overrides.setdefault("trusted_contact_phone_number", CONTACT)
    return Settings(_env_file=None, **overrides)


async def _setup(
    *, controller: LoggingCallController | None = None, settings: Settings | None = None
) -> tuple[InterventionRunner, CallSession, LoggingCallController, InMemoryEventSink]:
    settings = settings or _settings()
    sink = InMemoryEventSink()
    registry = SessionRegistry(transcriber_factory=FakeTranscriber, sink=sink, settings=settings)
    session = await registry.open("CA1")
    controller = controller or LoggingCallController()
    runner = InterventionRunner(
        controller=controller, sink=sink, settings=settings, registry=registry
    )
    return runner, session, controller, sink


def _score_event(score: int, level: str | None = "alert", **extra: Any) -> Event:
    data: dict[str, Any] = {"score": score, "reasons": ["urgence"], "breakers_fired": []}
    if level is not None:
        data["level"] = level
    data.update(extra)
    return Event(call_id="CA1", t=12.0, type=EventType.SCORE, data=data)


async def test_alerte_annonce_et_sms_une_seule_fois() -> None:
    runner, _, controller, sink = await _setup()

    # Le score oscille : trois événements d'alerte d'affilée.
    for score in (70, 66, 72):
        await runner.handle_event(_score_event(score))

    assert controller.calls == [
        ("announce_to_victim", "CA1", ANNOUNCE_ALERT),
        ("send_sms", CONTACT, SMS_VERIFY_CONTACT),
    ]
    actions = sink.by_type(EventType.ACTION)
    assert [event.data["action"] for event in actions] == ["announce_victim", "sms_contact"]
    assert all(event.data["ok"] for event in actions)
    assert all(event.data["target"] for event in actions)


async def test_intervention_met_en_attente_avant_d_annoncer() -> None:
    runner, session, controller, _ = await _setup()

    await runner.handle_event(_score_event(90, "intervene"))

    # L'ordre est le point critique : si l'annonce passait d'abord, le scammeur
    # pourrait l'entendre.
    assert [call[0] for call in controller.calls] == [
        "hold_caller",
        "announce_to_victim",
        "send_sms",
    ]
    assert session.caller_held is True


async def test_la_vigilance_ne_declenche_rien_d_audible() -> None:
    runner, _, controller, sink = await _setup()

    await runner.handle_event(_score_event(50, "watch"))

    assert controller.calls == []
    assert sink.by_type(EventType.ACTION) == []


async def test_une_aggravation_declenche_le_niveau_suivant() -> None:
    runner, session, controller, _ = await _setup()

    await runner.handle_event(_score_event(70, "alert"))
    await runner.handle_event(_score_event(90, "intervene"))

    assert [call[0] for call in controller.calls] == [
        "announce_to_victim",
        "send_sms",
        "hold_caller",
        "announce_to_victim",
        "send_sms",
    ]
    assert session.caller_held is True


async def test_le_nom_du_proche_vient_de_l_identite_revendiquee() -> None:
    runner, session, controller, _ = await _setup()
    session.signals.content = ContentSignal(risk=0.95, claimed_identity="Lucas, petit-fils")

    await runner.handle_event(_score_event(90, "intervene"))

    message = next(call[2] for call in controller.calls if call[0] == "announce_to_victim")
    assert "Lucas" in message
    assert "petit-fils" not in message


async def test_une_action_en_echec_n_empeche_pas_les_suivantes() -> None:
    class HoldFails(LoggingCallController):
        async def hold_caller(self, call_id: str) -> bool:
            raise RuntimeError("erreur Twilio 16000")

    runner, session, controller, sink = await _setup(controller=HoldFails())

    await runner.handle_event(_score_event(90, "intervene"))

    assert [call[0] for call in controller.calls] == ["announce_to_victim", "send_sms"]
    assert session.caller_held is False
    actions = {event.data["action"]: event.data["ok"] for event in sink.by_type(EventType.ACTION)}
    assert actions == {"hold_caller": False, "announce_victim": True, "sms_contact": True}


async def test_sans_numero_de_proche_le_sms_echoue_sans_bloquer() -> None:
    runner, _, controller, sink = await _setup(settings=_settings(trusted_contact_phone_number=""))

    await runner.handle_event(_score_event(70, "alert"))

    assert [call[0] for call in controller.calls] == ["announce_to_victim"]
    actions = {event.data["action"]: event.data["ok"] for event in sink.by_type(EventType.ACTION)}
    assert actions == {"announce_victim": True, "sms_contact": False}


async def test_un_evenement_score_malforme_est_ignore_sans_lever() -> None:
    runner, _, controller, sink = await _setup()

    await runner.handle_event(Event(call_id="CA1", t=1.0, type=EventType.SCORE, data={}))
    await runner.handle_event(_score_event(70, level="n_importe_quoi", reasons="urgence"))
    await runner.handle_event(
        Event(call_id="CA1", t=1.0, type=EventType.SCORE, data={"score": "beaucoup"})
    )

    # Le deuxième événement a un score valide : le niveau est recalculé depuis les
    # seuils plutôt que jeté, pour ne pas perdre une alerte à cause d'un libellé.
    assert [call[0] for call in controller.calls] == ["announce_to_victim", "send_sms"]
    assert len(sink.by_type(EventType.ACTION)) == 2


async def test_les_autres_types_d_evenements_sont_ignores() -> None:
    runner, _, controller, _ = await _setup()

    await runner.handle_event(
        Event(
            call_id="CA1",
            t=1.0,
            type=EventType.TRANSCRIPT,
            speaker="caller",
            data={"text": "bonjour"},
        )
    )

    assert controller.calls == []


async def test_un_score_sur_un_appel_inconnu_est_ignore() -> None:
    runner, _, controller, _ = await _setup()

    event = Event(
        call_id="CA_inconnu",
        t=1.0,
        type=EventType.SCORE,
        data={"score": 90, "level": "intervene"},
    )
    await runner.handle_event(event)

    assert controller.calls == []
