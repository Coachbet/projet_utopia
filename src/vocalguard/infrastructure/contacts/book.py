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

from vocalguard.domain.models import Contact

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


class JsonContactBook:
    """Implémente le port `ContactBook`."""

    def __init__(self, contacts: Sequence[Contact] = ()) -> None:
        self._contacts = tuple(contacts)
        self._by_number: dict[str, Contact] = {}
        for contact in self._contacts:
            key = normalize_number(contact.phone_number)
            # Une clé vide ferait passer un appelant sans numéro pour un contact
            # connu, donc ferait *baisser* le score à tort : le sens dangereux.
            if key:
                self._by_number[key] = contact

    @classmethod
    def from_path(cls, path: str | Path) -> JsonContactBook:
        """Charge le carnet. Un fichier absent ne doit pas casser la démo."""
        p = Path(path)
        if not p.exists():
            logger.warning("Carnet d'adresses introuvable (%s) : aucun contact connu", p)
            return cls()
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("Carnet d'adresses illisible (%s) : aucun contact connu", p)
            return cls()

        entries = payload.get("contacts", payload) if isinstance(payload, dict) else payload
        contacts = [
            Contact(
                name=e["name"],
                phone_number=e["phone_number"],
                relation=e.get("relation", ""),
                aliases=tuple(e.get("aliases", ())),
            )
            for e in entries
            if e.get("name") and e.get("phone_number")
        ]
        return cls(contacts)

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
