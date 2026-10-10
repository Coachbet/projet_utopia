"""`POST /control/calls/{id}/verify` : le déclencheur principal du Lot 2.

C'est le bouton « Vérifier son identité » de l'app de la personne protégée, et le
seul déclencheur sur lequel on puisse compter : il ne dépend d'aucun flux audio,
donc il marche sur un appel de CONFIANCE — exactement le cas du scénario
d'usurpation, où l'appel est passé par la liste blanche et n'est pas analysé.

Ce que ces tests protègent, en plus de la plomberie HTTP :

- la réponse renvoie le code (l'app l'affiche en grand) alors que le bus
  d'événements, lui, ne le porte jamais ;
- 404 sur un appel inconnu, et JAMAIS de 500 : un service absent ou une panne
  restent un 200 avec `ok: false`, sinon l'app affiche une erreur serveur à une
  personne de 80 ans ;
- la forme de la réponse ne bouge pas d'un cas à l'autre : toujours les six mêmes
  clés, parce que l'app les lit sans rien tester.

Même montage que `test_control_routes.py` : app complète sur les doublures de
`infrastructure/fakes/`, donc aucun réseau, aucun téléphone, aucun credential.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from vocalguard.config import Settings
from vocalguard.domain.events import EventType
from vocalguard.domain.models import ContentSignal
from vocalguard.infrastructure.fakes.event_sink import InMemoryEventSink
from vocalguard.infrastructure.fakes.transcriber import FakeTranscriber
from vocalguard.interface.app import create_app

CALL_ID = "CA_verify_route_01"
LUCAS = "+33672019048"
INCONNU = "+33612000000"

CARNET = {
    "owner": "Jeanne",
    "contacts": [
        {
            "name": "Lucas Martin",
            "phone_number": LUCAS,
            "relation": "petit-fils",
            "aliases": ["Lucas"],
        }
    ],
}


@pytest.fixture
def harness(tmp_path):
    """Une app complète, et un carnet à nous.

    Le carnet est écrit dans `tmp_path` et pas lu depuis `data/contacts.json` :
    ce fichier est gitignoré (c'est le carnet réel de la démo), donc un test qui
    en dépendrait passerait sur cette machine et nulle part ailleurs.
    """
    chemin = tmp_path / "contacts.json"
    chemin.write_text(json.dumps(CARNET), encoding="utf-8")

    settings = Settings(
        _env_file=None,
        app_pin="432198",
        transcribe_backend="fake",
        public_hostname="demo.ngrok-free.app",
        victim_phone_number="+33600000000",
        trusted_contact_phone_number=LUCAS,
        contacts_path=chemin,
        log_level="WARNING",
    )
    sink = InMemoryEventSink()
    app = create_app(settings, sink=sink, transcriber_factory=FakeTranscriber)
    with TestClient(app, headers={"X-App-Pin": "432198"}) as client:
        yield client, app, sink


async def _ouvrir_appel(app, *, caller: str = LUCAS, transcribe: bool = False, claimed=None):
    """Un appel en cours, ouvert par le registre.

    Directement par `registry.open` et pas par `/twilio/voice/inbound` : le
    routage liste blanche du Lot 1 décide lui-même du `transcribe` et du TwiML,
    et ces tests-là ne portent pas sur lui. `transcribe=False` est le cas qui
    compte ici — un appel de confiance, sans un octet d'audio, pour lequel ce
    bouton est le SEUL déclencheur possible.
    """
    session = await app.state.registry.open(CALL_ID, caller_number=caller, transcribe=transcribe)
    if claimed is not None:
        session.signals.content = ContentSignal(risk=0.8, claimed_identity=claimed)
    return session


def _sms(app) -> list[tuple[str, ...]]:
    return [c for c in app.state.controller.calls if c[0] == "send_sms"]


async def test_le_bouton_envoie_un_code_au_vrai_telephone_du_proche(harness) -> None:
    """Le scénario du pitch : l'appel se présente avec le numéro de Lucas.

    C'est ce qu'un usurpateur sait faire. Le code part au numéro du CARNET — le
    même, et c'est voulu : usurper un numéro d'appel ne donne pas la réception de
    ses SMS, donc le message arrive sur la SIM du vrai Lucas, qui n'est pas en
    ligne.
    """
    client, app, sink = harness
    await _ouvrir_appel(app)

    reponse = client.post(f"/control/calls/{CALL_ID}/verify")

    assert reponse.status_code == 200
    corps = reponse.json()
    assert set(corps) == {"ok", "call_id", "code", "sent_to", "expires_in", "reason"}
    assert corps["ok"] is True
    assert corps["call_id"] == CALL_ID
    assert corps["sent_to"] == "Lucas Martin"
    assert corps["expires_in"] == 120
    assert corps["reason"] is None
    assert len(corps["code"]) == 4 and corps["code"].isdigit()

    # Un SMS, vers le numéro du carnet, et une annonce pour la personne protégée
    # seule — jamais un `<Say>` dans la conférence, que l'appelant entendrait.
    assert len(_sms(app)) == 1
    assert _sms(app)[0][1] == LUCAS
    assert corps["code"] in _sms(app)[0][2]
    annonces = [c for c in app.state.controller.calls if c[0] == "announce_to_victim"]
    assert len(annonces) == 1

    # Le bus porte le fait, jamais le code : il part au dashboard et à l'app par
    # un WebSocket sans authentification.
    actions = [e.data for e in sink.by_type(EventType.ACTION)]
    assert actions == [
        {
            "action": "verify_requested",
            "target": "trusted_contact",
            "contact": "Lucas Martin",
            "source": "app",
            "ok": True,
        }
    ]
    assert corps["code"] not in json.dumps([e.to_wire() for e in sink.events])


async def test_un_numero_inconnu_avec_une_identite_revendiquee(harness) -> None:
    client, app, _ = harness
    await _ouvrir_appel(app, caller=INCONNU, transcribe=True, claimed="Lucas, votre petit-fils")

    corps = client.post(f"/control/calls/{CALL_ID}/verify").json()

    assert corps["ok"] is True
    assert _sms(app)[0][1] == LUCAS, "le code part chez le vrai Lucas, pas chez l'appelant"


async def test_un_inconnu_hors_carnet_ne_declenche_aucun_sms(harness) -> None:
    client, app, sink = harness
    await _ouvrir_appel(app, caller=INCONNU, transcribe=True)

    reponse = client.post(f"/control/calls/{CALL_ID}/verify")

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["ok"] is False
    assert corps["reason"] == "contact_inconnu"
    assert corps["code"] is None
    assert _sms(app) == []
    assert sink.by_type(EventType.ACTION) == []


def test_un_appel_inconnu_repond_404_et_n_emet_rien(harness) -> None:
    client, app, sink = harness

    reponse = client.post("/control/calls/CA_jamais_vu/verify")

    # 404 et pas 200/ok:false : le bouton doit disparaître, pas clignoter.
    assert reponse.status_code == 404
    assert "CA_jamais_vu" in reponse.json()["detail"]
    assert sink.by_type(EventType.ACTION) == []
    assert app.state.controller.calls == []


async def test_deux_clics_ne_font_qu_un_seul_code(harness) -> None:
    """L'opérateur, ou la personne protégée, appuie deux fois.

    Un second SMS poserait au proche une question qu'il a déjà sous les yeux, et
    un second code dans son oreille rendrait la vérification impossible.
    """
    client, app, sink = harness
    await _ouvrir_appel(app)

    premier = client.post(f"/control/calls/{CALL_ID}/verify").json()
    second = client.post(f"/control/calls/{CALL_ID}/verify").json()

    assert second["ok"] is True
    assert second["code"] == premier["code"]
    assert second["reason"] == "deja_actif"
    assert second["expires_in"] <= 120
    assert len(_sms(app)) == 1
    assert len(sink.by_type(EventType.ACTION)) == 1


async def test_un_sms_refuse_par_twilio_reste_un_200(harness) -> None:
    client, app, _ = harness
    await _ouvrir_appel(app)

    async def refuse(to_number: str, message: str) -> bool:
        return False

    app.state.controller.send_sms = refuse

    reponse = client.post(f"/control/calls/{CALL_ID}/verify")

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["ok"] is False
    assert corps["reason"] == "sms_refuse"
    assert corps["code"] is None, "un code que le proche n'a pas reçu n'existe pas"


async def test_un_service_absent_ne_fait_pas_croire_au_succes(harness) -> None:
    """Garde défensive, celle de `contact-denied` : un bouton qui n'a rien
    déclenché ne doit pas répondre `ok: true`."""
    client, app, _ = harness
    await _ouvrir_appel(app)
    app.state.verifier = None

    reponse = client.post(f"/control/calls/{CALL_ID}/verify")

    assert reponse.status_code == 200
    assert reponse.json()["ok"] is False
    assert reponse.json()["reason"] == "verification_indisponible"


async def test_un_service_qui_leve_ne_devient_pas_une_500(harness) -> None:
    client, app, _ = harness
    await _ouvrir_appel(app)

    class ServiceEnPanne:
        async def request(self, call_id: str, *, source: str = "app"):
            raise RuntimeError("Twilio injoignable")

    app.state.verifier = ServiceEnPanne()

    reponse = client.post(f"/control/calls/{CALL_ID}/verify")

    assert reponse.status_code == 200
    assert reponse.json() == {
        "ok": False,
        "call_id": CALL_ID,
        "code": None,
        "sent_to": None,
        "expires_in": 0,
        "reason": "erreur_interne",
    }


async def test_la_fin_de_l_appel_purge_la_verification(harness) -> None:
    """`lifecycle.release_call` est câblé sur le service comme sur le pipeline.

    Sans ça, le minuteur d'expiration se réveillerait après le raccrochage pour
    annoncer quelque chose dans une conférence qui n'existe plus.
    """
    client, app, _ = harness
    await _ouvrir_appel(app)
    client.post(f"/control/calls/{CALL_ID}/verify")

    client.post("/twilio/voice/status", data={"CallSid": CALL_ID, "CallStatus": "completed"})

    assert app.state.registry.get(CALL_ID) is None
    assert app.state.verifier._pending == {}
    # L'appel n'existe plus : le bouton doit disparaître, pas réessayer.
    assert client.post(f"/control/calls/{CALL_ID}/verify").status_code == 404
