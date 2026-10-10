"""Le webhook qui recueille la réponse du proche au SMS de vérification.

Au niveau `alert`, le système envoie « Est-ce toi qui appelles en ce moment ? »
au vrai proche (`domain.alerts.SMS_VERIFY_CONTACT`). Sa réponse est
l'information la plus décisive du système : aucun score ne peut trancher ce que
le vrai Lucas tranche en trois lettres. Ce fichier prouve que `/twilio/sms/inbound`
la transmet au pipeline — et surtout qu'il ne la transmet PAS quand il n'est pas
sûr, parce qu'un faux « non » mettrait en attente un vrai petit-fils.

Le test qui compte le plus est `test_sms_d_un_numero_etranger_ne_declenche_rien` :
notre numéro Twilio est public, et sans le filtre sur l'expéditeur, un SMS
suffirait à faire interrompre un appel légitime.
"""

from __future__ import annotations

from types import SimpleNamespace
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient

from vocalguard.config import Settings
from vocalguard.infrastructure.fakes.event_sink import InMemoryEventSink
from vocalguard.infrastructure.fakes.transcriber import FakeTranscriber
from vocalguard.interface.app import create_app

CALL_ID = "CA_sms_01"
AUTRE_CALL_ID = "CA_sms_02"

CONTACT = "+33611111111"
"""Le proche de confiance, tel qu'il est écrit dans le `.env` de ce test."""

INCONNU = "+33799999999"
"""Un numéro quelconque : le scammeur, ou n'importe qui ayant vu notre numéro."""


def _app(**overrides):
    """App complète sur doublures : ni téléphone, ni AWS, ni réseau."""
    champs = {
        "transcribe_backend": "fake",
        "public_hostname": "demo.ngrok-free.app",
        "victim_phone_number": "+33600000000",
        "trusted_contact_phone_number": CONTACT,
        "log_level": "WARNING",
    }
    settings = Settings(_env_file=None, **(champs | overrides))
    return create_app(
        settings,
        sink=InMemoryEventSink(),
        transcriber_factory=FakeTranscriber,
    )


@pytest.fixture
def client():
    with TestClient(_app()) as c:
        yield c


def _ouvrir_appel(client: TestClient, call_id: str = CALL_ID) -> None:
    """Un appel en cours : sans lui, le SMS n'a aucun appel à désigner."""
    response = client.post(
        "/twilio/voice/inbound",
        data={"CallSid": call_id, "From": "+33755555555", "To": "+33600000000"},
    )
    assert response.status_code == 200


def _poser_hook(pipeline, monkeypatch, hook) -> None:
    """Installe la doublure de `contact_replied` sur le pipeline.

    `raising=False` : la méthode n'existe pas encore sur `ScoringPipeline` (Dev 2
    l'écrit en parallèle), et ces tests doivent passer avant comme après son
    arrivée.
    """
    monkeypatch.setattr(pipeline, "contact_replied", hook, raising=False)


@pytest.fixture
def appels_recus(client, monkeypatch):
    """Pose la doublure de `contact_replied` et rend son journal."""
    recu: list[tuple[str, bool]] = []

    def contact_replied(call_id: str, *, denied: bool) -> None:
        recu.append((call_id, denied))

    _poser_hook(client.app.state.pipeline, monkeypatch, contact_replied)
    return recu


def _sms(client: TestClient, body: str, *, from_number: str = CONTACT):
    response = client.post(
        "/twilio/sms/inbound",
        data={
            "From": from_number,
            "To": "+33700000000",
            "Body": body,
            "MessageSid": "SM_test_01",
        },
    )
    # Twilio attend du XML : un 200 vide suffirait à lui faire logguer une erreur
    # (et à rejouer le webhook), donc la forme est vérifiée à chaque appel.
    assert response.status_code == 200
    assert ElementTree.fromstring(response.text).tag == "Response"
    return response


@pytest.mark.parametrize(
    "body",
    [
        "non",
        "NON !!!",
        "Non, c'est pas moi",
        "nope",
        # Sans accent ni apostrophe : le proche tape d'un pouce, sous stress.
        "ce n est pas moi",
        "jamais appele",
        "No",
    ],
)
def test_un_non_du_contact_de_confiance_pese_sur_le_score(client, appels_recus, body):
    """Le seul cas qui déclenche quelque chose, dans toutes ses orthographes."""
    _ouvrir_appel(client)

    _sms(client, body)

    assert appels_recus == [(CALL_ID, True)], f"« {body} » doit compter comme un démenti"


def test_un_hook_asynchrone_est_attendu(client, monkeypatch):
    """Dev 2 peut écrire `contact_replied` en `async def` : les deux doivent passer.

    Sans `await`, la coroutine ne s'exécuterait jamais et Python ne le dirait
    qu'en warning — un signal perdu en silence, le pire des cas ici.
    """
    recu: list[tuple[str, bool]] = []

    async def contact_replied(call_id: str, *, denied: bool) -> None:
        recu.append((call_id, denied))

    _poser_hook(client.app.state.pipeline, monkeypatch, contact_replied)
    _ouvrir_appel(client)

    _sms(client, "non")

    assert recu == [(CALL_ID, True)]


@pytest.mark.parametrize("body", ["oui", "Oui c'est moi", "yes", "C EST BIEN MOI", "si"])
def test_un_oui_rassure_et_ne_declenche_rien(client, appels_recus, body):
    _ouvrir_appel(client)

    _sms(client, body)

    assert appels_recus == [], "un proche qui se confirme ne doit rien faire peser"


@pytest.mark.parametrize(
    "body",
    [
        "",
        "???",
        "Je suis dans le train, je te rappelle",
        "qui ca",
        "👍",
    ],
)
def test_un_corps_incompris_ne_declenche_rien(client, appels_recus, body):
    """On ne devine pas : un faux « denied » mettrait en attente un vrai appel.

    « Je te rappelle » n'est ni un oui ni un non — c'est précisément le genre de
    message qu'une heuristique trop zélée lirait comme un démenti.
    """
    _ouvrir_appel(client)

    _sms(client, body)

    assert appels_recus == []


def test_sms_d_un_numero_etranger_ne_declenche_rien(client, appels_recus):
    """LE test de sécurité : notre numéro Twilio est public.

    Sans le filtre sur l'expéditeur, n'importe qui — le scammeur en premier —
    enverrait « non » et ferait mettre en attente un appel légitime. C'est la
    seule authentification de cette route.
    """
    _ouvrir_appel(client)

    _sms(client, "non", from_number=INCONNU)
    # Numéro absent du formulaire : ne doit pas davantage passer pour le proche.
    _sms(client, "non", from_number="")

    assert appels_recus == []


def test_contact_reconnu_dans_un_autre_format(monkeypatch):
    """`.env` saisi à la main en national, Twilio qui poste de l'E.164.

    C'est le cas par défaut, pas un cas limite : personne ne tape « +33 » dans
    son `.env`. Une comparaison brute jetterait la vraie réponse du vrai proche.
    """
    recu: list[tuple[str, bool]] = []
    app = _app(trusted_contact_phone_number="06 12 34 56 78")
    _poser_hook(
        app.state.pipeline,
        monkeypatch,
        lambda call_id, *, denied: recu.append((call_id, denied)),
    )

    with TestClient(app) as client:
        _ouvrir_appel(client)
        _sms(client, "non", from_number="+33612345678")

    assert recu == [(CALL_ID, True)]


def test_reponse_arrivee_apres_la_fin_de_l_appel(client, appels_recus):
    """Cas réel et fréquent : le proche répond 30 s trop tard, l'appel est fini.

    Rien à faire peser, mais surtout : pas de 500. Twilio rejouerait le webhook
    et le dashboard de démo afficherait une erreur sans cause.
    """
    assert client.app.state.registry.active() == []

    _sms(client, "non")

    assert appels_recus == []


def test_deux_appels_actifs_rendent_la_reponse_inutilisable(client, appels_recus):
    """Le SMS ne porte aucun `call_id` : à deux appels, on ne peut pas choisir.

    Accuser le mauvais appel est pire que rater le signal — limite assumée de la
    démo, et c'est pour ça qu'elle a un test plutôt qu'un commentaire seul.
    """
    _ouvrir_appel(client, CALL_ID)
    _ouvrir_appel(client, AUTRE_CALL_ID)
    assert len(client.app.state.registry.active()) == 2

    _sms(client, "non")

    assert appels_recus == []


def test_pipeline_sans_contact_replied_ne_leve_pas(client):
    """L'état du dépôt aujourd'hui : le hook n'existe pas encore.

    Tant que Dev 2 ne l'a pas écrit, le webhook doit répondre normalement à
    Twilio et se contenter d'un warning. Un `SimpleNamespace` plutôt que le vrai
    pipeline : ce test doit continuer de prouver l'absence de levée le jour où
    `ScoringPipeline.contact_replied` existe.
    """
    client.app.state.pipeline = SimpleNamespace()
    _ouvrir_appel(client)

    _sms(client, "non")


def test_un_hook_qui_leve_ne_devient_pas_une_500(client, monkeypatch):
    """Une erreur du pipeline est à nous ; la 500 serait pour Twilio, qui
    rejouerait le SMS sans rien résoudre."""

    def contact_replied(call_id: str, *, denied: bool) -> None:
        raise RuntimeError("Bedrock injoignable")

    _poser_hook(client.app.state.pipeline, monkeypatch, contact_replied)
    _ouvrir_appel(client)

    _sms(client, "non")
