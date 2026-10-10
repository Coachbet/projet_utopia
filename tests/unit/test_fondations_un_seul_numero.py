"""Le socle du montage « un seul numéro » : carnet, réglages dérivés, session muette.

Ces trois briques n'ont l'air de rien et portent chacune une décision qu'un
« nettoyage » ultérieur casserait en silence :

- un contact sans champ `mode` est de CONFIANCE. C'est la règle produit (être au
  carnet, c'est être de confiance) et c'est aussi ce qui garde lisible le carnet
  écrit avant le routage. Inverser ce défaut mettrait la famille sous écoute.
- `loop_guard_numbers` écarte les valeurs VIDES. Normalisés, deux numéros vides
  sont égaux : les garder ferait passer un appel masqué (`From` absent) pour une
  boucle de renvoi, donc ferait refuser exactement l'appel qu'on veut analyser.
- `demo_spoof_active` exige DEUX réglages. `DEMO_SPOOF_AS` seul, oublié dans un
  `.env` après la démo, réécrirait le `From` d'appels réels et ferait croire de
  confiance n'importe quel inconnu.
- une session peut exister sans transcription. La session est nécessaire
  (vérification d'identité, mise en attente, dashboard) ; l'écoute, non.
- le schéma d'événement et le code nomment les MÊMES actions. Le contrat est
  figé à H0 et `data.action` est sa seule extension possible : deux listes qui
  divergent rendraient le schéma faux sans qu'aucun test ne le dise.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vocalguard.application.sessions import SessionRegistry
from vocalguard.config import Settings
from vocalguard.domain.alerts import ActionKind, ReportedAction
from vocalguard.domain.models import Contact
from vocalguard.infrastructure.contacts.book import JsonContactBook
from vocalguard.infrastructure.fakes.event_sink import InMemoryEventSink
from vocalguard.infrastructure.fakes.transcriber import FakeTranscriber

RACINE = Path(__file__).resolve().parents[2]


def _ecrire_carnet(tmp_path, entries, *, owner: str | None = "Jeanne Moreau"):
    payload: dict[str, object] = {"contacts": entries}
    if owner is not None:
        payload["owner"] = owner
    path = tmp_path / "contacts.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# --- Le mode d'un contact ---------------------------------------------------


class TestModeDuContact:
    def test_un_contact_sans_mode_est_de_confiance(self, tmp_path) -> None:
        """Rétrocompatibilité du carnet existant : aucune de ses entrées ne porte
        `mode`, et toutes doivent continuer à passer sans analyse."""
        book = JsonContactBook.from_path(
            _ecrire_carnet(tmp_path, [{"name": "Lucas", "phone_number": "+33612345678"}])
        )
        contact = book.by_number("+33612345678")
        assert contact is not None
        assert contact.mode == "trusted"
        assert contact.is_trusted is True

    @pytest.mark.parametrize(
        ("mode", "attendu"),
        [("trusted", True), ("analyze", False)],
    )
    def test_les_deux_modes_declares_sont_lus(self, tmp_path, mode, attendu) -> None:
        book = JsonContactBook.from_path(
            _ecrire_carnet(
                tmp_path,
                [{"name": "Banque", "phone_number": "+33969321234", "mode": mode}],
            )
        )
        contact = book.by_number("+33969321234")
        assert contact is not None
        assert contact.mode == mode
        assert contact.is_trusted is attendu

    @pytest.mark.parametrize("mode", ["Trusted", "analyse", "", "oui", 1, None])
    def test_un_mode_inconnu_retombe_sur_trusted_en_le_disant(self, tmp_path, mode, caplog) -> None:
        """Le choix qui compte, et qui se « corrigerait » facilement à l'envers.

        « Analyze par prudence » serait prudent pour la détection et faux pour la
        vie privée : une faute de frappe (`"analyse"` en français, une majuscule)
        mettrait la famille sous écoute sans que personne le sache. On applique
        donc le défaut annoncé, et on le dit assez fort pour que l'intention
        réelle soit corrigée dans le fichier.
        """
        entry = {"name": "Camille", "phone_number": "+33698765432"}
        if mode is not None:
            entry["mode"] = mode

        with caplog.at_level("WARNING"):
            book = JsonContactBook.from_path(_ecrire_carnet(tmp_path, [entry]))

        contact = book.by_number("+33698765432")
        assert contact is not None
        assert contact.mode == "trusted"
        # `mode` absent est le cas NORMAL : il ne doit pas avertir. Une valeur
        # inconnue, si — c'est une erreur de saisie, pas un défaut assumé.
        avertissements = [r for r in caplog.records if "mode" in r.getMessage()]
        assert bool(avertissements) is (mode is not None)

    def test_le_mode_ne_change_pas_la_recherche_par_identite(self, tmp_path) -> None:
        """`mode` décide du ROUTAGE, pas de la reconnaissance : un contact en
        `analyze` reste trouvable par son nom, sinon la vérification d'identité
        par code ne saurait plus à qui envoyer le SMS."""
        book = JsonContactBook.from_path(
            _ecrire_carnet(
                tmp_path,
                [
                    {
                        "name": "Banque Crédit Mutuel",
                        "phone_number": "+33969321234",
                        "aliases": ["ma banque"],
                        "mode": "analyze",
                    }
                ],
            )
        )
        assert book.by_identity("c'est ma banque") is not None


class TestOwnerDuCarnet:
    def test_le_prenom_de_la_personne_protegee_est_lu(self, tmp_path) -> None:
        book = JsonContactBook.from_path(_ecrire_carnet(tmp_path, [], owner="Jeanne Moreau"))
        assert book.owner == "Jeanne Moreau"

    def test_un_carnet_sans_owner_ne_lui_invente_pas_de_nom(self, tmp_path) -> None:
        """Chaîne vide, jamais un nom de remplacement : le SMS de vérification
        doit alors se formuler sans nom. Annoncer au proche un appel chez
        quelqu'un qui n'existe pas serait pire que de ne pas nommer."""
        book = JsonContactBook.from_path(_ecrire_carnet(tmp_path, [], owner=None))
        assert book.owner == ""

    def test_un_carnet_absent_ne_casse_rien(self, tmp_path) -> None:
        book = JsonContactBook.from_path(tmp_path / "jamais_ecrit.json")
        assert book.owner == ""
        assert book.all() == ()

    def test_le_carnet_construit_en_memoire_accepte_un_owner(self) -> None:
        """Le chemin des doublures et du rejeu : pas de fichier, mais le même
        contrat de lecture que `from_path`."""
        book = JsonContactBook([Contact(name="Lucas", phone_number="+33612345678")], owner="Jeanne")
        assert book.owner == "Jeanne"


# --- Les réglages dérivés ---------------------------------------------------


class TestGardeAntiBoucle:
    def test_les_trois_numeros_du_montage_sont_refuses(self) -> None:
        settings = Settings(
            _env_file=None,
            protected_phone_number="+33611111111",
            victim_phone_number="+33622222222",
            twilio_phone_number="+16202648948",
        )
        assert settings.loop_guard_numbers == (
            "+33611111111",
            "+33622222222",
            "+16202648948",
        )

    def test_les_numeros_vides_sont_ecartes(self) -> None:
        """Le piège exact : normalisés, deux numéros vides sont ÉGAUX. Les garder
        ferait passer un appel masqué pour une boucle de renvoi — et un appel
        masqué est justement celui qu'on veut analyser, pas refuser."""
        settings = Settings(_env_file=None, twilio_phone_number="+16202648948")
        assert settings.loop_guard_numbers == ("+16202648948",)

    def test_aucun_numero_configure_ne_garde_rien(self) -> None:
        assert Settings(_env_file=None).loop_guard_numbers == ()


class TestCibleDeLaJambeProtegee:
    def test_une_identite_devient_une_cible_client(self) -> None:
        settings = Settings(_env_file=None, voice_client_identity="mamie")
        assert settings.voice_client_target == "client:mamie"

    def test_sans_identite_il_n_y_a_pas_de_cible(self) -> None:
        """Chaîne vide et non `"client:"` : un `to="client:"` partirait chez
        Twilio pour être refusé par un code opaque, là où une valeur vide fait
        choisir le repli téléphonique."""
        assert Settings(_env_file=None).voice_client_target == ""


class TestJetonVoiceConfigure:
    def _complet(self, **surcharges):
        valeurs = {
            "twilio_account_sid": "AC_factice",
            "twilio_api_key_sid": "SK_factice",
            "twilio_api_key_secret": "secret_factice",
            "twilio_twiml_app_sid": "AP_factice",
            "voice_client_identity": "mamie",
        }
        valeurs.update(surcharges)
        return Settings(_env_file=None, **valeurs)

    def test_les_cinq_valeurs_presentes(self) -> None:
        assert self._complet().app_token_configured is True

    @pytest.mark.parametrize(
        "manquant",
        [
            "twilio_account_sid",
            "twilio_api_key_sid",
            "twilio_api_key_secret",
            "twilio_twiml_app_sid",
            "voice_client_identity",
        ],
    )
    def test_il_en_manque_toujours_une(self, manquant) -> None:
        """Exigées ensemble, parce qu'il manque toujours la dernière quand on
        croit avoir fini — et que le symptôme d'une API Key absente est un 401
        opaque côté navigateur, pas un message lisible."""
        assert self._complet(**{manquant: ""}).app_token_configured is False


class TestUsurpationSimulee:
    def test_les_deux_reglages_sont_exiges(self) -> None:
        settings = Settings(
            _env_file=None,
            demo_spoof_as="+33612345678",
            scammer_phone_number="+33755555555",
        )
        assert settings.demo_spoof_active is True

    def test_le_numero_a_imiter_seul_est_inerte(self) -> None:
        """La garde qui compte : un `DEMO_SPOOF_AS` oublié dans un `.env` après la
        démo réécrirait le `From` d'appels RÉELS, et le service croirait de
        confiance n'importe quel inconnu."""
        settings = Settings(_env_file=None, demo_spoof_as="+33612345678")
        assert settings.demo_spoof_active is False

    def test_le_telephone_arnaqueur_seul_est_inerte(self) -> None:
        settings = Settings(_env_file=None, scammer_phone_number="+33755555555")
        assert settings.demo_spoof_active is False

    def test_rien_n_est_arme_par_defaut(self) -> None:
        assert Settings(_env_file=None).demo_spoof_active is False


class TestAnnonceAlAppelant:
    def test_la_phrase_par_defaut_informe_de_l_analyse(self) -> None:
        """Le texte lui-même porte le montage juridique : il doit nommer
        l'analyse, sinon l'annonce n'informe de rien."""
        texte = Settings(_env_file=None).caller_notice_text
        assert "analyzed" in texte.casefold()
        assert texte.endswith(".")


# --- Une session sans écoute ------------------------------------------------


class TestSessionSansEcoute:
    def _registre(self):
        return SessionRegistry(
            transcriber_factory=FakeTranscriber,
            sink=InMemoryEventSink(),
            settings=Settings(_env_file=None, transcribe_backend="fake"),
        )

    async def test_un_appel_de_confiance_n_ouvre_aucune_piste(self) -> None:
        """Le cœur de la promesse : la famille n'est pas écoutée.

        On vérifie l'absence de piste ouverte et pas seulement l'absence de
        `<Stream>` dans le TwiML : les deux doivent être vraies, et c'est celle-ci
        qui évite en plus deux connexions Transcribe facturées pour une piste qui
        restera muette.
        """
        session = await self._registre().open("CA_confiance", transcribe=False)

        assert session.transcribe is False
        assert session._streams == {}

    async def test_un_inconnu_est_ecoute_par_defaut(self) -> None:
        """Le défaut reste l'analyse : tout appelant est écouté sauf décision
        explicite du routage. L'inverse ferait d'un oubli une absence de
        protection."""
        session = await self._registre().open("CA_inconnu")

        assert session.transcribe is True
        assert set(session._streams) == {"caller", "victim"}

    async def test_la_session_existe_quand_meme(self) -> None:
        """Elle doit exister : c'est elle que retrouvent la vérification
        d'identité, la mise en attente et le dashboard. Ne pas l'ouvrir ferait
        répondre 404 à tous les boutons sur un appel de confiance."""
        registre = self._registre()
        await registre.open("CA_confiance", transcribe=False)

        assert registre.get("CA_confiance") is not None
        assert [s.call_id for s in registre.active()] == ["CA_confiance"]


# --- Le contrat d'événement et le code disent la même chose -----------------


class TestContratDesActions:
    """`contracts/event.schema.json` est la référence que lisent les autres
    briques, et personne ne l'exécute. Sans ce test, ajouter un fait de routage
    dans le code laisserait le schéma affirmer qu'il est invalide — et c'est
    justement le fichier qu'on brandit pour dire « le contrat est figé »."""

    def _enum_du_schema(self) -> set[str]:
        schema = json.loads(
            (RACINE / "contracts" / "event.schema.json").read_text(encoding="utf-8")
        )
        return set(schema["properties"]["data"]["properties"]["action"]["enum"])

    def test_le_schema_declare_exactement_les_actions_du_code(self) -> None:
        attendu = {kind.value for kind in ActionKind} | {fait.value for fait in ReportedAction}
        assert self._enum_du_schema() == attendu

    def test_les_deux_familles_ne_se_chevauchent_pas(self) -> None:
        """`InMemoryEventSink.actions()` écarte les `ReportedAction` pour isoler
        ce que la politique d'alerte a déclenché. Une valeur présente dans les
        deux familles rendrait ce filtre aveugle à une vraie action."""
        effets = {kind.value for kind in ActionKind}
        faits = {fait.value for fait in ReportedAction}
        assert effets.isdisjoint(faits)

    def test_aucun_type_d_evenement_n_a_ete_ajoute(self) -> None:
        """La règle non négociable du contrat figé à H0 : les nouveautés passent
        par `data.action`, jamais par un nouveau `type`."""
        from vocalguard.domain.events import EventType

        schema = json.loads(
            (RACINE / "contracts" / "event.schema.json").read_text(encoding="utf-8")
        )
        assert set(schema["properties"]["type"]["enum"]) == {t.value for t in EventType}
        assert len(EventType) == 7
