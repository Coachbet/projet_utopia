"""Vérification d'identité par code à 4 chiffres — le moment fort du pitch.

Ce que ces tests protègent, dans l'ordre d'importance :

- **le code ne part que vers un numéro du carnet**, jamais vers le `From` de
  l'appel. C'est l'invariant de sécurité : le `From` est précisément ce qu'un
  usurpateur contrôle, lui envoyer le code serait lui donner la réponse ;
- **le code ne circule jamais sur le bus d'événements**, qui part au dashboard et
  à l'app par un WebSocket sans authentification ;
- **la lecture des nombres français dits à voix haute** : le proche dicte
  « quarante-huit vingt et un » et le serveur attend « 4821 ». Table de cas
  généreuse, c'est le point technique du lot ;
- **une information douteuse ne conclut rien** : un code faux fait répéter, et une
  fin de fenêtre sur un appel qu'on n'écoute pas (contact de confiance : aucun
  flux audio) n'accuse personne.

Aucun réseau, aucun SMS, aucun téléphone : tout passe par les doublures de
`infrastructure/fakes/`.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from vocalguard.application.sessions import CallSession, SessionRegistry
from vocalguard.application.verification import (
    REASON_ALREADY_ACTIVE,
    REASON_ATTEMPTS_EXHAUSTED,
    REASON_CALLER_IS_TARGET,
    REASON_NO_CONTACT,
    REASON_SMS_REFUSED,
    IdentityVerifier,
    code_reconnu,
    codes_entendus,
    demande_de_verification,
)
from vocalguard.config import Settings
from vocalguard.domain.alerts import (
    ANNOUNCE_VERIFY_EXPIRED,
    ANNOUNCE_VERIFY_NO_CONTACT,
    ANNOUNCE_VERIFY_OK,
    ANNOUNCE_VERIFY_SAME_NUMBER,
    ANNOUNCE_VERIFY_SMS_FAILED,
    ANNOUNCE_VERIFY_UNHEARD,
    ANNOUNCE_VERIFY_WRONG,
    say_digits,
)
from vocalguard.domain.events import Event, EventType
from vocalguard.domain.models import Contact, ContentSignal
from vocalguard.infrastructure.contacts.book import JsonContactBook
from vocalguard.infrastructure.fakes.call_controller import LoggingCallController
from vocalguard.infrastructure.fakes.event_sink import InMemoryEventSink
from vocalguard.infrastructure.fakes.transcriber import FakeTranscriber
from vocalguard.infrastructure.telephony.media_stream import Dtmf
from vocalguard.interface.lifecycle import release_call

CALL_ID = "CA_verif_01"

LUCAS = "+33672019048"
"""Le VRAI téléphone de Lucas, tel que le carnet le connaît. C'est là que le code
doit arriver, y compris — surtout — quand l'appel se présente avec ce numéro."""

INCONNU = "+33612000000"
"""Le téléphone de l'arnaqueur quand il n'usurpe rien."""


def _carnet() -> JsonContactBook:
    """Un carnet réaliste, chargé sans fichier."""
    return JsonContactBook(
        [
            Contact(
                name="Lucas Martin",
                phone_number=LUCAS,
                relation="petit-fils",
                aliases=("Lucas", "petit-fils"),
            ),
            Contact(name="Camille Martin", phone_number="+33698765432", relation="fille"),
        ],
        owner="Jeanne",
    )


class PipelineEspion:
    """Ne retient que ce qui compte : a-t-on armé le coupe-circuit du démenti ?"""

    def __init__(self) -> None:
        self.denied: list[str] = []

    async def contact_replied(self, call_id: str, *, denied: bool) -> None:
        if denied:
            self.denied.append(call_id)


class Minuteur:
    """Remplace `asyncio.sleep` : la fenêtre se ferme quand le test le décide.

    Attendre vraiment 120 s rendrait la suite inutilisable ; mettre la fenêtre à
    0 s la fermerait avant qu'on ait pu faire parler l'appelant.
    """

    def __init__(self) -> None:
        self.feu_vert = asyncio.Event()
        self.attentes: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.attentes.append(delay)
        await self.feu_vert.wait()

    def fermer_la_fenetre(self) -> None:
        self.feu_vert.set()


class RefusingSms(LoggingCallController):
    """Twilio refuse le SMS : numéro non joignable, permission géographique…"""

    async def send_sms(self, to_number: str, message: str) -> bool:
        self.calls.append(("send_sms", to_number, message))
        return False


@dataclass
class Banc:
    verifier: IdentityVerifier
    registry: SessionRegistry
    session: CallSession
    controller: LoggingCallController
    pipeline: PipelineEspion
    sink: InMemoryEventSink
    minuteur: Minuteur

    def sms(self) -> list[tuple[str, ...]]:
        return [c for c in self.controller.calls if c[0] == "send_sms"]

    def annonces(self) -> list[str]:
        return [c[2] for c in self.controller.calls if c[0] == "announce_to_victim"]

    def actions(self) -> list[dict[str, Any]]:
        return [e.data for e in self.sink.by_type(EventType.ACTION)]


async def _banc(
    *,
    caller: str = INCONNU,
    claimed: str | None = None,
    transcribe: bool = True,
    contacts: Any = None,
    controller: LoggingCallController | None = None,
) -> Banc:
    settings = Settings(_env_file=None, verify_code_ttl_seconds=120)
    sink = InMemoryEventSink()
    registry = SessionRegistry(transcriber_factory=FakeTranscriber, sink=sink, settings=settings)
    controller = controller or LoggingCallController()
    pipeline = PipelineEspion()
    minuteur = Minuteur()
    verifier = IdentityVerifier(
        registry=registry,
        controller=controller,
        contacts=contacts if contacts is not None else _carnet(),
        settings=settings,
        pipeline=pipeline,
        sleep=minuteur,
    )
    # Posé AVANT d'ouvrir l'appel, comme le fait `create_app` : le hook est
    # recopié dans la session à son ouverture, pas relu à chaque touche.
    registry.dtmf_hook = verifier.on_dtmf

    session = await registry.open(CALL_ID, caller_number=caller, transcribe=transcribe)
    if claimed is not None:
        # Ce que Claude a extrait de la conversation ; on le pose à la main pour
        # ne pas mêler ces tests à ceux du pipeline de score.
        session.signals.content = ContentSignal(risk=0.8, claimed_identity=claimed)

    return Banc(verifier, registry, session, controller, pipeline, sink, minuteur)


async def _dire(banc: Banc, speaker: str, texte: str) -> None:
    """Un segment final de transcript, comme le publie `CallSession.on_segment`."""
    await banc.verifier.emit(
        Event(
            call_id=CALL_ID,
            t=1.0,
            type=EventType.TRANSCRIPT,
            speaker=speaker,  # type: ignore[arg-type]
            data={"text": texte, "is_final": True},
        )
    )


# --- Le code lui-même -------------------------------------------------------


async def test_le_code_fait_quatre_chiffres_et_part_au_proche() -> None:
    banc = await _banc(claimed="Lucas, mon petit-fils")

    resultat = await banc.verifier.request(CALL_ID)

    assert resultat.ok is True
    assert resultat.code is not None
    assert len(resultat.code) == 4 and resultat.code.isdigit(), "4 chiffres, zéros compris"
    assert resultat.sent_to == "Lucas Martin"
    assert resultat.expires_in == 120

    # Le SMS part au VRAI numéro du carnet, avec le prénom de la personne
    # protégée (sinon le proche ne sait pas chez qui l'appel a lieu).
    assert len(banc.sms()) == 1
    assert banc.sms()[0][1] == LUCAS
    corps = banc.sms()[0][2]
    assert resultat.code in corps
    assert "Jeanne" in corps
    assert "NO" in corps, "la réponse NO réutilise le chemin existant du démenti"


async def test_mille_codes_tires_restent_a_quatre_chiffres() -> None:
    """`secrets.randbelow(10000)` tire aussi 7 et 912 : le formatage doit tenir."""
    banc = await _banc(claimed="Lucas")
    vus = set()
    for _ in range(200):
        resultat = await banc.verifier.request(CALL_ID)
        assert resultat.code is not None and len(resultat.code) == 4
        vus.add(resultat.code)
        banc.verifier.forget(CALL_ID)
    await banc.verifier.aclose()
    # Un code constant passerait tous les autres tests : on vérifie qu'il bouge.
    assert len(vus) > 50


async def test_le_code_est_annonce_chiffre_par_chiffre() -> None:
    """Sans séparation, la voix dit « quatre mille huit cent vingt et un » : la
    personne protégée entend un montant et ne reconnaît plus son code."""
    banc = await _banc(claimed="Lucas")

    resultat = await banc.verifier.request(CALL_ID)

    assert resultat.code is not None
    annonce = banc.annonces()[0]
    assert say_digits(resultat.code) in annonce
    assert "Lucas Martin" in annonce
    assert resultat.code not in annonce, "les chiffres sont séparés, pas collés"


# --- À qui part le code : les trois cas -------------------------------------


async def test_cas_1_le_numero_appelant_est_au_carnet() -> None:
    """Le cas de l'usurpation, et tout l'intérêt du lot.

    L'appel se présente avec le numéro de Lucas (c'est ce qu'un spoofer sait
    faire). On envoie le code au numéro du CARNET — le même, et c'est voulu :
    usurper un numéro d'appel ne donne pas la réception de ses SMS, donc le
    message arrive sur la SIM du vrai Lucas et l'arnaqueur ne le verra jamais.
    """
    banc = await _banc(caller=LUCAS)

    resultat = await banc.verifier.request(CALL_ID)

    assert resultat.ok is True
    assert resultat.sent_to == "Lucas Martin"
    assert banc.sms()[0][1] == LUCAS


async def test_cas_2_identite_revendiquee_qui_correspond_a_un_contact() -> None:
    """Numéro inconnu, mais Claude a entendu « c'est Lucas » : le code part chez
    le vrai Lucas, PAS chez celui qui appelle."""
    banc = await _banc(caller=INCONNU, claimed="Lucas, votre petit-fils")

    resultat = await banc.verifier.request(CALL_ID)

    assert resultat.ok is True
    assert banc.sms()[0][1] == LUCAS
    assert INCONNU not in [c[1] for c in banc.sms()]


async def test_cas_3_personne_au_carnet_annonce_et_rien_d_autre() -> None:
    banc = await _banc(caller=INCONNU, claimed=None)

    resultat = await banc.verifier.request(CALL_ID)

    assert resultat.ok is False
    assert resultat.reason == REASON_NO_CONTACT
    assert resultat.code is None
    assert banc.annonces() == [ANNOUNCE_VERIFY_NO_CONTACT]
    assert banc.sms() == [], "aucun SMS : on n'a aucun vrai téléphone vers qui l'envoyer"
    assert banc.actions() == [], "rien sur le bus non plus : il ne s'est rien passé"


async def test_une_identite_revendiquee_hors_carnet_ne_vaut_rien() -> None:
    banc = await _banc(caller=INCONNU, claimed="Maître Dubois, votre notaire")

    resultat = await banc.verifier.request(CALL_ID)

    assert resultat.ok is False
    assert resultat.reason == REASON_NO_CONTACT
    assert banc.sms() == []


# --- Jamais de SMS au numéro appelant ---------------------------------------


async def test_le_sms_ne_part_jamais_vers_un_numero_qui_ne_vient_pas_du_carnet() -> None:
    """L'invariant du lot, formulé comme il doit l'être.

    Le `From` de l'appel est ce qu'un usurpateur contrôle : il ne doit jamais
    devenir un destinataire. Seul `contact.phone_number` est passé à `send_sms`,
    et ce test le vérifie sur les deux voies de sélection.
    """
    numeros_du_carnet = {LUCAS, "+33698765432"}

    for caller, claimed in ((LUCAS, None), (INCONNU, "Lucas"), ("+33600000001", "la fille")):
        banc = await _banc(caller=caller, claimed=claimed)
        await banc.verifier.request(CALL_ID)
        destinataires = {c[1] for c in banc.sms()}
        assert destinataires <= numeros_du_carnet, f"appel de {caller} : {destinataires}"


async def test_un_contact_qui_porte_le_numero_appelant_est_refuse_par_l_identite() -> None:
    """La garde dure : arrivé par l'identité REVENDIQUÉE, le destinataire ne peut
    pas être le numéro de l'appel.

    Rien ne relie alors ce numéro au carnet — c'est l'appelant qui a choisi le
    nom — donc envoyer reviendrait à donner le code à vérifier à celui qu'on
    vérifie. Le carnet est tordu à la main parce qu'un vrai `JsonContactBook`
    aurait répondu par `by_number` avant d'arriver là.
    """

    class CarnetTordu:
        """`by_number` aveugle, `by_identity` qui renvoie le numéro appelant."""

        def by_number(self, phone_number: str) -> Contact | None:
            return None

        def by_identity(self, claimed_identity: str) -> Contact | None:
            return Contact(name="Lucas Martin", phone_number=INCONNU)

        def all(self) -> tuple[Contact, ...]:
            return ()

    banc = await _banc(caller=INCONNU, claimed="Lucas", contacts=CarnetTordu())

    resultat = await banc.verifier.request(CALL_ID)

    assert resultat.ok is False
    assert resultat.reason == REASON_CALLER_IS_TARGET
    assert banc.sms() == []
    assert banc.annonces() == [ANNOUNCE_VERIFY_SAME_NUMBER]


async def test_un_contact_sans_numero_exploitable_ne_declenche_rien() -> None:
    class CarnetVide:
        def by_number(self, phone_number: str) -> Contact | None:
            return Contact(name="Lucas Martin", phone_number="   ")

        def by_identity(self, claimed_identity: str) -> Contact | None:
            return None

        def all(self) -> tuple[Contact, ...]:
            return ()

    banc = await _banc(caller=LUCAS, contacts=CarnetVide())

    resultat = await banc.verifier.request(CALL_ID)

    assert resultat.ok is False
    assert banc.sms() == []


# --- Le code ne circule pas sur le bus --------------------------------------


async def test_le_code_ne_figure_dans_aucun_evenement_emis_par_le_service() -> None:
    """Le bus part au dashboard ET à l'app, par un WebSocket sans
    authentification derrière une URL publique : qui lirait le code pourrait le
    dicter, c'est-à-dire réussir la vérification sans le téléphone du proche.

    Portée exacte de la garantie : aucun événement **que ce service émet** ne
    porte le code. Le transcript, lui, contiendra forcément les chiffres quand le
    proche les lira à voix haute — c'est une propriété de la transcription, pas
    de la vérification, et la seule façon de l'éviter serait de ne pas
    transcrire l'appel.
    """
    banc = await _banc(claimed="Lucas")

    resultat = await banc.verifier.request(CALL_ID)
    await _dire(banc, "caller", f"alors le code c'est {resultat.code}")

    assert resultat.code is not None
    diffuse = json.dumps([e.to_wire() for e in banc.sink.events], ensure_ascii=False)
    assert resultat.code not in diffuse

    demande = banc.actions()[0]
    assert demande["action"] == "verify_requested"
    assert demande["contact"] == "Lucas Martin"
    assert demande["source"] == "app"
    assert "code" not in demande


# --- Idempotence ------------------------------------------------------------


async def test_deux_declenchements_rapproches_ne_font_qu_un_seul_code() -> None:
    """Deux clics, ou un clic puis la phrase « vérifie ».

    Un second SMS poserait au proche une question qu'il a déjà sous les yeux, et
    un second code dans l'oreille de la personne protégée rendrait la
    vérification impossible : elle ne saurait plus lequel attendre.
    """
    banc = await _banc(claimed="Lucas")

    premier = await banc.verifier.request(CALL_ID)
    second = await banc.verifier.request(CALL_ID, source="phrase")

    assert second.ok is True
    assert second.code == premier.code
    assert second.reason == REASON_ALREADY_ACTIVE
    assert len(banc.sms()) == 1
    assert len(banc.annonces()) == 1
    assert [a["action"] for a in banc.actions()] == ["verify_requested"]


async def test_un_sms_refuse_laisse_la_verification_rejouable() -> None:
    """Le code n'a jamais atteint le proche : il n'existe pas.

    Le garder « en cours » rendrait la demande suivante impossible (« déjà en
    cours ») pour un code que personne n'a reçu.
    """
    banc = await _banc(claimed="Lucas", controller=RefusingSms())

    premier = await banc.verifier.request(CALL_ID)

    assert premier.ok is False
    assert premier.reason == REASON_SMS_REFUSED
    assert banc.annonces() == [ANNOUNCE_VERIFY_SMS_FAILED]
    assert banc.actions() == []

    # Et la demande suivante repart de zéro, avec un vrai code cette fois.
    banc.controller.calls.clear()
    second = await banc.verifier.request(CALL_ID)
    assert second.reason == REASON_SMS_REFUSED, "le contrôleur refuse toujours"
    assert len(banc.sms()) == 1, "un nouveau SMS a bien été tenté"


# --- Lecture des nombres français dits à voix haute -------------------------
#
# Table généreuse : c'est elle qui décide si la vérification marche au téléphone.
# Le proche lit son SMS comme il le sent, et Transcribe écrit tantôt des mots,
# tantôt des chiffres, tantôt un mélange.


@pytest.mark.parametrize(
    ("texte", "code"),
    [
        # Chiffre par chiffre, la façon dont on dicte un code.
        ("quatre huit deux un", "4821"),
        ("quatre, huit, deux, un", "4821"),
        ("c'est le quatre huit deux un", "4821"),
        # Par paires, la façon dont on dicte un numéro.
        ("quarante-huit vingt et un", "4821"),
        ("quarante huit, vingt et un", "4821"),
        ("soixante-douze zéro trois", "7203"),
        ("quatre-vingt-dix-neuf quatre-vingt-dix-neuf", "9999"),
        ("soixante-dix-sept cinquante-six", "7756"),
        ("trente-cinq quatre-vingt-un", "3581"),
        # Lu comme un nombre entier.
        ("quatre mille huit cent vingt et un", "4821"),
        ("mille deux cent trente-quatre", "1234"),
        ("deux mille", "2000"),
        ("neuf mille neuf cent quatre-vingt-dix-neuf", "9999"),
        # Les zéros de tête doivent être prononcés.
        ("zéro quatre huit un", "0481"),
        ("zéro zéro quarante-huit", "0048"),
        # Ce que Transcribe écrit en chiffres.
        ("4821", "4821"),
        ("48 21", "4821"),
        ("le code est 4821, d'accord ?", "4821"),
        ("4 8 2 1", "4821"),
        # Mélanges — un chiffre reconnu, l'autre écrit en mot.
        ("48 vingt et un", "4821"),
        ("quarante-huit 21", "4821"),
        # Accents absents, comme les écrit souvent l'ASR.
        ("zero quatre huit un", "0481"),
    ],
)
def test_un_code_dicte_en_francais_est_reconnu(texte: str, code: str) -> None:
    assert code_reconnu(texte, code), f"« {texte} » devait valoir {code} : {codes_entendus(texte)}"


@pytest.mark.parametrize(
    ("texte", "code"),
    [
        # « quatre-vingt-un » vaut 81 en français, et rien d'autre : une lecture
        # permissive en ferait « 4-20-1 » ou « 4-21 » et accepterait un code que
        # personne n'a lu. Chaque acceptation à tort est un usurpateur validé.
        ("quatre-vingt-un", "4821"),
        ("quatre-vingt-un", "4201"),
        ("quatre-vingt-un", "0421"),
        # Un chiffre manquant, un chiffre de trop, un chiffre faux.
        ("quatre huit deux", "4821"),
        ("quatre huit deux neuf", "4821"),
        ("quarante-huit vingt-deux", "4821"),
        ("quatre cent quatre-vingt-un", "0481"),
        ("948210", "4821"),
        ("4821 ou 1357", "4821"),
        ("1357 puis 4821", "4821"),
        ("4821 ou 4821", "4821"),
        # Des mots qui contiennent des nombres sans en être.
        ("je n'ai pas reçu de message", "4821"),
        ("un instant, je cherche mes lunettes", "0001"),
        ("il y a un problème avec votre compte", "1000"),
    ],
)
def test_un_code_qui_n_a_pas_ete_lu_n_est_pas_reconnu(texte: str, code: str) -> None:
    assert not code_reconnu(texte, code), f"« {texte} » ne devait pas valoir {code}"


def test_les_mots_de_liaison_ne_deviennent_pas_des_tentatives_de_code() -> None:
    """« un » est un article avant d'être un chiffre.

    Si « j'ai un problème » produisait un candidat à quatre chiffres, chaque
    phrase de l'appelant déclencherait « ce code n'est pas le bon » dans l'oreille
    de la personne protégée.
    """
    candidats = codes_entendus("j'ai un problème, il me faut de l'argent")
    assert all(len(c) < 4 for c in candidats), candidats


def test_la_lecture_des_groupes_se_concatene_dans_l_ordre() -> None:
    assert "4821" in codes_entendus("quarante-huit vingt et un")
    assert codes_entendus("quatre-vingt-un") == ("81",)
    assert codes_entendus("quatre cent quatre-vingt-un") == ("481",)


# --- Résultats de la vérification -------------------------------------------


async def test_code_correct_annonce_et_confirme_l_identite() -> None:
    banc = await _banc(caller=LUCAS)

    resultat = await banc.verifier.request(CALL_ID)
    assert resultat.code is not None
    await _dire(banc, "caller", f"mamie c'est moi, le code c'est {say_digits(resultat.code)}")

    assert ANNOUNCE_VERIFY_OK in banc.annonces()
    verifie = [a for a in banc.actions() if a["action"] == "identity_verified"]
    assert verifie == [
        {
            "action": "identity_verified",
            "target": "caller",
            "contact": "Lucas Martin",
            "ok": True,
        }
    ]
    # Pas de démenti : c'est tout l'inverse qui vient d'être prouvé.
    assert banc.pipeline.denied == []

    # Et le minuteur est annulé : la fenêtre ne doit plus rien conclure.
    banc.minuteur.fermer_la_fenetre()
    await banc.verifier.aclose()
    assert [a["action"] for a in banc.actions()].count("verify_expired") == 0


async def test_un_code_faux_fait_repeter_sans_rien_conclure() -> None:
    """La transcription se trompe d'un chiffre régulièrement.

    Accuser sur une erreur de reconnaissance mettrait en attente un vrai
    petit-fils : on l'informe, elle fait répéter, et la conclusion attend la fin
    de la fenêtre.
    """
    banc = await _banc(caller=LUCAS)

    resultat = await banc.verifier.request(CALL_ID)
    faux = "1357" if resultat.code != "1357" else "2468"
    await _dire(banc, "caller", f"le code c'est {faux}")

    assert ANNOUNCE_VERIFY_WRONG in banc.annonces()
    assert banc.pipeline.denied == [], "rien n'est conclu sur un seul code mal entendu"
    assert [a["action"] for a in banc.actions()] == ["verify_requested"]

    # Le second essai ferme immédiatement la vérification et escalade.
    await _dire(banc, "caller", f"je répète, {faux}")
    assert banc.annonces().count(ANNOUNCE_VERIFY_WRONG) == 1
    assert banc.pipeline.denied == [CALL_ID]
    assert [a for a in banc.actions() if a["action"] == "identity_verified"][-1]["ok"] is False


async def test_un_code_faux_puis_la_fin_de_la_fenetre_escalade() -> None:
    banc = await _banc(caller=LUCAS)

    resultat = await banc.verifier.request(CALL_ID)
    faux = "1357" if resultat.code != "1357" else "2468"
    await _dire(banc, "caller", f"c'est le {faux}")
    banc.minuteur.fermer_la_fenetre()
    await banc.verifier.aclose()

    assert ANNOUNCE_VERIFY_EXPIRED in banc.annonces()
    echecs = [a for a in banc.actions() if a["action"] == "identity_verified"]
    assert echecs and echecs[-1]["ok"] is False
    # Même effet qu'un démenti du proche, par LE chemin existant.
    assert banc.pipeline.denied == [CALL_ID]


async def test_le_second_essai_correct_est_accepte() -> None:
    banc = await _banc(caller=LUCAS)
    result = await banc.verifier.request(CALL_ID)
    wrong = "1357" if result.code != "1357" else "2468"
    await _dire(banc, "caller", wrong)
    await _dire(banc, "caller", result.code)
    assert banc.pipeline.denied == []
    assert banc.actions()[-1]["action"] == "identity_verified"
    assert banc.actions()[-1]["ok"] is True
    await banc.verifier.aclose()


async def test_deux_essais_epuisent_le_budget_de_tout_l_appel() -> None:
    banc = await _banc(caller=LUCAS)
    result = await banc.verifier.request(CALL_ID)
    wrong = "1357" if result.code != "1357" else "2468"
    # Chaque liste contenant aussi le bon code est un essai refusé.
    await _dire(banc, "caller", f"{wrong} ou {result.code}")
    await _dire(banc, "caller", f"{result.code} ou {wrong}")
    assert banc.pipeline.denied == [CALL_ID]
    await _dire(banc, "caller", result.code)
    retry = await banc.verifier.request(CALL_ID)
    assert retry.ok is False and retry.reason == REASON_ATTEMPTS_EXHAUSTED
    assert retry.code is None
    assert len(banc.sms()) == 1
    assert not any(a.get("ok") for a in banc.actions() if a["action"] == "identity_verified")
    banc.minuteur.fermer_la_fenetre()
    await banc.verifier.aclose()
    assert banc.pipeline.denied == [CALL_ID]


async def test_un_code_recu_apres_expiration_ne_valide_jamais() -> None:
    banc = await _banc(caller=LUCAS)
    now = [0.0]
    banc.verifier._clock = lambda: now[0]
    result = await banc.verifier.request(CALL_ID)
    now[0] = 121.0
    await _dire(banc, "caller", result.code)
    assert banc.pipeline.denied == [CALL_ID]
    assert not any(a.get("ok") for a in banc.actions() if a["action"] == "identity_verified")
    banc.minuteur.fermer_la_fenetre()
    await banc.verifier.aclose()


async def test_deux_tentatives_concurrentes_ne_permettent_pas_un_troisieme_essai() -> None:
    banc = await _banc(caller=LUCAS)
    result = await banc.verifier.request(CALL_ID)
    wrong = "1357" if result.code != "1357" else "2468"
    await asyncio.gather(_dire(banc, "caller", wrong), _dire(banc, "caller", wrong))
    await _dire(banc, "caller", result.code)
    assert banc.pipeline.denied == [CALL_ID]
    assert len([a for a in banc.actions() if a["action"] == "identity_verified"]) == 1
    await banc.verifier.aclose()


async def test_une_fenetre_vide_sur_un_appel_ecoute_escalade() -> None:
    """On écoutait (des segments appelant sont arrivés) et le code n'est jamais
    venu : là, le silence veut dire quelque chose."""
    banc = await _banc(caller=LUCAS)

    await banc.verifier.request(CALL_ID)
    await _dire(banc, "caller", "je ne trouve pas mon téléphone, bref, l'argent")
    banc.minuteur.fermer_la_fenetre()
    await banc.verifier.aclose()

    expirations = [a for a in banc.actions() if a["action"] == "verify_expired"]
    assert expirations and expirations[-1]["ok"] is False
    assert banc.pipeline.denied == [CALL_ID]


async def test_sur_un_appel_de_confiance_la_fin_de_la_fenetre_n_accuse_personne() -> None:
    """Le faux positif qu'on refuse de créer.

    Un appel de confiance n'a aucun `<Stream>` : pas un octet d'audio n'arrive,
    donc aucun transcript, donc aucune façon de savoir si le code a été lu. La
    fenêtre ne peut que l'admettre — conclure au démenti sur un silence qu'on n'a
    pas pu écouter mettrait en attente un vrai petit-fils qui vient de lire son
    code à voix haute.
    """
    banc = await _banc(caller=LUCAS, transcribe=False)

    await banc.verifier.request(CALL_ID)
    banc.minuteur.fermer_la_fenetre()
    await banc.verifier.aclose()

    assert ANNOUNCE_VERIFY_UNHEARD in banc.annonces()
    expiration = [a for a in banc.actions() if a["action"] == "verify_expired"][-1]
    assert expiration["ok"] is False
    assert expiration["reasons"] == ["call not analyzed: the code could not be heard"]
    assert banc.pipeline.denied == [], "aucune escalade sur ce qu'on n'a pas entendu"


async def test_une_fenetre_fermee_sur_un_appel_raccroche_n_emet_rien() -> None:
    """Le minuteur se réveille après le raccrochage : il ne doit ni parler dans
    une conférence terminée, ni poser un événement après la fin de l'appel sur la
    frise du dashboard."""
    banc = await _banc(caller=LUCAS)

    await banc.verifier.request(CALL_ID)
    await banc.registry.close(CALL_ID)
    banc.controller.calls.clear()
    banc.sink.clear()
    banc.minuteur.fermer_la_fenetre()
    await banc.verifier.aclose()

    assert banc.controller.calls == []
    assert banc.sink.events == []


# --- Purge en fin d'appel ---------------------------------------------------


async def test_release_call_purge_la_verification_et_son_minuteur() -> None:
    """`lifecycle.release_call` traite le service exactement comme `pipeline` et
    `controller`, via son helper `_forget`."""
    banc = await _banc(caller=LUCAS)
    await banc.verifier.request(CALL_ID)
    banc.sink.clear()
    banc.controller.calls.clear()

    state = SimpleNamespace(
        registry=banc.registry,
        pipeline=banc.pipeline,
        controller=banc.controller,
        verifier=banc.verifier,
    )
    await release_call(state, CALL_ID)

    # Plus d'état par appel, et une seconde demande repart de zéro.
    assert banc.verifier._pending == {}

    # Le minuteur a été annulé : rien ne sort plus, même si la fenêtre se ferme.
    banc.minuteur.fermer_la_fenetre()
    await banc.verifier.aclose()
    assert banc.sink.events == []
    assert banc.controller.calls == []


async def test_release_call_est_idempotent_avec_le_service() -> None:
    banc = await _banc(caller=LUCAS)
    await banc.verifier.request(CALL_ID)
    state = SimpleNamespace(registry=banc.registry, verifier=banc.verifier)

    await release_call(state, CALL_ID)
    await release_call(state, CALL_ID)  # les deux déclencheurs arrivent souvent


# --- Déclencheurs complémentaires -------------------------------------------


async def test_la_touche_1_demande_une_verification() -> None:
    """Complément NON éprouvé : avec `<Start><Stream>` unidirectionnel, il n'est
    pas vérifié que Twilio remonte les touches (Lot 0 §5). Le câblage est testé,
    la remontée ne peut l'être qu'au téléphone."""
    banc = await _banc(caller=LUCAS)

    await banc.session.on_frame(Dtmf(stream_sid="MZ1", digit="1"))

    assert len(banc.sms()) == 1
    assert banc.actions()[0]["source"] == "dtmf"


async def test_les_autres_touches_ne_declenchent_rien() -> None:
    banc = await _banc(caller=LUCAS)

    for digit in ("0", "2", "9", "#", "*"):
        await banc.session.on_frame(Dtmf(stream_sid="MZ1", digit=digit))

    assert banc.sms() == []


async def test_une_touche_sur_un_service_en_panne_ne_casse_pas_l_appel() -> None:
    """La boucle média ne doit pas tomber pour une touche pressée par curiosité."""
    banc = await _banc(caller=LUCAS)

    class ServiceEnPanne:
        async def on_dtmf(self, call_id: str, digit: str) -> None:
            raise RuntimeError("service cassé")

    banc.session.dtmf_hook = ServiceEnPanne().on_dtmf

    await banc.session.on_frame(Dtmf(stream_sid="MZ1", digit="1"))  # ne lève pas


async def test_la_phrase_de_la_personne_protegee_declenche_la_verification() -> None:
    banc = await _banc(caller=LUCAS)

    await _dire(banc, "victim", "attends, je voudrais vérifier ton identité")

    assert len(banc.sms()) == 1
    assert banc.actions()[0]["source"] == "phrase"


@pytest.mark.parametrize(
    "phrase",
    [
        "mon code de carte c'est 1234",
        "le code de l'immeuble est 4821",
        "j'habite au code postal 75011",
        "d'accord, je te fais le virement",
    ],
)
async def test_le_mot_code_seul_ne_declenche_jamais_de_verification(phrase: str) -> None:
    """Elle dit « code » en lisant le code de sa carte, celui de l'immeuble, un
    code postal. Chaque faux déclenchement envoie un SMS au proche : « code »
    seul ne vaut donc rien, il faut un mot de vérification ou une tournure qui en
    demande un."""
    banc = await _banc(caller=LUCAS)

    await _dire(banc, "victim", phrase)

    assert banc.sms() == []
    assert banc.actions() == []


@pytest.mark.parametrize(
    "phrase",
    [
        "vérification",
        "vérifie qui c'est",
        "tu peux vérifier son identité",
        "vérifiez son identité s'il vous plaît",
        "envoie le code",
        "demande un code",
    ],
)
def test_les_tournures_qui_demandent_une_verification(phrase: str) -> None:
    assert demande_de_verification(phrase)


@pytest.mark.parametrize(
    "phrase",
    ["mon code secret", "c'est codé", "la vérité c'est autre chose", "d'accord, merci"],
)
def test_les_tournures_qui_n_en_demandent_pas(phrase: str) -> None:
    assert not demande_de_verification(phrase)


def test_un_participe_passe_declenche_quand_meme_et_c_est_assume() -> None:
    """« j'ai vérifié » se replie sur la même forme que « je vérifie ».

    Les accents tombent à la normalisation (Transcribe n'est pas fiable dessus),
    donc les distinguer demanderait une analyse grammaticale pour un gain nul :
    le coût du faux déclenchement est un SMS au proche et un code annoncé, pas
    une mise en attente. On consigne le comportement plutôt que de le découvrir
    en démo.
    """
    assert demande_de_verification("j'ai vérifié hier")


async def test_la_piste_appelant_ne_declenche_jamais_de_verification() -> None:
    """L'arnaqueur qui dit « vérification » ne doit pas pouvoir ouvrir une
    fenêtre : seule la personne protégée demande une preuve."""
    banc = await _banc(caller=LUCAS)

    await _dire(banc, "caller", "nous allons procéder à une vérification de votre compte")

    assert banc.sms() == []
    assert banc.actions() == []


async def test_les_transcripts_partiels_sont_ignores() -> None:
    banc = await _banc(caller=LUCAS)

    await banc.verifier.emit(
        Event(
            call_id=CALL_ID,
            t=1.0,
            type=EventType.TRANSCRIPT,
            speaker="victim",
            data={"text": "je voudrais une vérification", "is_final": False},
        )
    )

    assert banc.sms() == [], "un partiel bouge encore : il ne déclenche rien"


async def test_un_evenement_sans_texte_ou_d_un_autre_type_est_ignore() -> None:
    """Le service est branché dans le `FanOutSink` : une exception ici priverait
    le dashboard de tous les événements suivants."""
    banc = await _banc(caller=LUCAS)

    await banc.verifier.emit(
        Event(call_id=CALL_ID, t=0.0, type=EventType.TRANSCRIPT, data={"text": None})
    )
    await banc.verifier.emit(Event(call_id=CALL_ID, t=0.0, type=EventType.SCORE, data={}))


async def test_une_demande_sur_un_appel_inconnu_ne_fait_rien() -> None:
    banc = await _banc(caller=LUCAS)

    resultat = await banc.verifier.request("CA_jamais_vu")

    assert resultat.ok is False
    assert banc.sms() == []


async def test_un_pipeline_sans_contact_replied_ne_fait_pas_echouer_l_expiration() -> None:
    banc = await _banc(caller=LUCAS)
    banc.verifier._pipeline = object()

    await banc.verifier.request(CALL_ID)
    await _dire(banc, "caller", "je n'ai pas de téléphone sous la main")
    banc.minuteur.fermer_la_fenetre()
    await banc.verifier.aclose()

    assert ANNOUNCE_VERIFY_EXPIRED in banc.annonces()
