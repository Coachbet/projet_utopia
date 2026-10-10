"""Métadonnées d'appel persistables : aucun audio, transcript ni secret."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Literal

HistoryRoute = Literal["trusted_passthrough", "analyzed"]
CallDirection = Literal["incoming", "outgoing"]
VerificationResult = Literal["none", "verified", "failed", "expired"]


def utc_iso(value: datetime | None = None) -> str:
    """Format UTC uniforme pour les tris SQLite et les clients mobiles."""
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat()


# Indicatifs nationaux non partagés. Les zones à indicatif partagé (+1, +7,
# +262…) restent sans pays plutôt que de présenter une déduction comme un fait.
_COUNTRY_PREFIXES = {
    "33": "FR",
    "44": "GB",
    "32": "BE",
    "49": "DE",
    "34": "ES",
    "41": "CH",
    "31": "NL",
    "351": "PT",
    "352": "LU",
    "353": "IE",
    "354": "IS",
    "358": "FI",
    "45": "DK",
    "46": "SE",
    "47": "NO",
    "48": "PL",
    "43": "AT",
    "30": "GR",
    "376": "AD",
    "377": "MC",
    "212": "MA",
    "213": "DZ",
    "216": "TN",
    "221": "SN",
    "225": "CI",
    "27": "ZA",
    "81": "JP",
    "82": "KR",
    "86": "CN",
    "91": "IN",
    "55": "BR",
    "52": "MX",
    "54": "AR",
    "64": "NZ",
    "441481": "GG",
    "441534": "JE",
    "441624": "IM",
    "35818": "AX",
    # Quelques zones NANP identifiables ; +1 seul et les numéros gratuits
    # restent ambigus entre les pays participants.
    "1202": "US",
    "1212": "US",
    "1213": "US",
    "1312": "US",
    "1415": "US",
    "1617": "US",
    "1650": "US",
    "1718": "US",
    "1416": "CA",
    "1514": "CA",
    "1604": "CA",
    "1647": "CA",
    "1242": "BS",
    "1876": "JM",
}


def caller_origin(number: str, *, is_contact: bool = False) -> tuple[str, str | None]:
    """Provenance déduite de l'indicatif, jamais une position physique garantie.

    Un numéro masqué n'est pas un numéro E.164. Même avec +33, l'appelant peut
    voyager ou usurper l'identité : le pays désigne seulement le plan de numérotation.
    """
    if not number or not number.startswith("+") or not number[1:].isdigit():
        return "masqué", None
    digits = number[1:]
    country = next(
        (
            code
            for prefix, code in sorted(
                _COUNTRY_PREFIXES.items(), key=lambda item: len(item[0]), reverse=True
            )
            if digits.startswith(prefix)
        ),
        None,
    )
    if is_contact:
        return "contact", country
    return ("inconnu" if country == "FR" else "étranger"), country


@dataclass(slots=True)
class CallRecord:
    call_id: str
    started_at: str
    caller_number: str = ""
    caller_name: str = ""
    relation: str = ""
    origin: str = "inconnu"
    country: str | None = None
    line_type: str = "unknown"
    route: HistoryRoute = "analyzed"
    direction: CallDirection = "incoming"
    ended_at: str | None = None
    duration_s: float | None = None
    answered: bool = False
    max_score: int | None = None
    max_level: str | None = None
    watch_at_s: float | None = None
    alert_at_s: float | None = None
    intervene_at_s: float | None = None
    peak_llm: float | None = None
    peak_keywords: float | None = None
    peak_voice: float | None = None
    peak_reputation: float | None = None
    tactics: list[str] = field(default_factory=list)
    explanation: str = ""
    actions: list[str] = field(default_factory=list)
    verification: VerificationResult = "none"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class HistoryPage:
    items: list[CallRecord]
    total: int
    offset: int
    limit: int

    def to_dict(self) -> dict[str, object]:
        return {
            "items": [item.to_dict() for item in self.items],
            "total": self.total,
            "offset": self.offset,
            "limit": self.limit,
            "has_more": self.offset + len(self.items) < self.total,
        }
