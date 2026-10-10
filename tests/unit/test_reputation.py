"""Signal de réputation et carnet d'adresses. Aucun accès réseau : Lookup v2 est
facturé à la requête, on injecte toujours une fausse fonction de lookup.
"""

from __future__ import annotations

import json

import pytest

from vocalguard.config import Settings
from vocalguard.domain.models import Contact, LineType
from vocalguard.infrastructure.contacts.book import JsonContactBook, normalize_number
from vocalguard.infrastructure.telephony.reputation import TwilioReputationChecker

LUCAS = Contact(
    name="Lucas Martin",
    phone_number="+33612345678",
    relation="petit-fils",
    aliases=("Lucas",),
)
CAMILLE = Contact(name="Camille", phone_number="06 987 654 32", relation="fille")
SCAMMER = "+13105550123"


class FakeLookup:
    """Fausse fonction de lookup qui compte ses appels."""

    def __init__(self, line_type: str | None = "mobile", raises: bool = False) -> None:
        self.line_type = line_type
        self.raises = raises
        self.calls: list[str] = []

    def __call__(self, number: str) -> str | None:
        self.calls.append(number)
        if self.raises:
            raise RuntimeError("Lookup indisponible")
        return self.line_type


def make_checker(
    lookup: FakeLookup | None = None,
    *,
    enabled: bool = True,
    contacts: tuple[Contact, ...] = (LUCAS, CAMILLE),
) -> TwilioReputationChecker:
    return TwilioReputationChecker(
        JsonContactBook(contacts),
        settings=Settings(twilio_lookup_enabled=enabled),
        lookup=lookup or FakeLookup(),
    )


# --- Normalisation ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("06 12 34 56 78", "+33612345678"),
        ("+33 6 12 34 56 78", "+33612345678"),
        ("06.12.34.56.78", "+33612345678"),
        ("(06) 12-34-56-78", "+33612345678"),
        ("0033612345678", "+33612345678"),
        ("+13105550123", "+13105550123"),
        ("", ""),
    ],
)
def test_normalize_number(raw: str, expected: str) -> None:
    assert normalize_number(raw) == expected


# --- Carnet d'adresses -----------------------------------------------------


def test_book_from_path(tmp_path) -> None:
    path = tmp_path / "contacts.json"
    path.write_text(
        json.dumps(
            {
                "contacts": [
                    {
                        "name": "Lucas Martin",
                        "phone_number": "+33612345678",
                        "relation": "petit-fils",
                        "aliases": ["Lucas"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    book = JsonContactBook.from_path(path)
    assert len(book.all()) == 1
    assert book.by_number("06 12 34 56 78").name == "Lucas Martin"


def test_book_accepts_bare_list(tmp_path) -> None:
    path = tmp_path / "contacts.json"
    path.write_text(
        json.dumps([{"name": "Lucas", "phone_number": "+33612345678"}]), encoding="utf-8"
    )
    assert len(JsonContactBook.from_path(path).all()) == 1


def test_book_missing_file_is_empty(tmp_path) -> None:
    """Un carnet absent ne doit pas casser la démo."""
    book = JsonContactBook.from_path(tmp_path / "nope.json")
    assert book.all() == ()
    assert book.by_number("+33612345678") is None


def test_book_unreadable_file_is_empty(tmp_path) -> None:
    path = tmp_path / "contacts.json"
    path.write_text("{ pas du json", encoding="utf-8")
    assert JsonContactBook.from_path(path).all() == ()


def test_by_identity_prefers_longest_name() -> None:
    """« Lucas » ne doit pas éclipser « Lucas Martin »."""
    lucas_court = Contact(name="Lucas", phone_number="+33600000000")
    book = JsonContactBook((lucas_court, LUCAS))
    assert book.by_identity("c'est Lucas Martin").name == "Lucas Martin"


def test_by_identity_prefers_name_over_relation() -> None:
    """Plusieurs contacts peuvent être « fille », un seul s'appelle Camille."""
    autre_fille = Contact(name="Sophie", phone_number="+33611111111", relation="fille")
    book = JsonContactBook((autre_fille, CAMILLE))
    assert book.by_identity("Camille, ta fille").name == "Camille"


def test_by_identity_unknown_returns_none() -> None:
    assert JsonContactBook((LUCAS, CAMILLE)).by_identity("Commandant Dupont") is None


# --- Signal de réputation --------------------------------------------------


async def test_known_contact_from_own_number_is_low() -> None:
    signal = await make_checker().check("06 12 34 56 78")
    assert signal.in_contacts is True
    assert signal.identity_mismatch is False
    assert signal.risk <= 0.1
    assert signal.reasons == ()


async def test_known_contact_normalized_form() -> None:
    """Le carnet dit « 06 987 654 32 », Twilio dira « +33698765432 »."""
    signal = await make_checker().check("+33698765432")
    assert signal.in_contacts is True


async def test_real_grandson_calling_is_not_a_mismatch() -> None:
    signal = await make_checker().check("+33612345678", "Lucas, petit-fils")
    assert signal.identity_mismatch is False
    assert signal.risk <= 0.1


async def test_identity_mismatch_dominates() -> None:
    """L'appelant se dit Lucas, mais Lucas a un autre numéro."""
    signal = await make_checker().check(SCAMMER, "Lucas, votre petit-fils")
    assert signal.identity_mismatch is True
    assert signal.risk >= 0.9
    assert "numéro différent de celui de Lucas Martin" in signal.reasons


async def test_unknown_claimed_identity_is_not_a_mismatch() -> None:
    """Conservateur : sans vérité terrain, on ne contredit personne."""
    signal = await make_checker().check(SCAMMER, "Commandant Dupont, police nationale")
    assert signal.identity_mismatch is False
    assert signal.risk < 0.9


async def test_non_fixed_voip_worse_than_mobile() -> None:
    voip = await make_checker(FakeLookup("nonFixedVoip")).check(SCAMMER)
    mobile = await make_checker(FakeLookup("mobile")).check(SCAMMER)
    assert voip.line_type is LineType.NON_FIXED_VOIP
    assert voip.risk > mobile.risk
    assert "ligne VoIP non fixe" in voip.reasons
    assert "numéro inconnu" in mobile.reasons


async def test_mismatch_on_voip_is_maximal() -> None:
    checker = make_checker(FakeLookup("nonFixedVoip"))
    signal = await checker.check(SCAMMER, "Lucas")
    assert signal.risk == 1.0


async def test_lookup_disabled_skips_network() -> None:
    """Lookup est facturé : coupé, on ne doit pas émettre la requête."""
    fake = FakeLookup("nonFixedVoip")
    signal = await make_checker(fake, enabled=False).check(SCAMMER)
    assert fake.calls == []
    assert signal.line_type is LineType.UNKNOWN


async def test_lookup_failure_degrades_to_unknown() -> None:
    signal = await make_checker(FakeLookup(raises=True)).check(SCAMMER)
    assert signal.line_type is LineType.UNKNOWN
    assert signal.in_contacts is False


async def test_unexpected_line_type_degrades_to_unknown() -> None:
    signal = await make_checker(FakeLookup("sometrhingNew")).check(SCAMMER)
    assert signal.line_type is LineType.UNKNOWN


async def test_line_type_cached_per_number() -> None:
    """Le pipeline rappelle `check` à chaque nouvelle identité revendiquée :
    une seule requête facturée doit partir."""
    fake = FakeLookup("nonFixedVoip")
    checker = make_checker(fake)
    first = await checker.check(SCAMMER)
    second = await checker.check(SCAMMER, "Lucas")
    assert len(fake.calls) == 1
    assert first.line_type is second.line_type is LineType.NON_FIXED_VOIP
    assert second.identity_mismatch is True
