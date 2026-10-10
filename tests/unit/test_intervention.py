"""Du score aux actions Twilio : ordre, idempotence, et robustesse aux pannes."""

from __future__ import annotations

import asyncio
from typing import Any

from vocalguard.application.intervention import InterventionRunner
from vocalguard.application.sessions import CallSession, SessionRegistry
from vocalguard.config import Settings
from vocalguard.domain.alerts import ANNOUNCE_ALERT, SMS_VERIFY_CONTACT, AlertLevel
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

    # Le SMS de vérification ne part qu'une fois : il figure dans les deux
    # paliers, mais le proche ne doit pas recevoir deux fois la même question.
    assert [call[0] for call in controller.calls] == [
        "announce_to_victim",
        "send_sms",
        "hold_caller",
        "announce_to_victim",
    ]
    assert session.caller_held is True
    assert session.contact_texted is True


async def test_le_sms_de_verification_ne_part_qu_une_fois_de_65_a_85() -> None:
    """Le scénario exact de la démo : 65 puis 85. Sans la garde
    `contact_already_texted`, le vrai proche reçoit deux fois « Est-ce toi qui
    appelles en ce moment ? » à quelques secondes d'intervalle."""
    runner, _, controller, sink = await _setup()

    await runner.handle_event(_score_event(65, "alert"))
    await runner.handle_event(_score_event(85, "intervene"))

    envois = [call for call in controller.calls if call[0] == "send_sms"]
    assert envois == [("send_sms", CONTACT, SMS_VERIFY_CONTACT)]
    actions = [event.data["action"] for event in sink.by_type(EventType.ACTION)]
    assert actions.count("sms_contact") == 1


async def test_un_sms_en_echec_est_retente_au_score_suivant() -> None:
    """Symétrie de la garde : seul un SMS *parti* ferme la porte."""

    class SmsFlaky(LoggingCallController):
        def __init__(self) -> None:
            super().__init__()
            self.tentatives = 0

        async def send_sms(self, to_number: str, message: str) -> bool:
            self.tentatives += 1
            if self.tentatives == 1:
                return False
            return await super().send_sms(to_number, message)

    controller = SmsFlaky()
    runner, session, _, _ = await _setup(controller=controller)

    await runner.handle_event(_score_event(70, "alert"))
    assert session.contact_texted is False

    await runner.handle_event(_score_event(72, "alert"))

    assert controller.tentatives == 2
    assert session.contact_texted is True
    # L'annonce, elle, avait réussi du premier coup : elle n'est pas rejouée.
    assert [call[0] for call in controller.calls].count("announce_to_victim") == 1


async def test_une_annonce_ratee_est_rejouee_quand_la_victime_a_rejoint() -> None:
    """La condition de course la plus dangereuse de la démo.

    L'appelant parle pendant la sonnerie, le score atteint 90 avant que la
    victime décroche : `announce_to_victim` échoue parce que le participant
    victime n'est pas encore dans la conférence. Sans cette correction, le
    palier `intervene` était marqué atteint et la victime n'entendait plus
    jamais rien — appelant en attente, téléphone muet.
    """

    class AnnounceFailsUntilVictimJoins(LoggingCallController):
        def __init__(self) -> None:
            super().__init__()
            self.victime_presente = False

        async def announce_to_victim(self, call_id: str, message: str) -> bool:
            if not self.victime_presente:
                return False
            return await super().announce_to_victim(call_id, message)

    controller = AnnounceFailsUntilVictimJoins()
    runner, session, _, sink = await _setup(controller=controller)

    await runner.handle_event(_score_event(90, "intervene"))
    assert [call[0] for call in controller.calls] == ["hold_caller", "send_sms"]
    assert session.acted_level is AlertLevel.NONE, "un palier raté ne doit pas être marqué atteint"

    controller.victime_presente = True
    await runner.handle_event(_score_event(95, "intervene"))

    # L'annonce finit par être jouée, et rien d'autre n'est rejoué : ni une
    # seconde mise en attente, ni un second SMS.
    assert [call[0] for call in controller.calls] == [
        "hold_caller",
        "send_sms",
        "announce_to_victim",
    ]
    assert session.acted_level is AlertLevel.INTERVENE
    annonces = [
        event.data["ok"]
        for event in sink.by_type(EventType.ACTION)
        if event.data["action"] == "announce_victim"
    ]
    assert annonces == [False, True]


async def test_une_annonce_reussie_n_est_pas_rejouee_par_un_score_qui_oscille() -> None:
    """L'autre moitié du contrat, aussi importante : le palier ne se rejoue que
    pour ce qui a ÉCHOUÉ. Ici le SMS échoue indéfiniment (numéro refusé) et le
    score oscille — l'annonce ne doit pas être répétée dans l'oreille de la
    victime à chaque score."""

    class SmsAlwaysFails(LoggingCallController):
        def __init__(self) -> None:
            super().__init__()
            self.tentatives_sms = 0

        async def send_sms(self, to_number: str, message: str) -> bool:
            self.tentatives_sms += 1
            return False

    controller = SmsAlwaysFails()
    runner, session, _, _ = await _setup(controller=controller)

    for score in (90, 86, 92, 88):
        await runner.handle_event(_score_event(score, "intervene"))

    kinds = [call[0] for call in controller.calls]
    assert kinds.count("announce_to_victim") == 1
    assert kinds.count("hold_caller") == 1
    # Seule l'action en échec est retentée, à chaque score.
    assert controller.tentatives_sms == 4
    assert session.acted_level is AlertLevel.NONE


async def test_deux_scores_en_parallele_ne_doublent_pas_les_actions() -> None:
    """L'ancien code marquait `acted_level` avant exécution pour ça : la
    réservation par action doit garder la propriété."""
    runner, _, controller, _ = await _setup()

    await asyncio.gather(
        runner.handle_event(_score_event(90, "intervene")),
        runner.handle_event(_score_event(91, "intervene")),
    )

    kinds = [call[0] for call in controller.calls]
    assert kinds == ["hold_caller", "announce_to_victim", "send_sms"]


async def test_une_reprise_manuelle_ne_remet_pas_l_appelant_en_attente() -> None:
    """`interface/http/control.py` remet `caller_held` à False sans toucher
    `acted_level` : la décision humaine l'emporte sur le score. Elle doit y
    survivre même quand `acted_level` est resté en arrière à cause d'une action
    ratée — sinon le prochain score remettrait le scammeur en attente."""

    class AnnounceFails(LoggingCallController):
        async def announce_to_victim(self, call_id: str, message: str) -> bool:
            return False

    controller = AnnounceFails()
    runner, session, _, _ = await _setup(controller=controller)

    await runner.handle_event(_score_event(90, "intervene"))
    assert session.caller_held is True
    assert session.acted_level is AlertLevel.NONE

    # L'opérateur conclut au faux positif et rend la parole à l'appelant.
    session.caller_held = False

    await runner.handle_event(_score_event(95, "intervene"))

    assert [call[0] for call in controller.calls].count("hold_caller") == 1
    assert session.caller_held is False


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
