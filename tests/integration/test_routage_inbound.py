"""Le routage de `/twilio/voice/inbound` : transmettre, analyser, ou refuser.

C'est la décision la plus visible du produit, et celle dont une régression ne se
verrait nulle part ailleurs. La ligne protégée étant renvoyée vers notre numéro
Twilio, TOUS les appels passent par cette route — y compris ceux de la famille.
Quatre cas, quatre risques très différents :

- **confiance** : si un `<Stream>` réapparaissait, on écouterait la famille. La
  promesse du produit serait rompue et rien ne le signalerait ;
- **inconnu** : si l'annonce passait APRÈS le `<Stream>`, on capterait de l'audio
  avant d'avoir informé l'appelant. C'est le montage juridique entier qui tombe ;
- **masqué** : s'il passait pour un contact, on accorderait le silence à
  quelqu'un qui cache son numéro — l'erreur grave de ce routage ;
- **boucle** : si on rappelait la victime, le renvoi d'appel nous ferait
  rappeler nous-mêmes, à deux jambes facturées par tour jusqu'à épuisement du
  crédit Twilio.

Même montage que `test_control_routes.py` : app complète sur les doublures de
`infrastructure/fakes/`, donc aucun réseau, aucun téléphone, aucun credential.
Le carnet d'adresses est écrit dans un fichier jetable (`tmp_path`) : les tests
ne doivent dépendre ni de `data/contacts.json` (gitignoré, donc absent en CI) ni
des vrais numéros de la démo.
"""

from __future__ import annotations

import json
import time
from contextlib import ExitStack
from urllib.parse import urlencode
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient

from vocalguard.config import Settings
from vocalguard.domain.events import EventType
from vocalguard.infrastructure.fakes.call_controller import LoggingCallController
from vocalguard.infrastructure.fakes.event_sink import InMemoryEventSink
from vocalguard.infrastructure.fakes.transcriber import FakeTranscriber
from vocalguard.interface.app import create_app

CALL_ID = "CA_routage_01"

TWILIO = "+16202648948"
"""Notre relais. Dans le sens sortant (script de démo), c'est lui le `From`."""
PROTEGEE = "+33611111111"
VICTIME = "+33600000000"
LUCAS = "+33672019048"
BANQUE = "+33969321234"
ARNAQUEUR = "+33755555555"
INCONNU = "+33799999999"

CARNET = {
    "owner": "Jeanne",
    "contacts": [
        {
            "name": "Lucas Martin",
            "phone_number": LUCAS,
            "relation": "petit-fils",
            "aliases": ["Lucas"],
        },
        # Au carnet mais analysé : on sait la nommer à l'écran sans lui accorder
        # le silence. C'est le cas qui distingue « connu » de « de confiance ».
        {
            "name": "Banque Crédit Mutuel",
            "phone_number": BANQUE,
            "relation": "banque",
            "mode": "analyze",
        },
    ],
}


class ControleurQuiRappelle(LoggingCallController):
    """Doublure capable de rappeler la victime.

    `LoggingCallController` n'expose pas `dial_victim` — ce n'est pas dans le port
    `CallController`, seul l'adapter Twilio sait passer un appel. Sans cette
    sous-classe, « la victime n'a pas été rappelée » serait indistinguable de
    « la doublure ne sait pas rappeler », et c'est exactement la nuance que le cas
    boucle doit prouver.
    """

    async def dial_victim(self, call_id: str) -> str:
        self.calls.append(("dial_victim", call_id))
        return f"CA_victime_{call_id}"


@pytest.fixture
def monter_app(tmp_path):
    """Fabrique une app complète câblée sur des doublures, et de quoi l'inspecter.

    Une fabrique plutôt qu'une app toute faite : la moitié de ces tests porte sur
    un RÉGLAGE (`TRUSTED_VICTIM_ONLY_ANALYSIS`, `DEMO_SPOOF_AS`, un numéro saisi
    à la main), et `Settings` est figé à la construction.
    """
    pile = ExitStack()

    def _monter(*, carnet: dict | None = None, **reglages):
        chemin = tmp_path / "contacts.json"
        chemin.write_text(
            json.dumps(CARNET if carnet is None else carnet, ensure_ascii=False),
            encoding="utf-8",
        )
        # Dictionnaire fusionné et non `**reglages` en fin d'appel : un test doit
        # pouvoir REMPLACER un numéro de la base (le cas du numéro saisi à la
        # main), pas seulement ajouter des réglages.
        defauts = {
            "transcribe_backend": "fake",
            "public_hostname": "demo.ngrok-free.app",
            "twilio_phone_number": TWILIO,
            "protected_phone_number": PROTEGEE,
            "victim_phone_number": VICTIME,
            "trusted_contact_phone_number": LUCAS,
            "contacts_path": chemin,
            "log_level": "WARNING",
        }
        settings = Settings(_env_file=None, **{**defauts, **reglages})
        sink = InMemoryEventSink()
        app = create_app(settings, sink=sink, transcriber_factory=FakeTranscriber)
        # Remplacé après `create_app`, comme le fait `test_control_routes.py` :
        # la route lit `app.state.controller` à chaque requête.
        app.state.controller = ControleurQuiRappelle()
        client = pile.enter_context(TestClient(app))
        return client, app, sink

    yield _monter
    pile.close()


def _appeler(
    client: TestClient,
    *,
    depuis: str,
    params: dict[str, str] | None = None,
    call_sid: str = CALL_ID,
):
    """Pose le webhook entrant comme Twilio le pose, et rend la racine du TwiML."""
    requete = f"?{urlencode(params)}" if params else ""
    reponse = client.post(
        f"/twilio/voice/inbound{requete}",
        data={"CallSid": call_sid, "From": depuis, "To": TWILIO},
    )
    assert reponse.status_code == 200
    return ElementTree.fromstring(reponse.text)


def _actions(sink: InMemoryEventSink) -> list[dict]:
    return [event.data for event in sink.by_type(EventType.ACTION)]


def _rappels_victime(app, *, attendu: bool) -> list[tuple[str, ...]]:
    """Les `dial_victim` partis, après avoir laissé la boucle d'événements tourner.

    L'appel de la victime part en tâche de fond (Twilio attend le TwiML), donc il
    n'est pas forcément exécuté quand la réponse HTTP revient. Sans cette
    attente, « pas encore parti » se lirait comme « jamais parti » et le test
    passerait pour la mauvaise raison — y compris le jour où la garde anti-boucle
    cesserait de garder quoi que ce soit.
    """
    fin = time.monotonic() + 1.0
    while attendu and time.monotonic() < fin:
        if any(appel[0] == "dial_victim" for appel in app.state.controller.calls):
            break
        time.sleep(0.01)
    if not attendu:
        # Une absence ne se prouve pas par un sondage : la tâche est déjà créée
        # quand la réponse revient, on lui laisse une fenêtre fixe puis on
        # constate.
        time.sleep(0.05)
    return [appel for appel in app.state.controller.calls if appel[0] == "dial_victim"]


# --- Cas 1 : le contact de confiance ---------------------------------------


def test_un_contact_de_confiance_passe_sans_annonce_ni_analyse(monter_app):
    client, app, sink = monter_app()

    racine = _appeler(client, depuis=LUCAS)

    # Aucun flux audio : c'est la promesse produit, et elle se lit ici ou nulle part.
    assert racine.find("Start") is None
    assert racine.find(".//Stream") is None
    # Aucune annonce : l'appelant ne doit rien entendre qui lui apprenne qu'il a
    # été reconnu, ni même qu'un service de protection existe.
    assert racine.find("Say") is None
    conference = racine.find("Dial/Conference")
    assert conference is not None
    assert CALL_ID in (conference.text or "")

    # Et aucune piste Transcribe ouverte : sans ce drapeau, deux connexions AWS
    # seraient facturées pour une piste qui restera muette.
    session = app.state.registry.get(CALL_ID)
    assert session is not None, "la session existe même sans analyse (routes de contrôle)"
    assert session.transcribe is False

    assert _actions(sink) == [
        {
            "action": "trusted_passthrough",
            "target": "caller",
            "ok": True,
            "contact": "Lucas Martin",
            "message": "Appel de Lucas Martin : transmis sans annonce ni analyse.",
        }
    ]


def test_un_appel_de_confiance_rappelle_quand_meme_la_victime(monter_app):
    """« Non analysé » ne veut pas dire « non transmis » : c'est un appel de la
    famille, il doit sonner chez la personne protégée comme n'importe quel autre."""
    client, app, _ = monter_app()

    _appeler(client, depuis=LUCAS)

    assert _rappels_victime(app, attendu=True) == [("dial_victim", CALL_ID)]


# --- Cas 2 : l'inconnu ------------------------------------------------------


def test_un_inconnu_est_informe_avant_tout_flux_audio(monter_app):
    """L'ordre `Say` → `Stream` → `Dial` EST le montage juridique.

    Inverser les deux premiers verbes capterait de l'audio avant l'information de
    l'appelant, et aucun autre test du dépôt ne le verrait.
    """
    client, app, sink = monter_app()

    racine = _appeler(client, depuis=INCONNU)

    enfants = [enfant.tag for enfant in racine]
    assert enfants.index("Say") < enfants.index("Start") < enfants.index("Dial")

    say = racine.find("Say")
    assert say is not None
    assert say.get("language") == "fr-FR"
    assert say.text == app.state.settings.caller_notice_text

    stream = racine.find("Start/Stream")
    assert stream is not None
    assert stream.get("track") == "both_tracks"
    parametres = {p.get("name"): p.get("value") for p in stream.findall("Parameter")}
    assert parametres == {"caller_number": INCONNU}

    session = app.state.registry.get(CALL_ID)
    assert session is not None
    assert session.transcribe is True

    assert _actions(sink) == [
        {
            "action": "caller_notice",
            "target": "caller",
            "ok": True,
            "message": app.state.settings.caller_notice_text,
        }
    ]


@pytest.mark.parametrize("depuis", ["", "anonymous", "RESTRICTED", "Unknown"])
def test_un_appel_masque_est_analyse(monter_app, depuis):
    """L'erreur grave serait l'inverse : accorder le silence à qui cache son numéro.

    La casse varie parce que rien ne garantit celle que Twilio enverra, et un
    `Anonymous` lu comme un numéro ordinaire resterait inconnu par chance (il ne
    se normalise pas en numéro) plutôt que par décision.
    """
    client, app, sink = monter_app()

    racine = _appeler(client, depuis=depuis)

    assert racine.find("Say") is not None
    assert racine.find("Start/Stream") is not None
    assert app.state.registry.get(CALL_ID).transcribe is True
    assert [action["action"] for action in _actions(sink)] == ["caller_notice"]


def test_un_contact_en_mode_analyze_est_traite_comme_un_inconnu(monter_app):
    """Être au carnet n'ouvre pas le droit au silence : `mode: "analyze"` existe
    pour les entrées qu'on garde pour les reconnaître à l'écran (une banque, un
    démarcheur toléré) sans leur accorder la confiance."""
    client, app, sink = monter_app()

    racine = _appeler(client, depuis=BANQUE)

    assert racine.find("Say") is not None
    assert racine.find("Start/Stream") is not None
    assert app.state.registry.get(CALL_ID).transcribe is True
    assert [action["action"] for action in _actions(sink)] == ["caller_notice"]


# --- Cas 4 : la boucle de renvoi --------------------------------------------


@pytest.mark.parametrize(
    ("depuis", "role"),
    [
        (PROTEGEE, "le numéro public renvoyé vers nous"),
        (VICTIME, "le repli téléphonique"),
        (TWILIO, "notre propre relais"),
    ],
)
def test_une_boucle_de_renvoi_est_refusee_sans_rappeler_personne(monter_app, depuis, role):
    """Le cas qui coûte de l'argent, et le seul où l'on refuse d'acheminer.

    La personne protégée renvoie sa ligne vers notre numéro : rappeler un de nos
    numéros nous ferait rappeler nous-mêmes, à deux jambes facturées par tour.
    """
    client, app, sink = monter_app()

    racine = _appeler(client, depuis=depuis)

    # Une phrase, et surtout pas de `<Dial>` : c'est le `<Dial>` qui bouclerait.
    assert racine.find("Dial") is None
    assert racine.find(".//Stream") is None
    say = racine.find("Say")
    assert say is not None, f"{role} : une phrase vaut mieux qu'un raccrochage muet"

    # Aucune session, aucun rappel, aucun événement : l'appel n'a pas existé pour
    # le reste du système.
    assert app.state.registry.get(CALL_ID) is None
    assert _rappels_victime(app, attendu=False) == []
    assert _actions(sink) == []


def test_la_boucle_est_reconnue_sur_un_numero_saisi_a_la_main(monter_app):
    """Le `.env` est tapé par un humain ("06 12 34 56 78"), Twilio répond en E.164.

    Sans normalisation des deux côtés, la garde ne reconnaîtrait pas son propre
    numéro et la boucle s'amorcerait — avec un `.env` qui a pourtant l'air juste.
    """
    client, app, _ = monter_app(protected_phone_number="06 12 34 56 78")

    racine = _appeler(client, depuis="+33612345678")

    assert racine.find("Dial") is None
    assert app.state.registry.get(CALL_ID) is None


# --- L'échappatoire du script de démo ---------------------------------------


def test_l_echappatoire_du_script_de_demo_n_est_pas_bloquee(monter_app):
    """`scripts/lancer_appel_demo.py` inverse le sens de l'appel : la jambe
    SORTANTE arrive ici avec notre propre numéro en `From`. Sans l'échappatoire,
    notre propre garde anti-boucle refuserait la démo."""
    client, app, sink = monter_app()

    racine = _appeler(
        client,
        depuis=TWILIO,
        params={"demo_outbound": "1", "presented_from": ARNAQUEUR},
    )

    # Traité comme un appel entrant ordinaire d'un inconnu : annonce puis analyse.
    assert racine.find("Say") is not None
    assert racine.find("Start/Stream") is not None
    session = app.state.registry.get(CALL_ID)
    assert session is not None
    # L'identité présentée a remplacé notre relais partout en aval, sinon la
    # réputation et le carnet travailleraient sur le mauvais numéro.
    assert session.caller_number == ARNAQUEUR
    assert [action["action"] for action in _actions(sink)] == ["caller_notice"]
    assert _rappels_victime(app, attendu=True) == [("dial_victim", CALL_ID)]


def test_sans_le_parametre_de_demo_le_meme_appel_est_refuse(monter_app):
    """Le pendant du test précédent : l'échappatoire doit être un acte explicite,
    sinon la garde ne garde plus rien. Un réglage `.env` la désarmerait pour les
    vrais appels aussi."""
    client, app, _ = monter_app()

    racine = _appeler(client, depuis=TWILIO)

    assert racine.find("Dial") is None
    assert app.state.registry.get(CALL_ID) is None


def test_l_identite_presentee_est_ignoree_hors_mode_demo(monter_app):
    """`presented_from` seul ne doit rien pouvoir faire : l'URL du webhook est
    publique, et un paramètre qui réécrit le `From` sans drapeau explicite
    permettrait à n'importe qui de se faire passer pour un proche."""
    client, app, _ = monter_app()

    racine = _appeler(client, depuis=INCONNU, params={"presented_from": LUCAS})

    assert racine.find("Say") is not None, "resté un inconnu, donc annoncé"
    assert app.state.registry.get(CALL_ID).caller_number == INCONNU


# --- L'usurpation simulée (Lot 4) -------------------------------------------


def test_l_usurpation_simulee_fait_passer_l_arnaqueur_pour_un_proche(monter_app):
    """Le moment fort du pitch : Twilio interdit d'usurper un vrai numéro, donc on
    simule la seule chose qui compte — ce que notre serveur CROIT du `From`."""
    client, app, sink = monter_app(scammer_phone_number=ARNAQUEUR, demo_spoof_as=LUCAS)

    racine = _appeler(client, depuis=ARNAQUEUR)

    # Le routage a vraiment cru à Lucas : liste blanche, donc aucune analyse.
    assert racine.find(".//Stream") is None
    assert racine.find("Say") is None
    assert app.state.registry.get(CALL_ID).transcribe is False

    actions = _actions(sink)
    # L'usurpation est publiée AVANT la décision qu'elle a provoquée : le
    # dashboard doit pouvoir montrer le mensonge, pas seulement son effet.
    assert [action["action"] for action in actions] == ["spoof_simulated", "trusted_passthrough"]
    assert actions[0]["real_from"] == ARNAQUEUR
    assert actions[0]["presented_as"] == LUCAS
    assert actions[1]["contact"] == "Lucas Martin"


def test_l_usurpation_reste_inerte_sans_le_telephone_de_l_arnaqueur(monter_app):
    """`DEMO_SPOOF_AS` seul ne doit rien pouvoir faire. Un `.env` oublié après la
    démo ferait sinon passer des inconnus pour des proches — c'est-à-dire
    accorderait le silence aux appels qu'il fallait analyser."""
    client, _, sink = monter_app(demo_spoof_as=LUCAS)

    racine = _appeler(client, depuis=ARNAQUEUR)

    assert racine.find("Say") is not None
    assert racine.find("Start/Stream") is not None
    assert [action["action"] for action in _actions(sink)] == ["caller_notice"]


def test_l_usurpation_ne_touche_pas_les_autres_appelants(monter_app):
    """Seul le téléphone de l'arnaqueur est réécrit : un inconnu quelconque reste
    un inconnu, même pendant la démo."""
    client, _, sink = monter_app(scammer_phone_number=ARNAQUEUR, demo_spoof_as=LUCAS)

    racine = _appeler(client, depuis=INCONNU)

    assert racine.find("Say") is not None
    assert [action["action"] for action in _actions(sink)] == ["caller_notice"]


def test_le_script_de_demo_peut_jouer_le_scenario_de_l_usurpation(monter_app):
    """Les deux réécritures se chaînent, et c'est le scénario C du pitch joué sans
    payer d'appel international : le script annonce « c'est le téléphone de
    l'arnaqueur qui parle », `DEMO_SPOOF_AS` fait croire que c'est celui de
    Lucas."""
    client, app, sink = monter_app(scammer_phone_number=ARNAQUEUR, demo_spoof_as=LUCAS)

    racine = _appeler(
        client,
        depuis=TWILIO,
        params={"demo_outbound": "1", "presented_from": ARNAQUEUR},
    )

    assert racine.find(".//Stream") is None
    assert app.state.registry.get(CALL_ID).caller_number == LUCAS
    assert [action["action"] for action in _actions(sink)] == [
        "spoof_simulated",
        "trusted_passthrough",
    ]


# --- Lot 3 : la voix de la personne protégée seule --------------------------


def test_le_mode_lot_3_n_ecoute_que_la_personne_protegee(monter_app):
    """Option fausse par défaut, et « à valider juridiquement ». Si cette piste
    devenait `both_tracks`, on écouterait la famille sans que rien d'autre ne
    change dans le système."""
    client, app, sink = monter_app(trusted_victim_only_analysis=True)

    racine = _appeler(client, depuis=LUCAS)

    stream = racine.find("Start/Stream")
    assert stream is not None
    assert stream.get("track") == "outbound_track"
    # Toujours aucune annonce : l'interlocuteur n'est pas capté, il n'y a rien à
    # lui annoncer.
    assert racine.find("Say") is None
    assert app.state.registry.get(CALL_ID).transcribe is True
    assert [action["action"] for action in _actions(sink)] == ["trusted_passthrough"]


def test_le_mode_lot_3_n_ouvre_qu_une_seule_transcription(monter_app):
    """Les pistes transcrites doivent correspondre à celles que le TwiML diffuse.

    Le `<Stream>` ne porte que `outbound_track` : ouvrir aussi la piste appelant
    laisserait une connexion Transcribe facturée sur une piste qui ne recevra
    jamais un octet. Et le symptôme serait invisible — une transcription vide ne
    ressemble pas à une panne.
    """
    client, app, _ = monter_app(trusted_victim_only_analysis=True)

    _appeler(client, depuis=LUCAS)

    session = app.state.registry.get(CALL_ID)
    assert session.speakers == ("victim",)
    assert set(session._streams) == {"victim"}


def test_un_appel_analyse_ouvre_les_deux_transcriptions(monter_app):
    """Le cas nominal, pour que le test précédent ne puisse pas passer en
    cassant l'analyse des inconnus : `both_tracks` porte bien les deux pistes, et
    c'est leur séparation qui dit qui parle."""
    client, app, _ = monter_app()

    racine = _appeler(client, depuis=INCONNU)

    assert racine.find("Start/Stream").get("track") == "both_tracks"
    session = app.state.registry.get(CALL_ID)
    assert session.speakers == ("caller", "victim")
    assert set(session._streams) == {"caller", "victim"}


def test_le_mode_lot_3_est_eteint_par_defaut(monter_app):
    client, app, _ = monter_app()

    assert app.state.settings.trusted_victim_only_analysis is False
    assert _appeler(client, depuis=LUCAS).find(".//Stream") is None


# --- La jambe victime : qui appelle ? ---------------------------------------


def _joindre(client: TestClient, call_id: str = CALL_ID):
    reponse = client.get(f"/twilio/voice/join/{call_id}")
    assert reponse.status_code == 200
    return ElementTree.fromstring(reponse.text)


def test_la_jambe_victime_annonce_le_nom_du_contact(monter_app):
    """Son téléphone affiche notre numéro Twilio, jamais celui de l'appelant :
    sans cette phrase, la liste blanche est invisible pour elle."""
    client, _, _ = monter_app()
    _appeler(client, depuis=LUCAS)

    racine = _joindre(client)

    enfants = [enfant.tag for enfant in racine]
    assert enfants.index("Say") < enfants.index("Dial")
    say = racine.find("Say")
    assert say is not None
    assert say.text == "Appel de Lucas Martin."


def test_la_jambe_victime_annonce_un_inconnu_comme_tel(monter_app):
    client, _, _ = monter_app()
    _appeler(client, depuis=INCONNU)

    say = _joindre(client).find("Say")
    assert say is not None
    assert say.text == "Appel d'un numéro inconnu, appel protégé."


def test_la_jambe_victime_nomme_meme_un_contact_analyse(monter_app):
    """Savoir qui appelle est une question d'affichage, pas la décision de
    confiance : celle-ci s'est prise dans `voice_inbound`."""
    client, _, _ = monter_app()
    _appeler(client, depuis=BANQUE)

    say = _joindre(client).find("Say")
    assert say is not None
    assert say.text == "Appel de Banque Crédit Mutuel."


def test_aucune_annonce_quand_la_session_est_introuvable(monter_app):
    """Pas d'annonce plutôt qu'une annonce fausse : dire « numéro inconnu » quand
    on ne sait pas vaudrait le contraire de sa valeur sur un appel de confiance
    mal retrouvé. La conférence, elle, se monte quand même."""
    client, _, _ = monter_app()

    racine = _joindre(client, "CA_jamais_vu")

    assert racine.find("Say") is None
    assert racine.find("Dial/Conference") is not None


# --- Le garde-fou d'entrée --------------------------------------------------


def test_un_webhook_sans_callsid_ne_change_rien(monter_app):
    """Comportement d'avant le routage, préservé : sans `CallSid` il n'y a pas
    d'appel à router, et répondre du TwiML valide évite que Twilio rejoue."""
    client, app, sink = monter_app()

    reponse = client.post("/twilio/voice/inbound", data={"From": LUCAS})

    assert reponse.status_code == 200
    assert len(ElementTree.fromstring(reponse.text)) == 0
    assert app.state.registry.active() == []
    assert _actions(sink) == []
