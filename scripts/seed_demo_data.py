"""Remplit l'app avec du contenu de démonstration crédible.

Pourquoi ce script existe. Une démo sur écran vide ne montre rien : l'historique
sans appel, les favoris sans proche et la messagerie sans message donnent une app
qui a l'air cassée alors qu'elle fonctionne. Ce script écrit donc des appels, des
favoris et des messages PLAUSIBLES — un vrai petit-fils, un faux conseiller
bancaire, un numéro luxembourgeois qui a déclenché une intervention — pour que
chaque écran raconte quelque chose.

Ce qu'il faut savoir avant de s'en servir en public :

  - il écrit dans `data/calls.sqlite`, `data/voicemail.sqlite` et
    `data/contacts.json`. Rien n'est effacé sans `--reset` ;
  - À LANCER AVANT `make run`, ou redémarrer le serveur après. Le carnet
    d'adresses est chargé UNE fois au démarrage et partagé par cinq composants
    (cf. `JsonContactBook.reload`) : un serveur déjà lancé continuerait de servir
    les favoris d'avant le seed, et seule l'app saurait les relire en écrivant un
    contact. L'historique et la messagerie, eux, sont relus à chaque requête ;
  - les messages vocaux apparaissent dans la liste mais NE SE LISENT PAS. Leur
    audio est téléchargé chez Twilio à partir d'un `recording_sid` réel, qu'un
    script local ne peut pas inventer. Appuyer sur « Play » affichera une erreur.
    Utiliser `--no-voicemail` pour une démo où l'on montre la lecture ;
  - les données sont datées par rapport à MAINTENANT, donc l'historique dit
    « Today » et « Yesterday » quel que soit le jour de la démo.

    uv run python scripts/seed_demo_data.py [--reset] [--no-voicemail]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

from vocalguard.config import get_settings
from vocalguard.domain.history import CallRecord, caller_origin
from vocalguard.infrastructure.history.sqlite import SQLiteCallHistory
from vocalguard.infrastructure.history.voicemail import SQLiteVoicemailStore

# Les proches épinglés. Ce sont des noms DÉJÀ présents dans `data/contacts.json` :
# inventer un favori qui ne serait pas au carnet afficherait une ligne que
# l'onglet Contacts ne connaît pas.
FAVORIS = ("Lucas Martin", "Camille Martin", "Docteur Laurent")


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _type_de_ligne(number: str) -> str:
    """Ce que Twilio aurait répondu sur la nature de la ligne.

    `unknown` pour l'étranger, et c'est fidèle : la démo ne doit pas afficher
    « Mobile · Luxembourg » sur un numéro dont on n'a rien demandé.
    """
    if number.startswith(("+336", "+337")):
        return "mobile"
    if number.startswith("+33"):
        return "landline"
    return "unknown"


def _appels(maintenant: datetime) -> list[CallRecord]:
    """Les appels de la démo, du plus ancien au plus récent.

    Le mélange est voulu : des appels de proches qui passent sans analyse, des
    inconnus anodins, et trois arnaques de gravité croissante. Un historique
    uniquement composé d'arnaques ne démontrerait rien — c'est le contraste qui
    rend lisible le travail de la protection.
    """

    def heures(h: float) -> datetime:
        return maintenant - timedelta(hours=h)

    plan: list[dict[str, object]] = [
        # --- Une vraie arnaque, interrompue. Le cas de démonstration. ---
        {
            "at": heures(2.5),
            "number": "+33185642270",
            "name": "",
            "route": "analyzed",
            "duration": 184.0,
            "answered": True,
            "score": 93,
            "level": "intervene",
            "watch": 22.0,
            "alert": 61.0,
            "intervene": 118.0,
            "llm": 0.94,
            "keywords": 0.71,
            "voice": 0.66,
            "reputation": 0.52,
            "tactics": [
                "usurpation d'identité bancaire",
                "urgence",
                "demande de code à usage unique",
            ],
            "explanation": (
                "L'appelant se présente comme le service anti-fraude de la banque et "
                "réclame le code reçu par SMS pour « bloquer un virement ». Aucune "
                "banque ne demande ce code. L'appel a été mis en attente puis arrêté."
            ),
            "actions": ["caller_notice", "hold_caller", "protected_alert"],
            "verification": "failed",
        },
        # --- Le petit-fils, liste blanche : ni annonce, ni analyse. ---
        {
            "at": heures(5),
            "number": "+33672019048",
            "name": "Lucas Martin",
            "relation": "petit-fils",
            "route": "trusted_passthrough",
            "duration": 412.0,
            "answered": True,
            "trusted": True,
        },
        # --- Un démarchage pénible mais pas dangereux. ---
        {
            "at": heures(7.5),
            "number": "+33970123456",
            "name": "",
            "route": "analyzed",
            "duration": 48.0,
            "answered": True,
            "score": 44,
            "level": "watch",
            "watch": 19.0,
            "llm": 0.41,
            "keywords": 0.33,
            "reputation": 0.3,
            "tactics": ["démarchage insistant"],
            "explanation": (
                "Démarchage pour des travaux de rénovation. Insistant, mais aucune "
                "demande d'argent ni de code : la protection a surveillé sans agir."
            ),
            "actions": ["caller_notice"],
        },
        # --- Un appel manqué d'un numéro étranger. ---
        {
            "at": heures(9),
            "number": "+352621574348",
            "name": "",
            "route": "analyzed",
            "duration": None,
            "answered": False,
        },
        # --- Appel sortant vers le médecin : jamais analysé. ---
        {
            "at": heures(26),
            "number": "+33145678901",
            "name": "Docteur Laurent",
            "relation": "médecin",
            "route": "trusted_passthrough",
            "direction": "outgoing",
            "duration": 126.0,
            "answered": True,
            "trusted": True,
        },
        # --- Faux support technique, alerte sans interruption. ---
        {
            "at": heures(29),
            "number": "+33176548890",
            "name": "",
            "route": "analyzed",
            "duration": 203.0,
            "answered": True,
            "score": 78,
            "level": "alert",
            "watch": 31.0,
            "alert": 88.0,
            "llm": 0.81,
            "keywords": 0.58,
            "voice": 0.44,
            "reputation": 0.41,
            "tactics": ["faux support technique", "prise de contrôle à distance"],
            "explanation": (
                "L'appelant prétend que l'ordinateur est infecté et demande "
                "d'installer un logiciel de prise en main à distance. La personne "
                "protégée a été alertée et a raccroché d'elle-même."
            ),
            "actions": ["caller_notice", "protected_alert"],
        },
        # --- La fille, liste blanche. ---
        {
            "at": heures(32),
            "number": "+33698765432",
            "name": "Camille Martin",
            "relation": "fille",
            "route": "trusted_passthrough",
            "duration": 735.0,
            "answered": True,
            "trusted": True,
        },
        # --- Vérification d'identité réussie : un proche qui appelle d'ailleurs. ---
        {
            "at": heures(50),
            "number": "+33755014477",
            "name": "",
            "route": "analyzed",
            "duration": 97.0,
            "answered": True,
            "score": 58,
            "level": "watch",
            "watch": 26.0,
            "llm": 0.52,
            "keywords": 0.21,
            "reputation": 0.38,
            "tactics": ["identité à vérifier"],
            "explanation": (
                "L'appelant se dit être Thomas, le fils, depuis un numéro inconnu. "
                "Le code de vérification a été lu correctement : c'était bien lui."
            ),
            "actions": ["caller_notice", "identity_check"],
            "verification": "verified",
        },
        # --- Un manqué anodin. ---
        {
            "at": heures(55),
            "number": "+33188776655",
            "name": "",
            "route": "analyzed",
            "duration": None,
            "answered": False,
        },
        # --- La voisine, liste blanche, appel court. ---
        {
            "at": heures(74),
            "number": "+33156789012",
            "name": "Madame Bernard",
            "relation": "voisine",
            "route": "trusted_passthrough",
            "duration": 64.0,
            "answered": True,
            "trusted": True,
        },
        # --- Arnaque au faux neveu, interrompue elle aussi. ---
        {
            "at": heures(99),
            "number": "+33644112200",
            "name": "",
            "route": "analyzed",
            "duration": 221.0,
            "answered": True,
            "score": 88,
            "level": "intervene",
            "watch": 18.0,
            "alert": 54.0,
            "intervene": 131.0,
            "llm": 0.9,
            "keywords": 0.63,
            "voice": 0.72,
            "reputation": 0.33,
            "tactics": ["faux proche", "urgence", "virement immédiat"],
            "explanation": (
                "L'appelant se présente comme un neveu accidenté à l'étranger et "
                "réclame un virement immédiat. Il a refusé la vérification "
                "d'identité. L'appel a été mis en attente puis arrêté."
            ),
            "actions": ["caller_notice", "identity_check", "hold_caller", "protected_alert"],
            "verification": "failed",
        },
    ]

    records: list[CallRecord] = []
    for entree in plan:
        debut: datetime = entree["at"]  # type: ignore[assignment]
        duree = entree.get("duration")
        number = str(entree["number"])
        origine, pays = caller_origin(number, is_contact=bool(entree.get("trusted")))
        records.append(
            CallRecord(
                # `uuid4` et non un identifiant stable : rejouer le script doit
                # ajouter des appels, pas réécrire silencieusement les mêmes.
                call_id="CAdemo" + uuid.uuid4().hex[:26],
                started_at=_iso(debut),
                caller_number=number,
                caller_name=str(entree.get("name", "")),
                relation=str(entree.get("relation", "")),
                origin=origine,
                country=pays,
                line_type=str(entree.get("line_type", _type_de_ligne(number))),
                route=entree.get("route", "analyzed"),  # type: ignore[arg-type]
                direction=entree.get("direction", "incoming"),  # type: ignore[arg-type]
                ended_at=_iso(debut + timedelta(seconds=float(duree))) if duree else None,
                duration_s=float(duree) if duree else None,
                answered=bool(entree.get("answered")),
                max_score=entree.get("score"),  # type: ignore[arg-type]
                max_level=entree.get("level"),  # type: ignore[arg-type]
                watch_at_s=entree.get("watch"),  # type: ignore[arg-type]
                alert_at_s=entree.get("alert"),  # type: ignore[arg-type]
                intervene_at_s=entree.get("intervene"),  # type: ignore[arg-type]
                peak_llm=entree.get("llm"),  # type: ignore[arg-type]
                peak_keywords=entree.get("keywords"),  # type: ignore[arg-type]
                peak_voice=entree.get("voice"),  # type: ignore[arg-type]
                peak_reputation=entree.get("reputation"),  # type: ignore[arg-type]
                tactics=list(entree.get("tactics", [])),  # type: ignore[arg-type]
                explanation=str(entree.get("explanation", "")),
                actions=list(entree.get("actions", [])),  # type: ignore[arg-type]
                verification=entree.get("verification", "none"),  # type: ignore[arg-type]
            )
        )
    return records


def _epingler_favoris(path, reset: bool) -> int:
    """Écrit `favorite` dans le carnet, sans toucher au reste du fichier.

    Le carnet porte `owner`, des notes et des contacts désactivés que le
    chargeur ignore : on relit donc tout et on ne remplace que la clé visée.
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    entrees = document.get("contacts", []) if isinstance(document, dict) else []
    touches = 0
    for entree in entrees:
        voulu = entree.get("name") in FAVORIS
        if reset and not voulu:
            if entree.pop("favorite", None) is not None:
                touches += 1
            continue
        if voulu and not entree.get("favorite"):
            entree["favorite"] = True
            touches += 1
    path.write_text(
        json.dumps({**document, "contacts": entrees}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return touches


async def _messages(store: SQLiteVoicemailStore, maintenant: datetime) -> int:
    """Insère des messages vocaux directement en base.

    `expect` + `capture` est le chemin normal, mais il part d'un appel Twilio en
    cours : hors appel il n'y a ni `call_id` ni `recording_sid` à lui donner. On
    écrit donc la ligne telle que `list_messages` la relira.
    """
    plan = [
        ("+33672019048", "Lucas Martin", 1.5, 34, False),
        ("+33185642270", "", 20.0, 52, False),
        ("+33698765432", "Camille Martin", 47.0, 18, True),
    ]
    with sqlite3.connect(store.path) as db:
        for number, name, il_y_a_h, duree, ecoute in plan:
            db.execute(
                "INSERT OR IGNORE INTO voicemail (call_id, id, recording_sid, "
                "caller_number, caller_name, created_at, duration_s, listened, deleted) "
                "VALUES (?,?,?,?,?,?,?,?,0)",
                (
                    "CAdemo" + uuid.uuid4().hex[:26],
                    uuid.uuid4().hex[:16],
                    # Pas de SID Twilio réel : la lecture échouera, c'est écrit
                    # dans l'en-tête du script.
                    "REdemo" + uuid.uuid4().hex[:26],
                    number,
                    name,
                    _iso(maintenant - timedelta(hours=il_y_a_h)),
                    duree,
                    int(ecoute),
                ),
            )
    return len(plan)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="efface l'historique et les messages existants avant d'écrire",
    )
    parser.add_argument(
        "--no-voicemail",
        action="store_true",
        help="n'écrit pas de messages vocaux (leur lecture échoue sans Twilio)",
    )
    args = parser.parse_args()

    settings = get_settings()
    # La rétention réelle du serveur, pas une valeur à nous : `initialize()`
    # purge ce qui la dépasse, et seeder avec une fenêtre plus large écrirait
    # des appels que le prochain démarrage de l'app effacerait aussitôt.
    history = SQLiteCallHistory(
        settings.history_path,
        enabled=True,
        retention_days=settings.history_retention_days,
    )
    await history.initialize()
    if args.reset:
        print(f"historique  : {await history.clear()} appel(s) effacé(s)")

    maintenant = datetime.now(UTC)
    appels = _appels(maintenant)
    for record in appels:
        await history.save(record)
    print(f"historique  : {len(appels)} appels écrits")

    favoris = _epingler_favoris(settings.contacts_path, args.reset)
    print(f"favoris     : {favoris} contact(s) modifié(s) → {', '.join(FAVORIS)}")

    if args.no_voicemail:
        print("messagerie  : ignorée (--no-voicemail)")
        return
    mailbox = SQLiteVoicemailStore(settings.voicemail_path, retention_days=30)
    await mailbox.initialize()
    if args.reset:
        with sqlite3.connect(settings.voicemail_path) as db:
            efface = db.execute("DELETE FROM voicemail").rowcount
        print(f"messagerie  : {efface} message(s) effacé(s)")
    print(f"messagerie  : {await _messages(mailbox, maintenant)} messages écrits")
    print("\nATTENTION : les messages vocaux s'affichent mais ne se lisent pas —")
    print("leur audio vient de Twilio, qu'un script local ne peut pas simuler.")


if __name__ == "__main__":
    asyncio.run(main())
