"""L'Access Token Voice : ce qu'il autorise, pour qui, et pour combien de temps.

Ce jeton est la clé du téléphone de la personne protégée — il permet de
DÉCROCHER ses appels. Trois propriétés méritent donc un test chacune, et aucune
n'est vérifiable à l'œil dans un JWT :

- l'identité est bien `VOICE_CLIENT_IDENTITY` et pas autre chose (un jeton émis
  pour une mauvaise identité ne sonne jamais, et le symptôme est le silence) ;
- le grant autorise l'ENTRANT (sans `incoming_allow`, l'app se connecte,
  s'enregistre, et ne reçoit rien) ;
- la réponse HTTP ne contient aucun secret en clair. Le JWT lui-même est un
  porteur de droits, c'est son objet ; `TWILIO_API_KEY_SECRET` et
  `TWILIO_AUTH_TOKEN`, eux, signent — les livrer au navigateur rendrait le PIN
  décoratif.

Le JWT est décodé **sans vérifier la signature** : on inspecte ses claims, on ne
rejoue pas le travail de Twilio. `PyJWT` est une dépendance directe du paquet
`twilio` (`Requires: aiohttp, aiohttp-retry, pyjwt, requests`), donc aucune
dépendance n'est ajoutée au projet pour ces tests.

Toutes les valeurs sont manifestement factices : rien ici ne doit ressembler à
un identifiant réel, ni dans le code ni dans une sortie de test copiée-collée.
Le secret fait exprès plus de 32 caractères, pour que PyJWT ne couvre pas la
sortie de tests d'un avertissement sur la longueur de clé HMAC.
"""

from __future__ import annotations

import jwt as pyjwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vocalguard.config import Settings
from vocalguard.interface.http import phone_app

ACCOUNT_SID = "ACfactice000000000000000000000001"
API_KEY_SID = "SKfactice000000000000000000000002"
API_KEY_SECRET = "secret-dapi-key-totalement-factice-pour-les-tests"
TWIML_APP_SID = "APfactice000000000000000000000003"
AUTH_TOKEN = "jeton-de-compte-factice-jamais-envoye-au-navigateur"
IDENTITE = "mamie"
PIN = "4321"


def make_settings(**overrides: object) -> Settings:
    """Réglages isolés du `.env` de la machine : sinon le test dépend du poste."""
    base: dict[str, object] = {
        "twilio_account_sid": ACCOUNT_SID,
        "twilio_auth_token": AUTH_TOKEN,
        "twilio_api_key_sid": API_KEY_SID,
        "twilio_api_key_secret": API_KEY_SECRET,
        "twilio_twiml_app_sid": TWIML_APP_SID,
        "voice_client_identity": IDENTITE,
        "app_pin": PIN,
        "app_token_ttl_seconds": 900,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def make_client(**overrides: object) -> TestClient:
    """Une app réduite au seul router de l'app téléphone.

    Pas `create_app` : ce fichier teste la fabrication du jeton, pas le câblage
    du pipeline. Une app minimale rend l'échec lisible — si le test rougit, c'est
    le jeton, pas Transcribe ni le carnet.
    """
    app = FastAPI()
    app.state.settings = make_settings(**overrides)
    app.include_router(phone_app.router)
    return TestClient(app)


def demander_jeton(client: TestClient, pin: str = PIN):
    return client.post("/app/token", json={"pin": pin})


def claims(jeton: str) -> dict:
    """Claims du JWT, signature non vérifiée : on inspecte, on ne valide pas."""
    return pyjwt.decode(jeton, options={"verify_signature": False})


class TestContenuDuJeton:
    def test_le_jeton_porte_l_identite_du_client_voice(self) -> None:
        """Une identité différente de `VOICE_CLIENT_IDENTITY` donne une app qui
        s'enregistre et ne sonne jamais — une panne entièrement silencieuse."""
        reponse = demander_jeton(make_client())
        assert reponse.status_code == 200
        assert claims(reponse.json()["token"])["grants"]["identity"] == IDENTITE

    def test_le_jeton_autorise_les_appels_entrants(self) -> None:
        """La seule permission dont l'app a besoin : décrocher. Sans elle, Twilio
        accepte l'enregistrement et n'envoie jamais l'appel."""
        reponse = demander_jeton(make_client())
        voice = claims(reponse.json()["token"])["grants"]["voice"]
        assert voice["incoming"]["allow"] is True

    def test_le_jeton_porte_la_twiml_app(self) -> None:
        """Nous ne passons aucun appel SORTANT depuis l'app, mais le `VoiceGrant`
        exige quand même la TwiML App : sans elle, Twilio refuse le jeton."""
        reponse = demander_jeton(make_client())
        voice = claims(reponse.json()["token"])["grants"]["voice"]
        assert voice["outgoing"]["application_sid"] == TWIML_APP_SID

    def test_le_jeton_est_signe_par_l_api_key_pas_par_le_compte(self) -> None:
        """Le compte-token ne sait pas signer un jeton Voice : c'est l'API Key
        qui émet (`iss`) pour le compte (`sub`)."""
        payload = claims(demander_jeton(make_client()).json()["token"])
        assert payload["iss"] == API_KEY_SID
        assert payload["sub"] == ACCOUNT_SID

    def test_la_duree_de_vie_vient_du_reglage(self) -> None:
        """Courte par principe, mais réglable : l'app doit pouvoir sonner une
        heure après la saisie du code, sans la redemander."""
        payload = claims(demander_jeton(make_client(app_token_ttl_seconds=900)).json()["token"])
        assert payload["exp"] - payload["nbf"] == 900


class TestFormeDeLaReponse:
    def test_la_reponse_annonce_l_identite_et_la_duree(self) -> None:
        """La page en a besoin pour afficher l'état et pour renouveler le jeton
        avant qu'il expire."""
        charge = demander_jeton(make_client()).json()
        assert charge["identity"] == IDENTITE
        assert charge["ttl_seconds"] == 900
        assert charge["token"]

    def test_la_reponse_ne_contient_aucun_secret_hors_le_jwt(self) -> None:
        """Le test qui compte vraiment. Le JWT est un porteur de droits et c'est
        normal ; le secret d'API Key et le compte-token, eux, SIGNENT — les
        envoyer au navigateur rendrait le code d'accès décoratif."""
        brut = demander_jeton(make_client()).text
        assert API_KEY_SECRET not in brut
        assert AUTH_TOKEN not in brut
        assert PIN not in brut

    def test_le_jeton_n_est_pas_journalise(self, caplog: pytest.LogCaptureFixture) -> None:
        """Un journal de démo se partage, se copie-colle et finit en capture
        d'écran. Le jeton y vivrait une heure de trop."""
        with caplog.at_level("INFO", logger="vocalguard.interface.http.phone_app"):
            jeton = demander_jeton(make_client()).json()["token"]
        assert jeton not in caplog.text
        assert API_KEY_SECRET not in caplog.text
        # La trace reste utile : on doit savoir qu'un jeton est parti, et pour qui.
        assert IDENTITE in caplog.text


class TestConfigurationIncomplete:
    """Il manque toujours la dernière valeur quand on croit avoir fini, et le
    symptôme naturel d'une API Key absente est un `401` opaque côté navigateur,
    à cinq minutes de la démo, sans rien pour savoir lequel des cinq réglages
    fait défaut."""

    @pytest.mark.parametrize(
        ("champ", "nom_env"),
        [
            ("twilio_account_sid", "TWILIO_ACCOUNT_SID"),
            ("twilio_api_key_sid", "TWILIO_API_KEY_SID"),
            ("twilio_api_key_secret", "TWILIO_API_KEY_SECRET"),
            ("twilio_twiml_app_sid", "TWILIO_TWIML_APP_SID"),
            ("voice_client_identity", "VOICE_CLIENT_IDENTITY"),
        ],
    )
    def test_le_reglage_absent_est_nomme_et_aucun_jeton_ne_part(
        self, champ: str, nom_env: str
    ) -> None:
        reponse = demander_jeton(make_client(**{champ: ""}))
        assert reponse.status_code == 503
        assert nom_env in reponse.json()["detail"]
        assert "token" not in reponse.json()

    @pytest.mark.parametrize(
        "champ",
        [
            "twilio_account_sid",
            "twilio_api_key_sid",
            "twilio_api_key_secret",
            "twilio_twiml_app_sid",
            "voice_client_identity",
        ],
    )
    def test_la_liste_des_manquants_suit_app_token_configured(self, champ: str) -> None:
        """`reglages_manquants` double la liste de `Settings.app_token_configured`
        — la première dit « lequel », la seconde « oui ou non ». Deux listes en
        parallèle divergent au premier réglage ajouté : ce test est le seul
        garde-fou contre un message d'erreur qui oublie un champ."""
        incomplet = make_settings(**{champ: ""})
        assert phone_app.reglages_manquants(incomplet)
        assert incomplet.app_token_configured is False

        complet = make_settings()
        assert phone_app.reglages_manquants(complet) == []
        assert complet.app_token_configured is True

    def test_la_configuration_n_est_pas_racontee_avant_le_code(self) -> None:
        """L'ordre des gardes compte : la liste des réglages d'un serveur ne se
        détaille pas à un inconnu qui n'a même pas le bon code."""
        reponse = demander_jeton(make_client(twilio_api_key_sid=""), pin="0000")
        assert reponse.status_code == 403
        assert "TWILIO_API_KEY_SID" not in reponse.text
