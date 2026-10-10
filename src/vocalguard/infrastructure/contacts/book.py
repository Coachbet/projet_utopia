"""Carnet d'adresses de la victime, chargé depuis un JSON.

C'est la seule source de vérité hors ligne du système : gratuite, instantanée,
disponible même si le réseau bloque Twilio. Elle porte le signal décisif
`identity_mismatch` (cf. `telephony/reputation.py`).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from pathlib import Path

from vocalguard.domain.models import Contact, ContactMode

logger = logging.getLogger(__name__)

_NON_DIAL = re.compile(r"[^\d+]")
_FR = "+33"


def normalize_number(raw: str) -> str:
    """Forme comparable d'un numéro de téléphone, en E.164 quand c'est possible.

    Le carnet d'adresses est saisi à la main ("06 12 34 56 78", "+33 (0)6 …") et
    Twilio renvoie de l'E.164 ("+33612345678"). Sans normalisation complète, un
    proche connu passe pour un numéro inconnu : `identity_mismatch` s'arme, le
    coupe-circuit plancher le score à 85, et on met en attente un vrai
    petit-fils. C'est exactement le faux positif que le projet veut éviter.

    Hypothèse assumée : un numéro écrit en national (commençant par 0) est
    français. C'est la limite du système — un numéro romain saisi
    "06 1234 5678" devient "+33…". La lever demanderait un indicatif de pays
    par contact, que le carnet ne porte pas.
    """
    if not raw:
        return ""
    cleaned = _NON_DIAL.sub("", raw.strip())
    if not cleaned:
        return ""
    # Un "+" ne vaut qu'en tête ; "00" est son équivalent composé.
    cleaned = cleaned[:1] + cleaned[1:].replace("+", "")

    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    elif cleaned.startswith("0") and len(cleaned) == 10:
        # National français : 0X XX XX XX XX -> +33XXXXXXXXX.
        cleaned = _FR + cleaned[1:]
    elif not cleaned.startswith(("+", "0")):
        # E.164 écrit sans son "+" ("33612345678"). Les numéros commençant par 0
        # sont exclus : ils sont nationaux ou malformés, jamais internationaux —
        # mieux vaut les laisser visiblement cassés que leur inventer un
        # indicatif plausible ("06123456789" ne doit pas devenir "+06123456789").
        cleaned = "+" + cleaned

    if cleaned.startswith(_FR + "0"):
        # Convention "+33 (0)6 …" : aucun abonné français ne commence par 0
        # après l'indicatif, donc ce 0 est le préfixe interurbain. On n'en
        # retire qu'un seul — au-delà le numéro est de toute façon malformé, et
        # le maquiller en numéro plausible serait pire que de le laisser tel quel.
        cleaned = _FR + cleaned[len(_FR) + 1 :]
    return cleaned


def _match_strength(contact: Contact, needle: str) -> tuple[int, int]:
    """Force du rapprochement entre un contact et une identité revendiquée.

    Priorité au nom plutôt qu'à la relation : plusieurs contacts peuvent être
    "petit-fils", un seul s'appelle "Lucas Martin". À priorité égale, le libellé
    le plus long gagne, pour que "Lucas" n'éclipse pas "Lucas Martin".
    """
    best = (0, 0)
    for label in (contact.name, *contact.aliases):
        if label and label.casefold() in needle:
            best = max(best, (2, len(label)))
    if contact.relation and contact.relation.casefold() in needle:
        best = max(best, (1, len(contact.relation)))
    return best


CONTACT_MODES: tuple[str, ...] = ("trusted", "analyze")
"""Les deux seuls modes de routage. Public parce que les routes d'écriture de
l'app refusent une autre valeur : une saisie faite dans l'app peut être corrigée
immédiatement par la personne, contrairement à un fichier écrit à la main."""


def _mode_of(entry: dict, name: str) -> ContactMode:
    """Lit `mode` dans une entrée JSON, en se rabattant sur le défaut documenté.

    Champ absent → `trusted` : c'est la règle (être au carnet, c'est être de
    confiance) et c'est ce qui garde le carnet d'avant le Lot 1 utilisable.

    Valeur inconnue (faute de frappe, `"Trusted"`, `"analyse"` en français) →
    `trusted` **avec un avertissement**. On aurait pu choisir `analyze`, « plus
    prudent » : ce serait prudent pour la détection et faux pour la vie privée.
    Une coquille mettrait alors la famille sous écoute en silence, alors que
    notre promesse est l'inverse. On applique donc le défaut annoncé, et on le
    dit assez fort pour que l'intention réelle soit corrigée dans le fichier.
    """
    raw = entry.get("mode")
    if raw is None:
        return "trusted"
    if raw in CONTACT_MODES:
        return raw  # type: ignore[return-value]
    logger.warning(
        "Contact %s : mode %r inconnu (attendu %s) — traité comme « trusted », "
        "donc transmis sans analyse. Corriger le carnet si l'intention était l'inverse.",
        name,
        raw,
        " ou ".join(CONTACT_MODES),
    )
    return "trusted"


def _lire(path: Path, *, consequence: str) -> tuple[tuple[Contact, ...], str] | None:
    """Lit le fichier. `None` — et non un carnet vide — quand il est inutilisable.

    Distinguer « fichier illisible » de « carnet sans contact » est tout l'objet
    de ce retour : au démarrage les deux se valent, mais `reload` doit garder
    l'état en mémoire dans le premier cas et l'écraser dans le second. Un carnet
    vidé par accident fait de chaque proche un inconnu — annonce à l'appelant et
    analyse comprises — et c'est une panne entièrement silencieuse.

    `consequence` est la phrase de journal du cas d'échec, parce qu'elle n'est
    pas la même au démarrage (« aucun contact connu ») et au rechargement
    (« l'état en mémoire est conservé »).
    """
    if not path.exists():
        logger.warning("Carnet d'adresses introuvable (%s) : %s", path, consequence)
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        entries = payload.get("contacts", payload) if isinstance(payload, dict) else payload
        contacts = tuple(
            Contact(
                name=e["name"],
                phone_number=e["phone_number"],
                relation=e.get("relation", ""),
                aliases=tuple(e.get("aliases", ())),
                mode=_mode_of(e, e["name"]),
            )
            for e in entries
            if e.get("name") and e.get("phone_number")
        )
        owner = payload.get("owner", "") if isinstance(payload, dict) else ""
    except (OSError, json.JSONDecodeError, AttributeError, TypeError, KeyError):
        # Les deux dernières familles d'erreurs comptent autant que les deux
        # premières : un JSON valide mais de mauvaise forme (`{"contacts": 3}`,
        # une entrée qui n'est pas un objet) lèverait sinon au milieu du
        # chargement, donc ferait planter `create_app` au démarrage et
        # renverrait une 500 sans rapport depuis la route qui recharge.
        logger.exception("Carnet d'adresses illisible (%s) : %s", path, consequence)
        return None
    return contacts, str(owner or "")


class JsonContactBook:
    """Implémente le port `ContactBook`.

    Mutable par `reload`, et c'est délibéré. L'objet est construit une seule fois
    par `create_app` puis partagé par cinq détenteurs — `app.state.contacts`, le
    `ScoringPipeline`, l'`IdentityVerifier`, l'enregistreur d'historique et le
    `TwilioReputationChecker`. Remplacer l'objet ne mettrait à jour que la
    première de ces références : ajouter son fils depuis l'app le laisserait
    traiter comme un inconnu jusqu'au redémarrage, sans aucun message d'erreur.
    `reload` remplace donc l'état INTERNE, en place.
    """

    def __init__(
        self,
        contacts: Sequence[Contact] = (),
        *,
        owner: str = "",
        path: str | Path | None = None,
    ) -> None:
        # Mémorisé pour que `reload()` sans argument sache quoi relire. Les
        # carnets construits en mémoire (doublures, rejeu) n'en ont pas.
        self._path = Path(path) if path is not None else None
        self._adopter(tuple(contacts), owner)

    def _adopter(self, contacts: tuple[Contact, ...], owner: str) -> None:
        """Installe un état complet : les trois attributs changent ensemble.

        L'index est reconstruit à neuf plutôt que mis à jour : un contact retiré
        du fichier doit disparaître de `_by_number`, sinon il reste « de
        confiance » et son appel continue de passer sans analyse — le sens
        dangereux d'un retrait qui n'aurait pas pris.
        """
        self._contacts = contacts
        self._owner = owner
        index: dict[str, Contact] = {}
        for contact in contacts:
            key = normalize_number(contact.phone_number)
            # Une clé vide ferait passer un appelant sans numéro pour un contact
            # connu, donc ferait *baisser* le score à tort : le sens dangereux.
            if key:
                index[key] = contact
        self._by_number = index

    @classmethod
    def from_path(cls, path: str | Path) -> JsonContactBook:
        """Charge le carnet. Un fichier absent ne doit pas casser la démo."""
        p = Path(path)
        charge = _lire(p, consequence="aucun contact connu")
        if charge is None:
            # Le chemin est retenu malgré l'échec : le fichier peut naître d'un
            # premier ajout depuis l'app, et `reload()` doit alors le trouver.
            return cls(path=p)
        contacts, owner = charge
        return cls(contacts, owner=owner, path=p)

    def reload(self, path: str | Path | None = None) -> bool:
        """Relit le fichier et remplace l'état interne en place. `False` si échec.

        Appelée après chaque écriture du carnet depuis l'app : sans elle, le
        fichier change et les cinq détenteurs continuent de répondre avec le
        carnet du démarrage.

        Un échec ne vide jamais la mémoire. Perdre le carnet en pleine démo
        transformerait tous les proches en inconnus, donc les ferait annoncer et
        analyser : on préfère un carnet périmé, qui reste juste pour tous les
        contacts déjà connus, à un carnet vide.
        """
        cible = Path(path) if path is not None else self._path
        if cible is None:
            logger.warning(
                "Rechargement du carnet impossible : ce carnet a été construit en "
                "mémoire, sans fichier d'origine. L'état en mémoire est conservé."
            )
            return False
        charge = _lire(cible, consequence="l'état en mémoire est conservé")
        if charge is None:
            return False
        self._path = cible
        self._adopter(*charge)
        return True

    @property
    def owner(self) -> str:
        """Nom de la personne protégée, tel que le carnet le déclare.

        Sert au SMS de vérification : « si c'est bien toi qui appelles Jeanne en
        ce moment… ». Un prénom rend le message immédiatement compréhensible pour
        le proche qui le reçoit ; sans lui, le SMS parle d'un appel sans dire
        chez qui. Chaîne vide si le carnet ne le déclare pas — l'appelant doit
        alors formuler sa phrase sans nom, pas inventer un nom."""
        return self._owner

    def by_number(self, phone_number: str) -> Contact | None:
        return self._by_number.get(normalize_number(phone_number))

    def by_identity(self, claimed_identity: str) -> Contact | None:
        needle = (claimed_identity or "").strip().casefold()
        if not needle:
            return None
        best: Contact | None = None
        best_strength = (0, 0)
        for contact in self._contacts:
            if not contact.matches_identity(claimed_identity):
                continue
            strength = _match_strength(contact, needle)
            if strength > best_strength:
                best_strength, best = strength, contact
        return best

    def all(self) -> Sequence[Contact]:
        return self._contacts
