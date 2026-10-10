"""Le contrôleur agit sur un appel réel : tout est testé contre un client Twilio
simulé. Ce qu'on vérifie surtout, c'est qu'un échec réseau renvoie `False` au lieu
de remonter — pendant un appel, une annonce ratée ne doit pas tuer la session."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from twilio.base.exceptions import TwilioRestException

from vocalguard.config import Settings
from vocalguard.infrastructure.telephony.client import TwilioCallController

CALLER_SID = "CAcaller0000000000000000000000000"
VICTIM_SID = "CAvictim0000000000000000000000000"
CONFERENCE_SID = "CF00000000000000000000000000000000"


def make_settings(**overrides: object) -> Settings:
    """Settings isolés du `.env` de la machine, sinon le test dépend du poste."""
    base: dict[str, object] = {
        "public_hostname": "a1b2.ngrok-free.app",
        "twilio_account_sid": "AC00000000000000000000000000000000",
        "twilio_auth_token": "token",
        "twilio_phone_number": "+15550000000",
        "victim_phone_number": "+33600000000",
        "twilio_conference_name": "utopia-guard",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


@pytest.fixture
def client() -> MagicMock:
    """Client Twilio simulé avec une conférence en cours et ses deux participants."""
    mock = MagicMock()

    conference = MagicMock()
    conference.sid = CONFERENCE_SID
    mock.conferences.list.return_value = [conference]

    caller = MagicMock()
    caller.call_sid = CALLER_SID
    victim = MagicMock()
    victim.call_sid = VICTIM_SID
    mock.conferences.return_value.participants.list.return_value = [caller, victim]

    return mock


@pytest.fixture
def controller(client: MagicMock) -> TwilioCallController:
    return TwilioCallController(make_settings(), client=client)


def updated_participant(client: MagicMock) -> str:
    """Le `call_sid` passé à `participants(...)` lors du dernier update."""
    return client.conferences.return_value.participants.call_args.args[0]


def update_kwargs(client: MagicMock) -> dict[str, object]:
    return client.conferences.return_value.participants.return_value.update.call_args.kwargs


class TestParticipantResolution:
    async def test_conference_looked_up_by_name(
        self, controller: TwilioCallController, client: MagicMock
    ) -> None:
        await controller.hold_caller(CALLER_SID)
        kwargs = client.conferences.list.call_args.kwargs
        assert kwargs["friendly_name"] == f"utopia-guard-{CALLER_SID}"
        assert kwargs["status"] == "in-progress"

    async def test_conference_sid_cached(
        self, controller: TwilioCallController, client: MagicMock
    ) -> None:
        """Deux actions peuvent s'enchaîner en deux secondes : un lookup suffit."""
        await controller.hold_caller(CALLER_SID)
        await controller.resume_caller(CALLER_SID)
        assert client.conferences.list.call_count == 1

    async def test_caller_is_the_inbound_leg(
        self, controller: TwilioCallController, client: MagicMock
    ) -> None:
        await controller.hold_caller(CALLER_SID)
        assert updated_participant(client) == CALLER_SID

    async def test_victim_is_the_other_leg(
        self, controller: TwilioCallController, client: MagicMock
    ) -> None:
        """L'annonce ne doit jamais partir vers l'appelant : il entendrait qu'il est
        repéré."""
        await controller.announce_to_victim(CALLER_SID, "Attention")
        assert updated_participant(client) == VICTIM_SID

    async def test_conference_not_started_yet(
        self, controller: TwilioCallController, client: MagicMock
    ) -> None:
        """Avant que la victime décroche, la conférence n'existe pas côté Twilio."""
        client.conferences.list.return_value = []
        assert await controller.hold_caller(CALLER_SID) is False

    async def test_victim_absent_from_conference(
        self, controller: TwilioCallController, client: MagicMock
    ) -> None:
        caller = MagicMock()
        caller.call_sid = CALLER_SID
        client.conferences.return_value.participants.list.return_value = [caller]
        assert await controller.announce_to_victim(CALLER_SID, "Attention") is False


class TestHoldAndResume:
    async def test_hold_plays_music_from_our_webhook(
        self, controller: TwilioCallController, client: MagicMock
    ) -> None:
        assert await controller.hold_caller(CALLER_SID) is True
        kwargs = update_kwargs(client)
        assert kwargs["hold"] is True
        assert kwargs["hold_url"] == "https://a1b2.ngrok-free.app/twilio/voice/hold-music"
        assert kwargs["hold_method"] == "POST"

    async def test_resume_releases_hold(
        self, controller: TwilioCallController, client: MagicMock
    ) -> None:
        assert await controller.resume_caller(CALLER_SID) is True
        assert update_kwargs(client) == {"hold": False}

    async def test_no_hangup_available(self, controller: TwilioCallController) -> None:
        """Pas de raccrochage automatique, même par erreur : un faux positif sur un
        vrai appel d'urgence serait grave."""
        assert not [name for name in dir(controller) if "hangup" in name.lower()]


class TestAnnounce:
    async def test_message_queued_before_api_call(
        self, controller: TwilioCallController, client: MagicMock
    ) -> None:
        """Twilio peut venir chercher `announce_url` immédiatement après l'update."""
        assert await controller.announce_to_victim(CALLER_SID, "Attention") is True
        assert controller.announcements.pending(CALLER_SID) == ("Attention",)
        kwargs = update_kwargs(client)
        assert kwargs["announce_url"] == (
            f"https://a1b2.ngrok-free.app/twilio/voice/announce/{CALLER_SID}"
        )
        assert kwargs["announce_method"] == "POST"

    async def test_failed_announce_does_not_leave_orphan_message(
        self, controller: TwilioCallController, client: MagicMock
    ) -> None:
        """Sinon la phrase serait jouée à la prochaine annonce réussie, avec un
        décalage incompréhensible pour la victime."""
        client.conferences.return_value.participants.return_value.update.side_effect = (
            TwilioRestException(400, "uri", "boom", code=20001)
        )
        assert await controller.announce_to_victim(CALLER_SID, "Attention") is False
        assert controller.announcements.pending(CALLER_SID) == ()

    async def test_announcements_are_fifo(
        self, controller: TwilioCallController, client: MagicMock
    ) -> None:
        """Le score peut passer de 65 à 85 : alerte puis intervention, dans l'ordre."""
        await controller.announce_to_victim(CALLER_SID, "alerte")
        await controller.announce_to_victim(CALLER_SID, "intervention")
        assert controller.announcements.pop(CALLER_SID) == "alerte"
        assert controller.announcements.pop(CALLER_SID) == "intervention"
        assert controller.announcements.pop(CALLER_SID) is None

    async def test_une_annonce_ratee_n_emporte_pas_la_precedente(
        self, controller: TwilioCallController, client: MagicMock
    ) -> None:
        """L'annulation doit viser SA phrase, pas la plus ancienne.

        Scénario réel : l'alerte est en file, pas encore réclamée par Twilio ; le
        score passe à 85 ; l'annonce d'intervention échoue parce que la victime
        n'a pas encore rejoint la conférence. Avec un `pop()` (FIFO), c'est
        l'alerte qui disparaissait et le texte d'intervention qui restait — la
        victime s'entendait dire « nous avons mis l'appel en pause » pour une
        alerte où rien n'avait été mis en pause.
        """
        await controller.announce_to_victim(CALLER_SID, "alerte")
        assert controller.announcements.pending(CALLER_SID) == ("alerte",)

        client.conferences.return_value.participants.return_value.update.side_effect = (
            TwilioRestException(400, "uri", "boom", code=20001)
        )
        assert await controller.announce_to_victim(CALLER_SID, "intervention") is False

        assert controller.announcements.pending(CALLER_SID) == ("alerte",)


class TestDialVictim:
    async def test_creates_leg_towards_join_webhook(
        self, controller: TwilioCallController, client: MagicMock
    ) -> None:
        client.calls.create.return_value.sid = VICTIM_SID
        assert await controller.dial_victim(CALLER_SID) == VICTIM_SID
        kwargs = client.calls.create.call_args.kwargs
        assert kwargs["to"] == "+33600000000"
        assert kwargs["from_"] == "+15550000000"
        assert kwargs["url"].endswith(f"/twilio/voice/join/{CALLER_SID}")

    async def test_explicit_number_wins(
        self, controller: TwilioCallController, client: MagicMock
    ) -> None:
        client.calls.create.return_value.sid = VICTIM_SID
        await controller.dial_victim(CALLER_SID, to_number="+33611111111")
        assert client.calls.create.call_args.kwargs["to"] == "+33611111111"

    async def test_unverified_number_returns_none(
        self, controller: TwilioCallController, client: MagicMock
    ) -> None:
        """Compte d'essai : appeler un numéro non vérifié échoue (code 21219)."""
        client.calls.create.side_effect = TwilioRestException(400, "uri", "unverified", code=21219)
        assert await controller.dial_victim(CALLER_SID) is None

    async def test_missing_victim_number(self, client: MagicMock) -> None:
        controller = TwilioCallController(make_settings(victim_phone_number=""), client=client)
        assert await controller.dial_victim(CALLER_SID) is None
        client.calls.create.assert_not_called()


class TestSms:
    async def test_sends_from_twilio_number(
        self, controller: TwilioCallController, client: MagicMock
    ) -> None:
        assert await controller.send_sms("+33611111111", "Est-ce toi qui appelles ?") is True
        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["to"] == "+33611111111"
        assert kwargs["from_"] == "+15550000000"
        assert kwargs["body"] == "Est-ce toi qui appelles ?"

    async def test_failure_returns_false(
        self, controller: TwilioCallController, client: MagicMock
    ) -> None:
        client.messages.create.side_effect = TwilioRestException(400, "uri", "boom", code=21608)
        assert await controller.send_sms("+33611111111", "coucou") is False

    async def test_empty_destination(
        self, controller: TwilioCallController, client: MagicMock
    ) -> None:
        assert await controller.send_sms("", "coucou") is False
        client.messages.create.assert_not_called()


class TestDegradedConfiguration:
    """On doit pouvoir démarrer l'app sans téléphonie : Dev 2 et Biz 1 travaillent
    sur de faux événements tant que Twilio n'est pas prêt."""

    async def test_without_credentials_nothing_raises(self) -> None:
        controller = TwilioCallController(
            make_settings(twilio_account_sid="", twilio_auth_token="")
        )
        assert await controller.hold_caller(CALLER_SID) is False
        assert await controller.resume_caller(CALLER_SID) is False
        assert await controller.announce_to_victim(CALLER_SID, "Attention") is False
        assert await controller.send_sms("+33611111111", "coucou") is False
        assert await controller.dial_victim(CALLER_SID) is None

    async def test_without_public_hostname(self, client: MagicMock) -> None:
        """Sans ngrok, `hold_url` pointerait vers `https:///...` : autant le dire."""
        controller = TwilioCallController(make_settings(public_hostname=""), client=client)
        assert await controller.hold_caller(CALLER_SID) is False
        client.conferences.list.assert_not_called()

    async def test_sms_works_without_public_hostname(self, client: MagicMock) -> None:
        """Le SMS ne dépend d'aucun callback : il doit marcher sans tunnel."""
        controller = TwilioCallController(make_settings(public_hostname=""), client=client)
        assert await controller.send_sms("+33611111111", "coucou") is True


class TestForget:
    async def test_forget_clears_cache_and_queue(
        self, controller: TwilioCallController, client: MagicMock
    ) -> None:
        await controller.announce_to_victim(CALLER_SID, "Attention")
        controller.forget(CALLER_SID)
        assert controller.announcements.pending(CALLER_SID) == ()
        await controller.hold_caller(CALLER_SID)
        # Le lookup est refait : le cache a bien été vidé.
        assert client.conferences.list.call_count == 2
