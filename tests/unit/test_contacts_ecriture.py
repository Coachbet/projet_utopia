"""Le carnet d'adresses rechargeable : `JsonContactBook.reload`.

Ce que ces tests protègent n'est pas une méthode mais deux pannes entièrement
silencieuses, qui n'apparaîtraient qu'au pire moment — pendant un appel réel :

- **un ajout sans effet.** `create_app` charge le carnet une seule fois et le
  partage entre cinq détenteurs : `app.state.contacts`, le pipeline de score, la
  vérification d'identité, l'enregistreur d'historique et le signal de
  réputation. Un `reload` qui RENVERRAIT un nouvel objet au lieu de remplacer
  son état n'atteindrait que le premier. On ajoute son fils depuis l'app, et il
  continue d'être traité comme un inconnu — annonce à l'appelant et analyse
  comprises — jusqu'au redémarrage du serveur, sans un seul message d'erreur ;

- **un carnet perdu.** Si un fichier devenu illisible vidait la mémoire, tous
  les proches deviendraient des inconnus d'un coup. On préfère un carnet périmé,
  qui reste juste pour tous les contacts déjà connus, à un carnet vide. Un
  carnet volontairement vidé par la personne, lui, doit bien s'appliquer : c'est
  toute la différence entre « illisible » et « sans contact ».

Numéros volontairement factices (`+3360000000x`) : aucun numéro réel n'entre
dans un test, et `data/contacts.json` est le carnet de la démo — ces tests
n'écrivent que dans `tmp_path`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vocalguard.domain.models import Contact
from vocalguard.infrastructure.contacts.book import JsonContactBook

LUCAS = "+33600000011"
CAMILLE = "+33600000022"


def _carnet(tmp_path: Path, entrees: list[dict], *, owner: str = "Jeanne") -> Path:
    chemin = tmp_path / "contacts.json"
    chemin.write_text(
        json.dumps({"owner": owner, "contacts": entrees}, ensure_ascii=False),
        encoding="utf-8",
    )
    return chemin


def _lucas(**extra: object) -> dict:
    return {"name": "Lucas Martin", "phone_number": LUCAS, "relation": "petit-fils", **extra}


def _camille() -> dict:
    return {"name": "Camille", "phone_number": CAMILLE, "relation": "fille"}


class Detenteur:
    """Ce que font les cinq consommateurs du carnet : ils gardent L'OBJET.

    `TwilioReputationChecker`, `IdentityVerifier`, `HistoryRecorder` et le
    pipeline reçoivent le carnet dans leur constructeur et ne le relisent jamais.
    Cette doublure les représente tous : si la référence qu'elle garde ne voit
    pas la modification, les quatre ne la voient pas non plus.
    """

    def __init__(self, carnet: JsonContactBook) -> None:
        self.carnet = carnet


class TestRechargementEnPlace:
    def test_un_contact_ajoute_au_fichier_est_vu_sans_redemarrage(self, tmp_path) -> None:
        chemin = _carnet(tmp_path, [_lucas()])
        carnet = JsonContactBook.from_path(chemin)
        detenteur = Detenteur(carnet)
        _carnet(tmp_path, [_lucas(), _camille()])

        assert carnet.reload() is True

        # L'objet, pas un remplaçant : c'est tout l'enjeu.
        assert detenteur.carnet is carnet
        contact = detenteur.carnet.by_number(CAMILLE)
        assert contact is not None
        assert contact.name == "Camille"
        assert len(detenteur.carnet.all()) == 2

    def test_un_contact_retire_du_fichier_disparait_de_l_index(self, tmp_path) -> None:
        """Le sens dangereux du retrait : un contact resté dans l'index continue
        d'être « de confiance », donc son appel continue de passer sans analyse.
        L'index est reconstruit à neuf, jamais complété."""
        chemin = _carnet(tmp_path, [_lucas(), _camille()])
        carnet = JsonContactBook.from_path(chemin)
        _carnet(tmp_path, [_lucas()])

        assert carnet.reload() is True

        assert carnet.by_number(CAMILLE) is None
        assert carnet.by_identity("c'est Camille") is None
        assert [c.name for c in carnet.all()] == ["Lucas Martin"]

    def test_un_changement_de_mode_s_applique(self, tmp_path) -> None:
        """Passer un contact en `analyze` est une décision de vie privée : elle
        doit prendre effet au prochain appel, pas au prochain redémarrage."""
        chemin = _carnet(tmp_path, [_lucas()])
        carnet = JsonContactBook.from_path(chemin)
        assert carnet.by_number(LUCAS).is_trusted is True

        _carnet(tmp_path, [_lucas(mode="analyze")])
        assert carnet.reload() is True

        assert carnet.by_number(LUCAS).is_trusted is False

    def test_le_nom_de_la_personne_protegee_suit_le_fichier(self, tmp_path) -> None:
        chemin = _carnet(tmp_path, [_lucas()], owner="Jeanne")
        carnet = JsonContactBook.from_path(chemin)
        _carnet(tmp_path, [_lucas()], owner="Jeanne Moreau")

        assert carnet.reload() is True
        assert carnet.owner == "Jeanne Moreau"

    def test_un_carnet_vide_par_la_personne_devient_vide(self, tmp_path) -> None:
        """La distinction qui compte : un fichier lisible SANS contact s'applique
        (elle a retiré son dernier contact), un fichier illisible non."""
        chemin = _carnet(tmp_path, [_lucas()])
        carnet = JsonContactBook.from_path(chemin)
        _carnet(tmp_path, [])

        assert carnet.reload() is True
        assert carnet.all() == ()
        assert carnet.by_number(LUCAS) is None

    def test_les_numeros_sont_reindexes_sous_leur_forme_normalisee(self, tmp_path) -> None:
        """Le fichier se remplit à la main ("06 00 00 00 22") et Twilio envoie de
        l'E.164 : sans réindexation normalisée, le contact est au carnet et son
        appel reste pourtant annoncé puis analysé."""
        chemin = _carnet(tmp_path, [_lucas()])
        carnet = JsonContactBook.from_path(chemin)
        _carnet(tmp_path, [{"name": "Camille", "phone_number": "06 00 00 00 22"}])

        assert carnet.reload() is True
        assert carnet.by_number(CAMILLE) is not None

    def test_un_numero_vide_n_entre_pas_dans_l_index(self, tmp_path) -> None:
        """Une clé vide ferait passer un appel MASQUÉ pour ce contact, donc ferait
        *baisser* son score : le sens dangereux, à écarter au rechargement comme
        au chargement."""
        chemin = _carnet(tmp_path, [_lucas()])
        carnet = JsonContactBook.from_path(chemin)
        _carnet(tmp_path, [{"name": "Sans numéro", "phone_number": "   "}])

        assert carnet.reload() is True
        assert carnet.by_number("") is None
        assert carnet.by_number("   ") is None

    def test_un_chemin_explicite_devient_le_chemin_memorise(self, tmp_path) -> None:
        """`reload(chemin)` sert au cas où le carnet déménage ; les rechargements
        suivants doivent suivre, sans quoi ils reliraient l'ancien fichier."""
        premier = _carnet(tmp_path, [_lucas()])
        second = tmp_path / "ailleurs.json"
        second.write_text(json.dumps({"contacts": [_camille()]}), encoding="utf-8")
        carnet = JsonContactBook.from_path(premier)

        assert carnet.reload(second) is True
        assert carnet.by_number(CAMILLE) is not None

        second.write_text(json.dumps({"contacts": []}), encoding="utf-8")
        assert carnet.reload() is True
        assert carnet.all() == ()


class TestUnEchecNeVidePasLeCarnet:
    @pytest.fixture
    def carnet_charge(self, tmp_path) -> tuple[JsonContactBook, Path]:
        chemin = _carnet(tmp_path, [_lucas()])
        return JsonContactBook.from_path(chemin), chemin

    def _etat_intact(self, carnet: JsonContactBook) -> None:
        contact = carnet.by_number(LUCAS)
        assert contact is not None, "un échec de rechargement ne doit rien faire perdre"
        assert contact.name == "Lucas Martin"
        assert carnet.owner == "Jeanne"

    def test_un_json_tronque_conserve_l_etat(self, carnet_charge) -> None:
        """Le symptôme d'une écriture interrompue. Vider le carnet ici
        transformerait tous les proches en inconnus, en pleine démo."""
        carnet, chemin = carnet_charge
        chemin.write_text('{"contacts": [{"name"', encoding="utf-8")

        assert carnet.reload() is False
        self._etat_intact(carnet)

    def test_un_fichier_disparu_conserve_l_etat(self, carnet_charge) -> None:
        carnet, chemin = carnet_charge
        chemin.unlink()

        assert carnet.reload() is False
        self._etat_intact(carnet)

    @pytest.mark.parametrize(
        "contenu",
        ['{"contacts": 3}', '{"contacts": ["Lucas"]}', '"Lucas"', "null"],
        ids=["contacts-non-liste", "entree-non-objet", "chaine", "null"],
    )
    def test_un_json_valide_mais_de_mauvaise_forme_conserve_l_etat(
        self, carnet_charge, contenu: str
    ) -> None:
        """Lisible par `json` et inutilisable comme carnet. Sans filet, ces
        formes lèvent au milieu du chargement : 500 depuis la route qui recharge,
        et plantage de `create_app` au démarrage."""
        carnet, chemin = carnet_charge
        chemin.write_text(contenu, encoding="utf-8")

        assert carnet.reload() is False
        self._etat_intact(carnet)

    def test_l_echec_est_journalise_sans_contenu_du_carnet(self, carnet_charge, caplog) -> None:
        """Un rechargement raté est invisible depuis l'app : sans trace, personne
        ne saura pourquoi le carnet « ne se met plus à jour »."""
        carnet, chemin = carnet_charge
        chemin.write_text("{ pas du json", encoding="utf-8")

        with caplog.at_level("WARNING", logger="vocalguard.infrastructure.contacts.book"):
            assert carnet.reload() is False

        assert caplog.text, "un échec doit laisser une trace"
        assert "conservé" in caplog.text
        assert LUCAS not in caplog.text


class TestCarnetSansFichier:
    def test_un_carnet_construit_en_memoire_refuse_de_recharger(self, caplog) -> None:
        """Le chemin des doublures et du rejeu : il n'y a pas de fichier
        d'origine, donc rien à relire. `False` et un journal, plutôt qu'une
        exception qui ferait tomber la route d'écriture."""
        carnet = JsonContactBook([Contact(name="Lucas Martin", phone_number=LUCAS)])

        with caplog.at_level("WARNING", logger="vocalguard.infrastructure.contacts.book"):
            assert carnet.reload() is False

        assert "mémoire" in caplog.text
        assert carnet.by_number(LUCAS) is not None

    def test_un_fichier_absent_au_demarrage_reste_rechargeable(self, tmp_path) -> None:
        """Installation neuve : `data/contacts.json` n'existe pas encore, et le
        premier ajout depuis l'app le crée. Sans chemin mémorisé malgré l'échec
        initial, ce premier ajout resterait invisible jusqu'au redémarrage."""
        chemin = tmp_path / "contacts.json"
        carnet = JsonContactBook.from_path(chemin)
        assert carnet.all() == ()

        _carnet(tmp_path, [_lucas()])

        assert carnet.reload() is True
        assert carnet.by_number(LUCAS) is not None
