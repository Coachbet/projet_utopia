"""Le panneau de contrôle humain : `POST /control/calls/{id}/resume` et `/hold`.

Ce que ces tests protègent n'est pas la plomberie HTTP mais trois décisions
produit, qui se « corrigent » facilement par erreur :

- un échec Twilio reste un 200 avec `ok: false` (le dashboard a besoin de la
  nuance entre « appel terminé » et « Twilio a refusé ») ;
- aucune des deux routes ne réarme `acted_level`, donc le score peut rester à 90
  sans remettre le scammeur en attente. La décision humaine l'emporte, et
  `/hold` est la seule façon de revenir en arrière ;
- une action sans effet (reprendre un appel jamais tenu, tenir un appel déjà
  tenu) répond 200 mais n'appelle pas Twilio et n'émet AUCUN événement : la
  frise du dashboard ne doit pas afficher une reprise qui n'a rien repris.

Même montage que `test_pipeline_smoke.py` : app complète sur les doublures de
`infrastructure/fakes/`, donc aucun réseau, aucun téléphone, aucun credential.

Les assertions passent par `sink.actions()` et non `sink.by_type(ACTION)` : depuis
le routage liste blanche, `/twilio/voice/inbound` — par lequel ces tests ouvrent
leur appel, pour partir du même état que la production — publie son propre fait
`caller_notice` dans le même type d'événement, le contrat figé à H0 ne permettant
rien d'autre. Ce qu'on mesure ici est la décision de l'opérateur, pas ce que le
routage a constaté. Cf. `domain.alerts.ReportedAction`.
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
    """Twilio qui refuse l'action : participant parti, conférence finie, 4xx.

    On hérite de la doublure plutôt que de la réécrire, pour que `calls` reste
    inspectable — on veut prouver que la route a bien *tenté* l'action.
    """

    async def resume_caller(self, call_id: str) -> bool:
        self.calls.append(("resume_caller", call_id))
        return False

    async def hold_caller(self, call_id: str) -> bool:
        self.calls.append(("hold_caller", call_id))
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


def _ouvrir_appel(client: TestClient, app):
    """Un appel en cours, aucune intervention jouée : `caller_held` est `False`.

    On passe par le webhook inbound pour partir du même état que la production.
    """
    client.post("/twilio/voice/inbound", data={"CallSid": CALL_ID, "From": "+1555", "To": "+1555"})
    return app.state.registry.get(CALL_ID)


def _ouvrir_appel_en_attente(client: TestClient, app):
    """Un appel déjà mis en attente par l'intervention automatique.

    On force `caller_held` / `acted_level` plutôt que de rejouer un vrai score :
    ça mélangerait ces tests avec ceux du pipeline de score.
    """
    session = _ouvrir_appel(client, app)
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
    actions = sink.actions()
    assert [e.data for e in actions] == [
        {"action": "resume_caller", "target": "caller", "ok": True}
    ]
    assert actions[0].call_id == CALL_ID


@pytest.mark.parametrize("route", ["resume", "hold"])
def test_appel_inconnu_repond_404_et_n_emet_rien(harness, route):
    client, app, sink = harness

    response = client.post(f"/control/calls/CA_jamais_vu/{route}")

    # 404 et pas 200/ok:false : le dashboard doit distinguer « appel terminé »
    # de « Twilio a refusé ».
    assert response.status_code == 404
    assert "CA_jamais_vu" in response.json()["detail"]
    assert sink.actions() == []
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

    actions = sink.actions()
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
    # Un seul événement, pas deux : le second clic n'a rien changé à l'appel, et
    # deux points de frise pour une seule décision mentiraient à l'opérateur.
    assert len(sink.actions()) == 1


# --- `POST /control/calls/{id}/hold` : le retour arrière ---------------------
#
# `AlertLevel.INTERVENE` est le niveau maximum et `handle_score` sort dès que
# `assessment.level <= session.acted_level` : après une reprise, aucun score —
# pas même 100 — ne peut remettre le scammeur en attente. Cette route est donc la
# SEULE protection restante après un clic de trop sur `resume`.


def test_la_mise_en_attente_manuelle_coupe_la_parole_a_l_appelant(harness):
    client, app, sink = harness
    session = _ouvrir_appel(client, app)

    response = client.post(f"/control/calls/{CALL_ID}/hold")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "call_id": CALL_ID, "caller_held": True}
    assert session.caller_held is True
    assert ("hold_caller", CALL_ID) in app.state.controller.calls

    # Même forme d'événement que l'action automatique d'`InterventionRunner` :
    # le contrat est figé, le dashboard ne distingue pas les deux origines.
    actions = sink.actions()
    assert [e.data for e in actions] == [{"action": "hold_caller", "target": "caller", "ok": True}]
    assert actions[0].call_id == CALL_ID


def test_un_echec_twilio_sur_la_mise_en_attente_reste_un_200(harness):
    client, app, sink = harness
    session = _ouvrir_appel(client, app)
    app.state.controller = RefusingCallController()

    response = client.post(f"/control/calls/{CALL_ID}/hold")

    assert response.status_code == 200
    assert response.json() == {"ok": False, "call_id": CALL_ID, "caller_held": False}
    # L'état ne suit pas une action ratée : l'appelant parle encore.
    assert session.caller_held is False
    assert ("hold_caller", CALL_ID) in app.state.controller.calls

    actions = sink.actions()
    assert [e.data for e in actions] == [{"action": "hold_caller", "target": "caller", "ok": False}]


def test_la_mise_en_attente_manuelle_ne_touche_pas_acted_level(harness):
    """Symétrique de la garde sur `resume`, et pour la même raison.

    Ces deux routes sont des surcharges humaines : elles agissent sur l'appel,
    pas sur la machine à états du score. Réarmer `acted_level` ici rendrait le
    prochain score capable de reprendre l'appel que l'opérateur vient de mettre
    en attente.
    """
    client, app, _ = harness
    session = _ouvrir_appel(client, app)
    session.acted_level = AlertLevel.ALERT

    client.post(f"/control/calls/{CALL_ID}/hold")

    assert session.acted_level is AlertLevel.ALERT


def test_hold_rend_la_protection_apres_une_reprise_definitive(harness):
    """Le scénario qui justifie la route : reprise par erreur, puis retour arrière.

    `acted_level` reste à INTERVENE après la reprise, donc le score ne protégera
    plus jamais cet appel. C'est l'opérateur, et lui seul, qui peut remettre le
    scammeur en attente.
    """
    client, app, sink = harness
    session = _ouvrir_appel_en_attente(client, app)

    client.post(f"/control/calls/{CALL_ID}/resume")
    assert session.caller_held is False

    client.post(f"/control/calls/{CALL_ID}/hold")

    assert session.caller_held is True
    assert session.acted_level is AlertLevel.INTERVENE
    assert [e.data["action"] for e in sink.actions()] == [
        "resume_caller",
        "hold_caller",
    ]


# --- Une action sans effet n'est pas un événement ---------------------------


def test_reprendre_un_appel_jamais_mis_en_attente_n_appelle_pas_twilio(harness):
    """La frise affichait une reprise qui n'avait rien repris.

    L'état demandé est déjà atteint : c'est un succès, donc 200 et `ok: true` —
    la forme de la réponse ne bouge pas, le dashboard n'a pas à distinguer
    « fait » de « déjà fait ». Mais rien n'a changé sur l'appel, donc rien ne
    part vers Twilio et rien ne part sur le bus.
    """
    client, app, sink = harness
    session = _ouvrir_appel(client, app)

    response = client.post(f"/control/calls/{CALL_ID}/resume")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "call_id": CALL_ID, "caller_held": False}
    assert session.caller_held is False
    assert app.state.controller.calls == [], "pas d'appel Twilio pour un état déjà atteint"
    assert sink.actions() == []


def test_mettre_en_attente_un_appel_deja_tenu_n_appelle_pas_twilio(harness):
    """Le symétrique exact du précédent : même raisonnement, même réponse."""
    client, app, sink = harness
    session = _ouvrir_appel_en_attente(client, app)

    response = client.post(f"/control/calls/{CALL_ID}/hold")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "call_id": CALL_ID, "caller_held": True}
    assert session.caller_held is True
    assert app.state.controller.calls == []
    assert sink.actions() == []


# --- Le démenti du proche, saisi à la main -----------------------------------
# Ces tests couvrent le chemin de secours du coupe-circuit
# `trusted_contact_denied`. Son unique autre déclencheur est un SMS d'un mobile
# français vers un long code américain : si la délivrabilité ne suit pas, c'est
# cette route qui porte le signal le plus décisif du système.


def test_le_dementi_du_proche_fait_monter_le_score_a_l_intervention(harness):
    """Le test qui compte : la route ne se contente pas de répondre 200, elle
    doit réellement armer le coupe-circuit et déclencher l'intervention."""
    client, app, sink = harness
    session = _ouvrir_appel(client, app)

    reponse = client.post(f"/control/calls/{CALL_ID}/contact-denied")

    assert reponse.status_code == 200
    assert reponse.json() == {"ok": True, "call_id": CALL_ID}

    scores = [e.data for e in sink.by_type(EventType.SCORE)]
    assert scores, "aucun score republié : le signal n'a pas atteint le pipeline"
    assert scores[-1]["score"] >= 85
    assert scores[-1]["level"] == "intervene"
    assert "trusted_contact_denied" in scores[-1]["breakers_fired"]

    # Et l'intervention a bien suivi : l'appelant est en attente.
    actions = [e.data["action"] for e in sink.actions()]
    assert "hold_caller" in actions
    assert session.caller_held is True


def test_un_dementi_sur_un_appel_inconnu_repond_404(harness):
    client, app, sink = harness
    _ouvrir_appel(client, app)

    reponse = client.post("/control/calls/CA_inexistant/contact-denied")

    assert reponse.status_code == 404
    assert "CA_inexistant" in reponse.json()["detail"]
    assert not [e for e in sink.by_type(EventType.SCORE)], "aucun score ne devait être publié"


def test_deux_dementis_de_suite_sont_sans_danger(harness):
    """Double clic de l'opérateur. `contact_replied` est idempotente côté
    pipeline : le second passage ne doit pas republier de score ni rejouer
    l'annonce dans l'oreille de la victime."""
    client, app, sink = harness
    _ouvrir_appel(client, app)

    premier = client.post(f"/control/calls/{CALL_ID}/contact-denied")
    scores_apres_un = len(sink.by_type(EventType.SCORE))
    second = client.post(f"/control/calls/{CALL_ID}/contact-denied")

    assert premier.status_code == second.status_code == 200
    assert second.json() == {"ok": True, "call_id": CALL_ID}
    assert len(sink.by_type(EventType.SCORE)) == scores_apres_un


def test_un_pipeline_sans_contact_replied_renvoie_ok_false(harness):
    """Garde défensive : un pipeline amputé ne doit pas faire croire au succès.

    C'est exactement l'état qu'avait le projet avant que Dev 2 n'écrive la
    méthode — le signal était reçu et ne pesait sur rien.
    """
    client, app, _ = harness
    _ouvrir_appel(client, app)

    class PipelineAmpute:
        """Tout sauf `contact_replied`."""

        async def emit(self, event):
            return None

    app.state.pipeline = PipelineAmpute()

    reponse = client.post(f"/control/calls/{CALL_ID}/contact-denied")

    assert reponse.status_code == 200
    assert reponse.json() == {"ok": False, "call_id": CALL_ID}


def test_un_pipeline_qui_leve_ne_devient_pas_une_500(harness):
    client, app, _ = harness
    _ouvrir_appel(client, app)

    class PipelineQuiLeve:
        async def contact_replied(self, call_id, *, denied):
            raise RuntimeError("Bedrock injoignable")

    app.state.pipeline = PipelineQuiLeve()

    reponse = client.post(f"/control/calls/{CALL_ID}/contact-denied")

    assert reponse.status_code == 200
    assert reponse.json() == {"ok": False, "call_id": CALL_ID}
