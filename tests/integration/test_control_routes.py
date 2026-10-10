"""Le panneau de contrôle humain : `POST /control/calls/{id}/resume`.

Ce que ces tests protègent n'est pas la plomberie HTTP mais deux décisions
produit, qui se « corrigent » facilement par erreur :

- un échec Twilio reste un 200 avec `ok: false` (le dashboard a besoin de la
  nuance entre « appel terminé » et « Twilio a refusé ») ;
- reprendre l'appel ne réarme PAS `acted_level`, donc le score peut rester à 90
  sans remettre le scammeur en attente. La décision humaine l'emporte.

Même montage que `test_pipeline_smoke.py` : app complète sur les doublures de
`infrastructure/fakes/`, donc aucun réseau, aucun téléphone, aucun credential.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from vocalguard.config import Settings
from vocalguard.domain.alerts import AlertLevel
from vocalguard.domain.events import EventType
from vocalguard.infrastructure.fakes.call_controller import LoggingCallController
from vocalguard.infrastructure.fakes.event_sink import InMemoryEventSink
from vocalguard.infrastructure.fakes.transcriber import FakeTranscriber
from vocalguard.interface.app import create_app

CALL_ID = "CA_control_01"


class RefusingCallController(LoggingCallController):
    """Twilio qui refuse la reprise : participant parti, conférence finie, 4xx.

    On hérite de la doublure plutôt que de la réécrire, pour que `calls` reste
    inspectable — on veut prouver que la route a bien *tenté* l'action.
    """

    async def resume_caller(self, call_id: str) -> bool:
        self.calls.append(("resume_caller", call_id))
        return False


@pytest.fixture
def harness():
    """Une app complète câblée sur des doublures, et de quoi l'inspecter."""
    settings = Settings(
        _env_file=None,
        transcribe_backend="fake",
        public_hostname="demo.ngrok-free.app",
        victim_phone_number="+33600000000",
        trusted_contact_phone_number="+33611111111",
        log_level="WARNING",
    )
    sink = InMemoryEventSink()
    app = create_app(settings, sink=sink, transcriber_factory=FakeTranscriber)
    with TestClient(app) as client:
        yield client, app, sink


def _ouvrir_appel_en_attente(client: TestClient, app):
    """Un appel déjà mis en attente par l'intervention automatique.

    On passe par le webhook inbound pour partir du même état que la production,
    puis on force `caller_held` / `acted_level` : rejouer un vrai score ici
    mélangerait ce test avec celui du pipeline de score.
    """
    client.post("/twilio/voice/inbound", data={"CallSid": CALL_ID, "From": "+1555", "To": "+1555"})
    session = app.state.registry.get(CALL_ID)
    session.caller_held = True
    session.acted_level = AlertLevel.INTERVENE
    return session


def test_reprise_rend_la_parole_a_l_appelant(harness):
    client, app, sink = harness
    session = _ouvrir_appel_en_attente(client, app)

    response = client.post(f"/control/calls/{CALL_ID}/resume")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "call_id": CALL_ID, "caller_held": False}
    assert session.caller_held is False
    assert ("resume_caller", CALL_ID) in app.state.controller.calls

    # La frise du dashboard doit garder la trace de la décision humaine, dans la
    # même forme que les actions automatiques : le contrat d'événement est figé.
    actions = sink.by_type(EventType.ACTION)
    assert [e.data for e in actions] == [
        {"action": "resume_caller", "target": "caller", "ok": True}
    ]
    assert actions[0].call_id == CALL_ID


def test_appel_inconnu_repond_404_et_n_emet_rien(harness):
    client, app, sink = harness

    response = client.post("/control/calls/CA_jamais_vu/resume")

    # 404 et pas 200/ok:false : le dashboard doit distinguer « appel terminé »
    # de « Twilio a refusé ».
    assert response.status_code == 404
    assert "CA_jamais_vu" in response.json()["detail"]
    assert sink.by_type(EventType.ACTION) == []
    assert app.state.controller.calls == [], "pas d'appel Twilio sur un appel inexistant"


def test_echec_twilio_reste_un_200_avec_ok_false(harness):
    client, app, sink = harness
    session = _ouvrir_appel_en_attente(client, app)
    # Même point de câblage que `create_app` : la route lit `app.state.controller`.
    app.state.controller = RefusingCallController()

    response = client.post(f"/control/calls/{CALL_ID}/resume")

    # Une 5xx ne dirait rien d'actionnable à l'opérateur ; `ok: false` lui dit
    # que le clic est parti et que c'est Twilio qui a refusé.
    assert response.status_code == 200
    assert response.json() == {"ok": False, "call_id": CALL_ID, "caller_held": True}
    # L'état ne suit pas une action ratée : le scammeur écoute encore la musique.
    assert session.caller_held is True

    actions = sink.by_type(EventType.ACTION)
    assert [e.data["ok"] for e in actions] == [False]
    assert actions[0].data["action"] == "resume_caller"


def test_la_reprise_ne_reinitialise_pas_acted_level(harness):
    """Verrouille la décision de conception, pour que personne ne la « corrige ».

    `InterventionRunner.handle_score` n'agit que si le niveau dépasse
    `acted_level` : laisser celui-ci à INTERVENE est ce qui empêche le prochain
    score à 90 de remettre immédiatement le scammeur en attente. La décision
    humaine l'emporte sur le score.
    """
    client, app, _ = harness
    session = _ouvrir_appel_en_attente(client, app)

    client.post(f"/control/calls/{CALL_ID}/resume")

    assert session.acted_level is AlertLevel.INTERVENE


def test_deux_reprises_de_suite_sont_sans_danger(harness):
    client, app, sink = harness
    session = _ouvrir_appel_en_attente(client, app)

    premiere = client.post(f"/control/calls/{CALL_ID}/resume")
    seconde = client.post(f"/control/calls/{CALL_ID}/resume")

    # Un opérateur qui double-clique ne doit pas obtenir d'erreur : l'appel est
    # déjà rendu, la seconde reprise est juste redondante.
    assert premiere.status_code == seconde.status_code == 200
    assert seconde.json() == {"ok": True, "call_id": CALL_ID, "caller_held": False}
    assert session.caller_held is False
    assert len(sink.by_type(EventType.ACTION)) == 2
