"""Ajouter et retirer un contact depuis l'app : `POST /app/contacts/add` · `/remove`.

Ces deux routes ne changent pas un affichage, elles changent le ROUTAGE des
appels : dès que l'ajout répond, un appel de ce numéro est transmis sans
annonce, sans flux audio et sans transcription. Cinq pannes à éviter, et chacune
serait muette :

- **un numéro non normalisé.** Le carnet est indexé par numéro normalisé et
  comparé à l'E.164 que Twilio envoie. Un « 06 00 00 00 22 » stocké tel quel ne
  serait jamais retrouvé : le contact serait au carnet et son appel resterait
  annoncé puis analysé ;
- **un numéro vide.** Il produirait une clé vide dans l'index, donc ferait
  passer un appel MASQUÉ pour ce contact, et ferait *baisser* son score ;
- **un doublon.** Deux entrées sur un même numéro et c'est l'ordre du fichier
  qui décide du routage ;
- **un de nos propres numéros.** S'ajouter soi-même masquerait la garde
  anti-boucle : la ligne renvoyée apparaîtrait comme « contact de confiance » ;
- **le fichier abîmé par l'écriture.** `data/contacts.json` porte `owner`, des
  `_notes` et des `_contacts_desactives` que le chargeur ignore. Le réécrire
  depuis le seul carnet en mémoire les effacerait sans un mot, et une écriture
  interrompue laisserait un JSON tronqué, donc un carnet vide au redémarrage.

Même montage que `test_phone_app.py` : app complète sur les doublures de
`infrastructure/fakes/`, aucun réseau, aucun credential. Le carnet est un
fichier jetable de `tmp_path` — jamais `data/contacts.json`, qui est le vrai
carnet de la démo. Numéros volontairement factices.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vocalguard.config import Settings
from vocalguard.infrastructure.fakes.event_sink import InMemoryEventSink
from vocalguard.infrastructure.fakes.transcriber import FakeTranscriber
from vocalguard.interface.app import create_app

PIN = "432198"
MAUVAIS_PIN = "0000"

CALL_ID = "CA_contacts_01"

TWILIO = "+15550000000"
PROTEGEE = "+33611111111"
VICTIME = "+33600000099"
PROCHE = "+33600000033"
LUCAS = "+33600000011"
CAMILLE = "+33600000022"
INCONNU = "+33755555555"

NOTES = ["Carnet de la démo : ne pas committer.", "Les numéros sont saisis à la main."]
DESACTIVES = [{"name": "Ancien médecin", "phone_number": "+33600000077"}]


@pytest.fixture
def carnet(tmp_path: Path) -> Path:
    """Un carnet qui ressemble au vrai : des clés que le chargeur n'utilise pas,
    et qu'une écriture naïve détruirait."""
    chemin = tmp_path / "contacts.json"
    _ecrire(
        chemin,
        {
            "_notes": NOTES,
            "owner": "Jeanne Moreau",
            "_contacts_desactives": DESACTIVES,
            "contacts": [{"name": "Lucas Martin", "phone_number": LUCAS, "relation": "petit-fils"}],
        },
    )
    return chemin


def _ecrire(chemin: Path, document: dict) -> None:
    chemin.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def _relire(chemin: Path) -> dict:
    return json.loads(chemin.read_text(encoding="utf-8"))


def make_settings(carnet: Path, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "transcribe_backend": "fake",
        "public_hostname": "demo.ngrok-free.app",
        "twilio_phone_number": TWILIO,
        "protected_phone_number": PROTEGEE,
        "victim_phone_number": VICTIME,
        "trusted_contact_phone_number": PROCHE,
        "contacts_path": carnet,
        # Vide exprès : c'est lui qui déciderait `twilio_configured`, donc un
        # vrai client Twilio et un vrai appel sortant en tâche de fond.
        "twilio_auth_token": "",
        # Historique et messagerie coupés, et rangés dans le dossier jetable :
        # ces tests n'ont rien à faire dans `data/`, qui porte les vraies
        # métadonnées de la démo.
        "history_enabled": False,
        "history_path": carnet.parent / "calls.sqlite",
        "voicemail_enabled": False,
        "voicemail_path": carnet.parent / "voicemail.sqlite",
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


def _ajouter(client: TestClient, **champs: object):
    return client.post("/app/contacts/add", json={"pin": PIN, **champs})


def _retirer(client: TestClient, numero: str):
    return client.post("/app/contacts/remove", json={"pin": PIN, "number": numero})


def _noms(reponse) -> list[str]:
    return [item["name"] for item in reponse.json()["items"]]


# --- Le code d'accès --------------------------------------------------------


@pytest.mark.parametrize("route", ["/app/contacts/add", "/app/contacts/remove"])
def test_les_routes_d_ecriture_exigent_le_code(harness, carnet: Path, route: str) -> None:
    """Écrire dans le carnet, c'est décider qui est transmis sans écoute. Une
    route ouverte sur une URL ngrok publique laisserait un inconnu s'y déclarer
    « contact de confiance »."""
    client, _ = harness
    avant = _relire(carnet)

    assert client.post(route, json={}).status_code == 403
    assert client.post(route, json={"pin": MAUVAIS_PIN, "number": CAMILLE}).status_code == 403

    assert _relire(carnet) == avant, "un refus ne doit rien écrire"


# --- Ajouter ----------------------------------------------------------------


class TestAjout:
    def test_le_contact_ajoute_revient_dans_la_meme_forme_que_la_lecture(self, harness) -> None:
        """Même clé `items` et mêmes champs que `POST /app/contacts` : l'app n'a
        ainsi qu'un seul format à lire, pour trois routes."""
        client, _ = harness

        reponse = _ajouter(client, name="Camille", number=CAMILLE, relation="fille")

        assert reponse.status_code == 200
        assert reponse.json() == {
            "items": [
                {"name": "Camille", "number": CAMILLE, "relation": "fille", "mode": "trusted"},
                {
                    "name": "Lucas Martin",
                    "number": LUCAS,
                    "relation": "petit-fils",
                    "mode": "trusted",
                },
            ]
        }
        # Et la lecture seule dit désormais exactement la même chose.
        assert client.post("/app/contacts", json={"pin": PIN}).json() == reponse.json()

    def test_l_appel_du_nouveau_contact_passe_sans_redemarrage(self, harness) -> None:
        """LE test du lot. Le carnet est partagé par cinq détenteurs chargés une
        seule fois : sans rechargement en place, on ajoute son fils dans l'app et
        il continue d'être annoncé puis analysé comme un inconnu."""
        client, _ = harness
        assert _ajouter(client, name="Camille", number=CAMILLE).status_code == 200

        client.post(
            "/twilio/voice/inbound",
            data={"CallSid": CALL_ID, "From": CAMILLE, "To": TWILIO},
        )
        charge = client.post("/app/incoming", json={"pin": PIN}).json()

        assert charge["caller_name"] == "Camille"
        assert charge["trusted"] is True

    def test_le_numero_est_normalise_avant_d_etre_stocke(self, harness, carnet: Path) -> None:
        """Saisi au clavier du téléphone, en national. Stocké tel quel, il ne
        serait jamais retrouvé face au `From` E.164 de Twilio."""
        client, _ = harness

        reponse = _ajouter(client, name="Camille", number="06 00 00 00 22")

        assert reponse.status_code == 200
        assert reponse.json()["items"][0]["number"] == CAMILLE
        entrees = _relire(carnet)["contacts"]
        assert entrees[-1]["phone_number"] == CAMILLE

    def test_le_mode_analyze_est_accepte_et_appliique(self, harness, carnet: Path) -> None:
        """« Au carnet » n'est pas « de confiance » : une banque doit être nommée
        à l'écran sans se voir accorder le silence."""
        client, _ = harness

        reponse = _ajouter(client, name="Banque", number=CAMILLE, mode="analyze")

        assert reponse.status_code == 200
        assert reponse.json()["items"][0]["mode"] == "analyze"
        assert _relire(carnet)["contacts"][-1]["mode"] == "analyze"

    def test_les_alias_sont_enregistres_sans_etre_renvoyes(self, harness, carnet: Path) -> None:
        """Les alias servent à reconnaître une identité revendiquée ; ce sont des
        aides internes, que la liste de l'app n'affiche pas."""
        client, _ = harness

        reponse = _ajouter(client, name="Camille Moreau", number=CAMILLE, aliases=["Camille", " "])

        assert reponse.status_code == 200
        assert "aliases" not in reponse.json()["items"][0]
        # L'alias vide est écarté : il ferait correspondre n'importe quel propos.
        assert _relire(carnet)["contacts"][-1]["aliases"] == ["Camille"]

    @pytest.mark.parametrize(
        ("champs", "attendu"),
        [
            ({"name": "", "number": CAMILLE}, "name"),
            ({"name": "   ", "number": CAMILLE}, "name"),
            ({"name": "Camille", "number": ""}, "number"),
            ({"name": "Camille", "number": "   "}, "number"),
            ({"name": "Camille", "number": "sans chiffre"}, "number"),
        ],
        ids=["nom-vide", "nom-blanc", "numero-vide", "numero-blanc", "numero-sans-chiffre"],
    )
    def test_un_nom_ou_un_numero_vide_est_refuse(
        self, harness, carnet: Path, champs: dict, attendu: str
    ) -> None:
        """Le refus le plus important. Un numéro vide produirait une clé vide dans
        l'index, donc ferait passer un appel masqué pour ce contact et ferait
        *baisser* son score — le sens dangereux. Un nom vide, lui, serait ignoré
        par le chargeur : l'ajout aurait l'air d'avoir marché sans rien faire."""
        client, _ = harness
        avant = _relire(carnet)

        reponse = _ajouter(client, **champs)

        assert reponse.status_code == 400
        assert attendu in reponse.json()["detail"]
        assert _relire(carnet) == avant

    def test_un_doublon_est_refuse_en_nommant_le_contact_deja_present(
        self, harness, carnet: Path
    ) -> None:
        """Deux entrées sur un même numéro et le routage dépend de l'ordre du
        fichier. Le nom déjà présent est la seule information utile : c'est
        peut-être le bon contact, sous un autre prénom."""
        client, _ = harness

        reponse = _ajouter(client, name="Lucas", number="06 00 00 00 11")

        assert reponse.status_code == 409
        assert "Lucas Martin" in reponse.json()["detail"]
        assert len(_relire(carnet)["contacts"]) == 1

    @pytest.mark.parametrize(
        "numero",
        [PROTEGEE, VICTIME, TWILIO, PROCHE],
        ids=["ligne-protegee", "ligne-de-secours", "numero-twilio", "vrai-proche"],
    )
    def test_nos_propres_numeros_sont_refuses(self, harness, carnet: Path, numero: str) -> None:
        """Un appel venant de là EST la boucle du renvoi, que la garde refuse. Le
        voir apparaître comme « contact de confiance » ferait croire la garde
        cassée et enverrait chercher très loin."""
        client, _ = harness
        avant = _relire(carnet)

        reponse = _ajouter(client, name="Moi", number=numero)

        assert reponse.status_code == 400
        assert "protected line" in reponse.json()["detail"]
        assert _relire(carnet) == avant

    def test_un_mode_inconnu_est_refuse_sans_repli_silencieux(self, harness, carnet: Path) -> None:
        """Contrairement au fichier écrit à la main, qui retombe sur « trusted »
        en le disant : la personne est devant son écran et peut corriger tout de
        suite. Retomber sur un défaut déciderait à sa place qui est écouté."""
        client, _ = harness
        avant = _relire(carnet)

        reponse = _ajouter(client, name="Banque", number=CAMILLE, mode="analyse")

        assert reponse.status_code == 400
        detail = reponse.json()["detail"]
        assert "trusted" in detail and "analyze" in detail
        assert _relire(carnet) == avant


# --- Retirer ----------------------------------------------------------------


class TestRetrait:
    def test_le_contact_retire_disparait_de_la_liste_et_du_routage(self, harness) -> None:
        client, _ = harness

        reponse = _retirer(client, LUCAS)

        assert reponse.status_code == 200
        assert reponse.json() == {"items": []}

        client.post(
            "/twilio/voice/inbound",
            data={"CallSid": CALL_ID, "From": LUCAS, "To": TWILIO},
        )
        charge = client.post("/app/incoming", json={"pin": PIN}).json()
        assert charge["caller_name"] == ""
        assert charge["trusted"] is False

    def test_un_numero_national_retrouve_une_entree_ecrite_en_e164(self, harness) -> None:
        """Le fichier est rempli à la main, l'app envoie ce qu'elle affiche :
        comparer les chaînes brutes répondrait 404 sur un contact présent."""
        client, _ = harness

        assert _retirer(client, "06 00 00 00 11").status_code == 200

    def test_un_numero_absent_est_une_404(self, harness, carnet: Path) -> None:
        """Pas un succès muet : « retiré » à l'écran alors que l'appel continuerait
        de passer sans analyse est le pire des deux."""
        client, _ = harness
        avant = _relire(carnet)

        reponse = _retirer(client, INCONNU)

        assert reponse.status_code == 404
        assert _relire(carnet) == avant

    def test_un_numero_vide_est_refuse(self, harness) -> None:
        client, _ = harness
        reponse = _retirer(client, "")
        assert reponse.status_code == 400
        assert "number" in reponse.json()["detail"]


# --- Le fichier -------------------------------------------------------------


class TestEcritureDuFichier:
    def test_les_cles_inconnues_du_chargeur_sont_preservees(self, harness, carnet: Path) -> None:
        """`_notes`, `owner` et `_contacts_desactives` ne sont pas lus par le
        chargeur, qui ne regarde que `contacts`. Réécrire le fichier depuis le
        seul carnet en mémoire les effacerait définitivement — y compris des
        contacts volontairement mis de côté."""
        client, _ = harness

        assert _ajouter(client, name="Camille", number=CAMILLE).status_code == 200
        assert _retirer(client, LUCAS).status_code == 200

        document = _relire(carnet)
        assert document["_notes"] == NOTES
        assert document["owner"] == "Jeanne Moreau"
        assert document["_contacts_desactives"] == DESACTIVES
        assert [e["name"] for e in document["contacts"]] == ["Camille"]

    def test_le_fichier_reste_lisible_et_modifiable_a_la_main(self, harness, carnet: Path) -> None:
        """`ensure_ascii=False` et `indent=2` : ce carnet se corrige beaucoup à la
        main. Une seule ligne pleine de « \\u00e9 » serait valide et
        pratiquement inutilisable."""
        client, _ = harness

        assert _ajouter(client, name="Léa", number=CAMILLE, relation="nièce").status_code == 200

        texte = carnet.read_text(encoding="utf-8")
        assert "Léa" in texte
        assert "\\u" not in texte
        assert '\n  "contacts"' in texte or '\n  "_notes"' in texte

    def test_aucun_fichier_temporaire_ne_reste_derriere(self, harness, carnet: Path) -> None:
        """L'écriture passe par un temporaire dans le même répertoire puis
        `os.replace` : atomique, donc jamais de JSON tronqué — mais pas de
        résidus pour autant, sinon le dossier `data/` se remplit à chaque ajout."""
        client, _ = harness

        _ajouter(client, name="Camille", number=CAMILLE)
        _retirer(client, LUCAS)

        # Le temporaire est préfixé d'un point : c'est lui, et lui seul, qu'on
        # cherche ici (le dossier jetable porte aussi les SQLite du montage).
        assert [p.name for p in carnet.parent.iterdir() if p.name.startswith(".")] == []

    def test_un_carnet_illisible_refuse_l_ecriture_sans_rien_perdre(
        self, harness, carnet: Path
    ) -> None:
        """Réécrire par-dessus ce qu'on n'a pas su lire, c'est perdre son
        contenu : on refuse, on le dit, et la mémoire reste intacte."""
        client, _ = harness
        carnet.write_text('{"contacts": [{"name"', encoding="utf-8")

        reponse = _ajouter(client, name="Camille", number=CAMILLE)

        assert reponse.status_code == 503
        assert "contacts.json" in reponse.json()["detail"]
        assert carnet.read_text(encoding="utf-8") == '{"contacts": [{"name"'
        # Le carnet du démarrage répond toujours : rien n'a été rechargé.
        assert _noms(client.post("/app/contacts", json={"pin": PIN})) == ["Lucas Martin"]

    def test_une_ecriture_impossible_laisse_la_memoire_intacte(self, tmp_path: Path) -> None:
        """Disque plein, droits insuffisants, dossier inaccessible : une erreur
        explicite, et surtout aucun rechargement — sinon un échec d'écriture
        viderait le carnet en mémoire par-dessus le marché."""
        obstacle = tmp_path / "pas_un_dossier"
        obstacle.write_text("", encoding="utf-8")
        app = create_app(
            make_settings(
                obstacle / "contacts.json",
                history_path=tmp_path / "calls.sqlite",
                voicemail_path=tmp_path / "voicemail.sqlite",
            ),
            sink=InMemoryEventSink(),
            transcriber_factory=FakeTranscriber,
        )
        with TestClient(app) as client:
            reponse = _ajouter(client, name="Camille", number=CAMILLE)

            assert reponse.status_code == 503
            assert "Nothing was changed" in reponse.json()["detail"]
            assert client.post("/app/contacts", json={"pin": PIN}).json() == {"items": []}

    def test_un_carnet_absent_est_cree_par_le_premier_ajout(self, tmp_path: Path) -> None:
        """Installation neuve : `data/contacts.json` est gitignoré, donc absent
        d'un clone. Le premier ajout depuis l'app doit marcher."""
        chemin = tmp_path / "jamais_ecrit.json"
        app = create_app(
            make_settings(chemin), sink=InMemoryEventSink(), transcriber_factory=FakeTranscriber
        )
        with TestClient(app) as client:
            reponse = _ajouter(client, name="Camille", number=CAMILLE)

            assert reponse.status_code == 200
            assert _noms(reponse) == ["Camille"]
        assert _relire(chemin)["contacts"][0]["phone_number"] == CAMILLE
