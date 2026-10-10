"""L'app sur le téléphone : la page, le code d'accès, et le choix de la jambe.

Ce que ces tests protègent n'est pas de la plomberie HTTP mais quatre décisions
dont l'échec serait silencieux ou dangereux :

- **le code d'accès est obligatoire**. L'URL ngrok est publique et un Access
  Token Voice permet de DÉCROCHER les appels de la personne protégée. Une route
  `/app/token` ouverte, c'est son téléphone posé sur Internet ;
- **`APP_PIN` vide refuse tout**. Le réflexe naturel (« pas de code configuré,
  donc pas de vérification ») donnerait exactement la route ouverte ci-dessus.
  On préfère une app inutilisable à une app ouverte ;
- **les codes ne sont jamais journalisés**, ni l'attendu ni le reçu : à un
  chiffre près, c'est le même, et un journal de démo finit en capture d'écran ;
- **`dial_victim` vise l'app quand elle existe, le téléphone sinon**. Viser le
  numéro public de la personne protégée créerait une boucle de renvoi facturée à
  chaque tour ; perdre le repli téléphonique rendrait l'app obligatoire, alors
  qu'une page web ne sonne que si elle est ouverte.

Même montage que `test_control_routes.py` : app complète sur les doublures de
`infrastructure/fakes/`, donc aucun réseau, aucun téléphone, aucun credential.
`TWILIO_AUTH_TOKEN` reste volontairement vide pour que `create_app` câble le
contrôleur factice : avec un compte-token renseigné, la jambe victime partirait
pour de vrai en tâche de fond à chaque webhook entrant de ce fichier.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from vocalguard.config import Settings
from vocalguard.infrastructure.fakes.event_sink import InMemoryEventSink
from vocalguard.infrastructure.fakes.transcriber import FakeTranscriber
from vocalguard.infrastructure.telephony.client import TwilioCallController
from vocalguard.interface.app import create_app

PIN = "432198"
MAUVAIS_PIN = "0000"

ACCOUNT_SID = "ACfactice000000000000000000000001"
API_KEY_SID = "SKfactice000000000000000000000002"
API_KEY_SECRET = "secret-dapi-key-totalement-factice-pour-les-tests"
TWIML_APP_SID = "APfactice000000000000000000000003"

CALL_ID = "CA_app_01"
NUMERO_LUCAS = "+33600000011"
NUMERO_INCONNU = "+33755555555"


@pytest.fixture
def carnet(tmp_path: Path) -> Path:
    """Carnet d'adresses à nous : `data/contacts.json` est le vrai carnet de la
    démo (gitignoré, donc absent d'un clone neuf) et un test ne doit dépendre ni
    de son existence ni de son contenu."""
    chemin = tmp_path / "contacts.json"
    chemin.write_text(
        json.dumps(
            {
                "owner": "Jeanne",
                "contacts": [
                    {
                        "name": "Lucas Martin",
                        "phone_number": NUMERO_LUCAS,
                        "relation": "petit-fils",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return chemin


def make_settings(carnet: Path, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "transcribe_backend": "fake",
        "public_hostname": "demo.ngrok-free.app",
        "victim_phone_number": "+33600000099",
        "contacts_path": carnet,
        "twilio_account_sid": ACCOUNT_SID,
        # Vide exprès : c'est lui qui déciderait `twilio_configured`, donc un
        # vrai client Twilio et un vrai appel sortant en tâche de fond.
        "twilio_auth_token": "",
        "twilio_api_key_sid": API_KEY_SID,
        "twilio_api_key_secret": API_KEY_SECRET,
        "twilio_twiml_app_sid": TWIML_APP_SID,
        "voice_client_identity": "mamie",
        "app_pin": PIN,
        "log_level": "WARNING",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


@pytest.fixture
def harness(carnet: Path):
    app = create_app(
        make_settings(carnet), sink=InMemoryEventSink(), transcriber_factory=FakeTranscriber
    )
    with TestClient(app) as client:
        yield client, app


@pytest.fixture
def sans_code(carnet: Path):
    """La configuration dangereuse : aucun `APP_PIN`."""
    app = create_app(
        make_settings(carnet, app_pin=""),
        sink=InMemoryEventSink(),
        transcriber_factory=FakeTranscriber,
    )
    with TestClient(app) as client:
        yield client


def ouvrir_appel(client: TestClient, numero: str = NUMERO_INCONNU) -> None:
    """Un appel en cours, ouvert par le même chemin que la production."""
    client.post(
        "/twilio/voice/inbound",
        data={"CallSid": CALL_ID, "From": numero, "To": "+15550000000"},
    )


# --- La page ----------------------------------------------------------------


def test_la_page_de_l_app_est_servie(harness) -> None:
    client, _ = harness
    reponse = client.get("/app")
    assert reponse.status_code == 200
    assert reponse.headers["content-type"].startswith("text/html")
    assert "Cut the Line" in reponse.text
    # La limite la plus importante de tout le lot doit rester écrite à l'écran :
    # une page web ne sonne que si elle est ouverte.
    assert "Keep this page open" in reponse.text


def test_le_manifeste_porte_son_type_mime(harness) -> None:
    """Servi en `text/plain`, Chrome ignore le manifeste en silence et le bouton
    « Installer » n'apparaît jamais — un symptôme sans message d'erreur."""
    client, _ = harness
    reponse = client.get("/app/manifest.webmanifest")
    assert reponse.status_code == 200
    assert reponse.headers["content-type"].startswith("application/manifest+json")
    assert reponse.json()["start_url"] == "/app"


def test_l_icone_est_servie(harness) -> None:
    client, _ = harness
    reponse = client.get("/app/icon.svg")
    assert reponse.status_code == 200
    assert reponse.headers["content-type"].startswith("image/svg+xml")


# --- Le code d'accès --------------------------------------------------------


def test_contacts_proteges_par_pin_et_sans_alias(harness) -> None:
    client, _ = harness
    assert client.post("/app/contacts", json={"pin": MAUVAIS_PIN}).status_code == 403
    response = client.post("/app/contacts", json={"pin": PIN})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "items": [
            {
                "name": "Lucas Martin",
                "number": NUMERO_LUCAS,
                "relation": "petit-fils",
                "mode": "trusted",
            }
        ]
    }


@pytest.mark.parametrize("route", ["/app/token", "/app/incoming"])
def test_les_routes_protegees_refusent_sans_code(harness, route: str) -> None:
    client, _ = harness
    reponse = client.post(route, json={})
    assert reponse.status_code == 403
    assert "token" not in reponse.text


@pytest.mark.parametrize("route", ["/app/token", "/app/incoming"])
def test_un_mauvais_code_est_refuse(harness, route: str) -> None:
    client, _ = harness
    reponse = client.post(route, json={"pin": MAUVAIS_PIN})
    assert reponse.status_code == 403
    assert "token" not in reponse.text


def test_le_refus_ne_journalise_aucun_code(harness, caplog: pytest.LogCaptureFixture) -> None:
    """Ni le code attendu ni le code reçu. Le reçu est tout aussi sensible que
    l'attendu : à un chiffre près, c'est le même."""
    client, _ = harness
    with caplog.at_level("WARNING", logger="vocalguard.interface.auth"):
        client.post("/app/token", json={"pin": MAUVAIS_PIN})
    assert caplog.text, "un refus doit laisser une trace"
    assert PIN not in caplog.text
    assert MAUVAIS_PIN not in caplog.text


def test_le_bon_code_donne_un_jeton(harness) -> None:
    client, _ = harness
    reponse = client.post("/app/token", json={"pin": PIN})
    assert reponse.status_code == 200
    charge = reponse.json()
    assert charge["token"]
    assert charge["identity"] == "mamie"
    # Le carnet déclare la personne protégée : l'écran d'attente la nomme.
    assert charge["owner"] == "Jeanne"
    # Aucun secret en clair hors le JWT, qui est lui-même le porteur de droits.
    assert API_KEY_SECRET not in reponse.text
    assert PIN not in reponse.text


@pytest.mark.parametrize("route", ["/app/token", "/app/incoming"])
def test_un_app_pin_vide_refuse_tout(sans_code: TestClient, route: str) -> None:
    """La configuration dangereuse. Sans ce refus, quiconque trouve l'URL ngrok
    obtient un jeton capable de décrocher les appels de la personne protégée."""
    reponse = sans_code.post(route, json={"pin": ""})
    assert reponse.status_code == 503
    assert "APP_PIN" in reponse.json()["detail"]
    assert "token" not in reponse.json()


def test_un_app_pin_vide_refuse_meme_un_code_devine(sans_code: TestClient) -> None:
    reponse = sans_code.post("/app/token", json={"pin": MAUVAIS_PIN})
    assert reponse.status_code == 503
    assert "token" not in reponse.json()


# --- Qui appelle en ce moment ----------------------------------------------


def test_sans_appel_actif_la_reponse_est_vide(harness) -> None:
    """Pas une erreur : la page interroge cette route dès que le SDK sonne, et le
    webhook entrant n'a parfois pas encore ouvert la session."""
    client, _ = harness
    charge = client.post("/app/incoming", json={"pin": PIN}).json()
    assert charge["call_id"] is None


def test_l_appel_en_cours_donne_le_call_id_et_le_numero(harness) -> None:
    """Le `call_id` est le seul moyen pour l'app d'agir sur l'appel : les routes
    `/control/calls/{call_id}/…` ne connaissent que le `CallSid` d'origine, que
    le SDK Voice ne voit jamais (il ne voit que celui de sa propre jambe)."""
    client, _ = harness
    ouvrir_appel(client, NUMERO_INCONNU)

    charge = client.post("/app/incoming", json={"pin": PIN}).json()

    assert charge["call_id"] == CALL_ID
    assert charge["caller_number"] == NUMERO_INCONNU
    assert charge["caller_name"] == ""
    assert charge["trusted"] is False


def test_un_contact_du_carnet_est_annonce_de_confiance(harness) -> None:
    """L'écran affiche « Appel de Lucas Martin · Contact de confiance » plutôt
    qu'un numéro que personne ne reconnaît — c'est tout l'intérêt de l'app."""
    client, _ = harness
    ouvrir_appel(client, NUMERO_LUCAS)

    charge = client.post("/app/incoming", json={"pin": PIN}).json()

    assert charge["caller_name"] == "Lucas Martin"
    assert charge["relation"] == "petit-fils"
    assert charge["trusted"] is True


def test_un_call_id_ambigu_ne_permet_pas_de_controler_une_autre_ligne(harness):
    client, _ = harness
    ouvrir_appel(client, NUMERO_INCONNU)
    client.post("/twilio/voice/inbound", data={"CallSid": "CA_autre", "From": NUMERO_LUCAS})
    charge = client.post("/app/incoming", json={"pin": PIN}).json()
    assert charge == {"call_id": None, "ambiguous": True}


def test_le_sid_du_sdk_designe_sa_ligne_meme_si_un_autre_appel_est_plus_recent(harness):
    client, app = harness
    ouvrir_appel(client, NUMERO_INCONNU)
    client.post("/twilio/voice/inbound", data={"CallSid": "CA_autre", "From": NUMERO_LUCAS})
    app.state.controller.call_id_for_leg = lambda sid: CALL_ID if sid == "CA_client" else None
    charge = client.post("/app/incoming", json={"pin": PIN, "sdk_call_id": "CA_client"}).json()
    assert charge["call_id"] == CALL_ID
    assert charge["caller_number"] == NUMERO_INCONNU
    assert charge["trusted"] is False
    unknown = client.post("/app/incoming", json={"pin": PIN, "sdk_call_id": "CA_étranger"}).json()
    assert unknown["call_id"] is None


def test_le_jeton_et_les_codes_http_ne_sont_pas_mis_en_cache(harness):
    client, _ = harness
    response = client.post("/app/token", json={"pin": PIN})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


# --- `dial_victim` : l'app ou le téléphone ---------------------------------
#
# Contrôleur testé avec un faux client Twilio injecté, comme dans
# `tests/unit/test_call_controller.py` : aucun appel réseau, aucun crédit.


def controleur(**overrides: object) -> tuple[TwilioCallController, MagicMock]:
    base: dict[str, object] = {
        "public_hostname": "a1b2.ngrok-free.app",
        "twilio_account_sid": ACCOUNT_SID,
        "twilio_auth_token": "jeton-factice",
        "twilio_phone_number": "+15550000000",
        "victim_phone_number": "+33600000099",
    }
    base.update(overrides)
    faux = MagicMock()
    faux.calls.create.return_value.sid = "CAjambe00000000000000000000000001"
    return TwilioCallController(Settings(_env_file=None, **base), client=faux), faux  # type: ignore[arg-type]


class TestChoixDeLaJambeVictime:
    async def test_l_app_est_visee_quand_l_identite_est_remplie(self) -> None:
        """`client:mamie` et pas un numéro : c'est ce qui évite la boucle de
        renvoi, et ce qui permet d'afficher le vrai appelant à l'écran."""
        ctrl, faux = controleur(voice_client_identity="mamie")
        assert await ctrl.dial_victim(CALL_ID)
        assert faux.calls.create.call_args.kwargs["to"] == "client:mamie"

    async def test_le_telephone_est_le_repli_quand_il_n_y_a_pas_d_app(self) -> None:
        """Le comportement d'avant l'app, qui doit rester intact : une page web
        ne sonne que si elle est ouverte."""
        ctrl, faux = controleur(voice_client_identity="")
        assert await ctrl.dial_victim(CALL_ID)
        assert faux.calls.create.call_args.kwargs["to"] == "+33600000099"

    async def test_un_numero_explicite_passe_devant_l_app(self) -> None:
        """La surcharge de l'appelant reste la plus forte : sinon un appel dirigé
        à la main vers un numéro précis partirait quand même vers l'app."""
        ctrl, faux = controleur(voice_client_identity="mamie")
        await ctrl.dial_victim(CALL_ID, to_number="+33611111111")
        assert faux.calls.create.call_args.kwargs["to"] == "+33611111111"

    async def test_sans_app_ni_telephone_rien_ne_part(self) -> None:
        """Et le message nomme les DEUX réglages : depuis l'app, il y a deux
        façons de faire sonner la personne protégée, et l'ancien message n'en
        citait qu'une — de quoi chercher dix minutes un numéro devenu inutile."""
        ctrl, faux = controleur(voice_client_identity="", victim_phone_number="")
        assert await ctrl.dial_victim(CALL_ID) is None
        faux.calls.create.assert_not_called()

    async def test_les_deux_destinations_se_loguent_distinctement(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Pendant une démo, « pourquoi ça n'a pas sonné dans l'app ? » doit
        trouver sa réponse dans le journal, sans relire le code."""
        with caplog.at_level("INFO", logger="vocalguard.infrastructure.telephony.client"):
            ctrl, _ = controleur(voice_client_identity="mamie")
            await ctrl.dial_victim(CALL_ID)
            assert "vers l'app" in caplog.text

            caplog.clear()
            ctrl, _ = controleur(voice_client_identity="")
            await ctrl.dial_victim(CALL_ID)
            assert "repli" in caplog.text

    async def test_l_appelant_voyage_dans_l_url_de_la_jambe(self) -> None:
        """Seul canal que `calls.create` laisse ouvert : la méthode documentée
        par Twilio pour remplir `Call.customParameters` est la noun `<Parameter>`
        de `<Dial><Client>`, que cette jambe n'emprunte pas."""
        ctrl, faux = controleur(voice_client_identity="mamie")
        await ctrl.dial_victim(CALL_ID, caller_number=NUMERO_LUCAS, caller_name="Lucas Martin")
        url = faux.calls.create.call_args.kwargs["url"]
        assert f"/twilio/voice/join/{CALL_ID}?" in url
        assert "caller_number=%2B33600000011" in url
        assert "caller_name=Lucas+Martin" in url

    async def test_sans_appelant_connu_l_url_ne_change_pas(self) -> None:
        """Les appelants existants ne passent pas ces valeurs : l'URL doit rester
        exactement celle d'avant, sans query string vide."""
        ctrl, faux = controleur(voice_client_identity="mamie")
        await ctrl.dial_victim(CALL_ID)
        assert faux.calls.create.call_args.kwargs["url"].endswith(f"/twilio/voice/join/{CALL_ID}")
