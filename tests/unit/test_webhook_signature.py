"""Validation de `X-Twilio-Signature` sur les routes `/twilio/**`.

Sans elle, derrière un tunnel ngrok public, quiconque connaît l'URL peut poster
sur `/twilio/voice/status` (libérer une session en cours) ou forger un `From` sur
`/twilio/sms/inbound` (la garde expéditeur de cette route est sa seule
authentification). La garde existe donc — mais `TWILIO_VALIDATE_SIGNATURE` est
faux par défaut, parce qu'une URL publique mal reconstruite met TOUS les webhooks
en 403 (cf. `Settings.twilio_validate_signature`).

Ce fichier tient les deux bouts : éteinte, la garde ne change rien du tout ;
allumée, elle refuse ce qu'elle doit refuser.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from vocalguard.config import Settings
from vocalguard.infrastructure.fakes.event_sink import InMemoryEventSink
from vocalguard.infrastructure.fakes.transcriber import FakeTranscriber
from vocalguard.interface.app import create_app
from vocalguard.interface.http.webhooks import SIGNATURE_HEADER

HOSTNAME = "demo.ngrok-free.app"
"""L'hôte PUBLIC. Il ne vaut jamais celui que le serveur voit derrière le
tunnel, et c'est tout l'enjeu de la garde."""

TOKEN = "token_de_test_0123456789"

CHEMIN = "/twilio/voice/status"
"""La route la plus exposée : un POST suffit à libérer un appel en cours."""

CORPS = {"CallSid": "CA_signature_01", "CallStatus": "completed"}


def _app(**overrides):
    """App complète sur doublures : ni téléphone, ni AWS, ni réseau.

    `twilio_account_sid` reste vide volontairement : le contrôleur est alors la
    doublure qui trace, alors que la garde n'a besoin que du token.
    """
    champs = {
        "transcribe_backend": "fake",
        "public_hostname": HOSTNAME,
        "twilio_auth_token": TOKEN,
        "log_level": "CRITICAL",
    }
    settings = Settings(_env_file=None, **(champs | overrides))
    return create_app(settings, sink=InMemoryEventSink(), transcriber_factory=FakeTranscriber)


def _client(**overrides) -> TestClient:
    return TestClient(_app(**overrides))


def _signature(corps: dict[str, str], *, chemin: str = CHEMIN, token: str = TOKEN) -> str:
    """Signe comme Twilio : URL publique + champs du formulaire.

    C'est la définition même de la garde ; la calculer ici avec le SDK (et non
    la recopier) prouve qu'on valide bien l'algorithme de Twilio, pas le nôtre.
    """
    return RequestValidator(token).compute_signature(f"https://{HOSTNAME}{chemin}", corps)


def test_reglage_eteint_laisse_tout_passer_sans_en_tete():
    """Le défaut livré au jury : zéro changement de comportement.

    Les webhooks Twilio n'ont jamais été signés jusqu'ici et la démo tourne : la
    garde éteinte ne doit pas regarder l'en-tête, même absent.
    """
    with _client(twilio_validate_signature=False) as client:
        assert client.post(CHEMIN, data=CORPS).status_code == 200
        assert client.post("/twilio/sms/inbound", data={"From": "+33799999999"}).status_code == 200
        assert client.get("/twilio/voice/hold-music").status_code == 200


def test_signature_valide_passe():
    with _client(twilio_validate_signature=True) as client:
        response = client.post(CHEMIN, data=CORPS, headers={SIGNATURE_HEADER: _signature(CORPS)})

    assert response.status_code == 200


def test_signature_absente_est_refusee():
    """Le cas de l'attaquant qui connaît juste l'URL du tunnel."""
    with _client(twilio_validate_signature=True) as client:
        response = client.post(CHEMIN, data=CORPS)

    assert response.status_code == 403


def test_signature_fausse_est_refusee():
    with _client(twilio_validate_signature=True) as client:
        response = client.post(CHEMIN, data=CORPS, headers={SIGNATURE_HEADER: "ZmF1c3NlPQ=="})

    assert response.status_code == 403


def test_une_signature_signee_avec_un_autre_token_est_refusee():
    """Signature bien formée, mais pas de notre compte : le token est le secret."""
    signature = _signature(CORPS, token="le_token_de_quelqu_un_d_autre")

    with _client(twilio_validate_signature=True) as client:
        response = client.post(CHEMIN, data=CORPS, headers={SIGNATURE_HEADER: signature})

    assert response.status_code == 403


def test_le_corps_du_formulaire_participe_a_la_signature():
    """Sinon une signature interceptée servirait à rejouer n'importe quel corps.

    C'est le scénario qui compte : réutiliser la signature d'un `completed`
    anodin pour faire libérer un AUTRE appel, celui qui est en cours.
    """
    signature = _signature(CORPS)
    corps_modifie = CORPS | {"CallSid": "CA_un_autre_appel"}

    with _client(twilio_validate_signature=True) as client:
        response = client.post(CHEMIN, data=corps_modifie, headers={SIGNATURE_HEADER: signature})

    assert response.status_code == 403


def test_une_signature_valable_pour_une_autre_route_est_refusee():
    """L'URL est dans la signature : elle n'est pas transposable d'une route à l'autre."""
    signature = _signature(CORPS, chemin="/twilio/conference/status")

    with _client(twilio_validate_signature=True) as client:
        response = client.post(CHEMIN, data=CORPS, headers={SIGNATURE_HEADER: signature})

    assert response.status_code == 403


def test_route_en_get_sans_corps_est_validee_aussi():
    """Twilio rappelle `/voice/hold-music` en GET : la garde couvre tout le router."""
    signature = _signature({}, chemin="/twilio/voice/hold-music")

    with _client(twilio_validate_signature=True) as client:
        assert client.get("/twilio/voice/hold-music").status_code == 403
        assert (
            client.get(
                "/twilio/voice/hold-music", headers={SIGNATURE_HEADER: signature}
            ).status_code
            == 200
        )


def test_sans_token_la_garde_refuse_au_lieu_de_laisser_passer():
    """Réglage allumé et `TWILIO_AUTH_TOKEN` vide : fermé, pas silencieux.

    Avec un token vide, `RequestValidator` signerait avec une clé vide : une
    signature « valide » serait calculable par n'importe qui. Mais le serveur
    doit quand même démarrer — l'erreur est dans le log et le 403, pas au boot.
    """
    with _client(twilio_validate_signature=True, twilio_auth_token="") as client:
        sans_en_tete = client.post(CHEMIN, data=CORPS)
        signee_a_vide = client.post(
            CHEMIN, data=CORPS, headers={SIGNATURE_HEADER: _signature(CORPS, token="")}
        )

    assert sans_en_tete.status_code == 403
    assert signee_a_vide.status_code == 403


@pytest.mark.parametrize(
    "hostname",
    ["autre.ngrok-free.app", ""],
)
def test_un_public_hostname_qui_ne_correspond_pas_met_tout_en_403(hostname):
    """Le piège documenté, prouvé : la garde valide l'URL PUBLIQUE reconstruite.

    `PUBLIC_HOSTNAME` faux (ou vide) et c'est 403 sur une requête parfaitement
    légitime de Twilio. Voilà pourquoi le réglage est éteint par défaut, et
    pourquoi le log de refus nomme `PUBLIC_HOSTNAME`.
    """
    with _client(twilio_validate_signature=True, public_hostname=hostname) as client:
        response = client.post(CHEMIN, data=CORPS, headers={SIGNATURE_HEADER: _signature(CORPS)})

    assert response.status_code == 403
