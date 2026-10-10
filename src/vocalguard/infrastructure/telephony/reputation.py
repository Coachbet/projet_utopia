"""Signal de réputation : que vaut la ligne d'où vient cet appel ?

Deux sources composées :
  - le carnet d'adresses, hors ligne, gratuit, toujours disponible ;
  - Twilio Lookup v2 `line_type_intelligence`, facturé à la requête.

Le carnet porte le signal fort. Lookup n'ajoute qu'une nuance sur le type de
ligne, donc le système reste utilisable avec `TWILIO_LOOKUP_ENABLED=false`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from vocalguard.config import Settings, get_settings
from vocalguard.domain.models import Contact, LineType, ReputationSignal
from vocalguard.domain.ports import ContactBook
from vocalguard.infrastructure.contacts.book import JsonContactBook, normalize_number

logger = logging.getLogger(__name__)

LookupFn = Callable[[str], str | None]
"""Numéro E.164 -> valeur brute `line_type_intelligence.type`. Synchrone."""


# Risque de base d'un numéro **inconnu** du carnet, par type de ligne.
# Le VoIP non fixe domine : c'est la ligne jetable à quelques centimes des
# campagnes d'arnaque, et jamais le téléphone personnel d'un vrai proche.
_UNKNOWN_NUMBER_RISK = {
    LineType.NON_FIXED_VOIP: 0.75,
    LineType.FIXED_VOIP: 0.45,
    LineType.TOLL_FREE: 0.40,
    LineType.MOBILE: 0.35,
    LineType.PERSONAL: 0.35,
    LineType.UNKNOWN: 0.35,
    LineType.LANDLINE: 0.25,
}

KNOWN_CONTACT_RISK = 0.05
"""Un numéro présent dans le carnet : le type de ligne ne le rend pas suspect.
Le carnet est la vérité terrain de la victime, on lui fait confiance."""

IDENTITY_MISMATCH_RISK = 0.95
"""L'appelant se dit Lucas mais Lucas a un autre numéro. Signal quasi décisif :
il alimente le coupe-circuit « paiement + identité » qui force le score à 85."""


def _line_type_reason(line_type: LineType) -> str | None:
    if line_type is LineType.NON_FIXED_VOIP:
        return "ligne VoIP non fixe"
    if line_type is LineType.FIXED_VOIP:
        return "ligne VoIP"
    if line_type is LineType.TOLL_FREE:
        return "numéro surtaxé ou gratuit"
    return None


class TwilioReputationChecker:
    """Implémente le port `ReputationChecker`."""

    def __init__(
        self,
        contacts: ContactBook | None = None,
        *,
        settings: Settings | None = None,
        lookup: LookupFn | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._contacts = contacts or JsonContactBook.from_path(self._settings.contacts_path)
        self._lookup = lookup
        # Cache de *tâches*, pas de valeurs : deux `check()` concurrents sur le
        # même numéro ne doivent déclencher qu'une seule requête facturée.
        self._line_types: dict[str, asyncio.Task[LineType]] = {}

    # --- Lookup ------------------------------------------------------------

    def _twilio_lookup(self, number: str) -> str | None:
        from twilio.rest import Client

        client = Client(self._settings.twilio_account_sid, self._settings.twilio_auth_token)
        info = client.lookups.v2.phone_numbers(number).fetch(fields="line_type_intelligence")
        raw = info.line_type_intelligence
        # Le SDK type ce champ `str` mais l'API renvoie un objet.
        if isinstance(raw, dict):
            return raw.get("type")
        return raw

    async def _fetch_line_type(self, number: str) -> LineType:
        """Ne lève jamais : un Lookup indisponible dégrade en UNKNOWN."""
        fn = self._lookup or self._twilio_lookup
        try:
            raw = await asyncio.to_thread(fn, number)
        except Exception:
            logger.warning("Lookup indisponible pour %s, réputation dégradée", number)
            return LineType.UNKNOWN
        try:
            return LineType(raw)
        except ValueError:
            return LineType.UNKNOWN

    async def _line_type(self, number: str) -> LineType:
        if not self._settings.twilio_lookup_enabled:
            return LineType.UNKNOWN
        task = self._line_types.get(number)
        if task is None:
            # La tâche est enregistrée avant tout `await` : deux appels
            # concurrents sur le même numéro partagent la même requête.
            task = asyncio.create_task(self._fetch_line_type(number))
            self._line_types[number] = task
        return await task

    # --- Signal ------------------------------------------------------------

    def _mismatch(self, number: str, claimed_identity: str | None) -> tuple[bool, Contact | None]:
        """Conservateur : mismatch seulement si le carnet *contredit* l'appelant.

        Une identité revendiquée absente du carnet n'est pas un mismatch — nous
        n'avons alors aucune vérité terrain à opposer, seulement une ignorance.
        """
        if not claimed_identity:
            return False, None
        contact = self._contacts.by_identity(claimed_identity)
        if contact is None:
            return False, None
        differs = normalize_number(contact.phone_number) != normalize_number(number)
        return differs, contact

    async def check(
        self, phone_number: str, *, claimed_identity: str | None = None
    ) -> ReputationSignal:
        number = normalize_number(phone_number)
        line_type = await self._line_type(number)
        known = self._contacts.by_number(number)
        mismatch, claimed_contact = self._mismatch(number, claimed_identity)

        reasons: list[str] = []
        if known is not None:
            risk = KNOWN_CONTACT_RISK
        else:
            risk = _UNKNOWN_NUMBER_RISK[line_type]
            reasons.append("numéro inconnu")
            if (reason := _line_type_reason(line_type)) is not None:
                reasons.append(reason)

        if mismatch and claimed_contact is not None:
            # Composition par max, pas par somme : le signal dominant décide et
            # le risque reste dans [0, 1].
            risk = max(risk, IDENTITY_MISMATCH_RISK)
            if line_type is LineType.NON_FIXED_VOIP:
                risk = 1.0
            reasons.append(f"numéro différent de celui de {claimed_contact.name}")

        return ReputationSignal(
            risk=round(risk, 3),
            line_type=line_type,
            in_contacts=known is not None,
            identity_mismatch=mismatch,
            reasons=tuple(reasons),
        )
