"""Lexique pondéré des termes à risque — français d'abord, anglais en secours.

Biz 1 est propriétaire de la liste finale ; `load_lexicon()` permet de la
remplacer par un fichier JSON sans toucher au code.

Poids de 1 à 5 :
  5 — quasi décisif, un seul suffit à rendre l'appel suspect (« carte cadeau »,
      « ne dis rien à », « code de la carte »)
  4 — fort, demande confirmation par un autre signal
  3 — caractéristique du scénario d'arnaque mais possible dans un vrai appel
  2 — faible
  1 — simple contexte, ne doit jamais peser seul

Les termes génériques d'un vrai appel de famille sont volontairement absents ou
à 1 : « ne quittez pas », « c'est moi », « argent » seul. Les deux appels
légitimes de Biz 1 doivent rester sous le seuil de vigilance.

Les revendications de détresse (accident, hôpital, garde à vue) sont classées
`urgency` : c'est le prétexte qui fabrique l'urgence, pas une catégorie à part.
"""

from __future__ import annotations

import json
from pathlib import Path

from vocalguard.domain.models import KeywordHit

CATEGORIES = (
    "urgency",
    "secrecy",
    "unusual_payment",
    "false_authority",
    "claimed_identity",
    "credentials",
    "isolation",
)

MIN_WEIGHT = 1
MAX_WEIGHT = 5


# --- Moyen de paiement inhabituel ------------------------------------------
# Le signal le plus discriminant : personne ne demande des cartes cadeaux à sa
# grand-mère. Les marques de recharge prépayée (PCS, Transcash, Neosurf) sont
# les véhicules historiques de l'arnaque au faux proche en France.
_UNUSUAL_PAYMENT: tuple[tuple[str, int], ...] = (
    ("carte cadeau", 5),
    ("carte PCS", 5),
    ("transcash", 5),
    ("neosurf", 5),
    ("paysafecard", 5),
    ("coupon de recharge", 5),
    ("bitcoin", 5),
    ("crypto", 5),
    ("cryptomonnaie", 5),
    ("western union", 5),
    ("mandat cash", 5),
    ("virement urgent", 5),
    ("virement immédiat", 5),
    ("virement instantané", 4),
    ("coursier", 4),
    ("un taxi va passer", 4),
    ("virement", 3),
    ("mandat", 3),
    ("retirer de l'argent", 3),
    ("gift card", 5),
    ("steam card", 5),
    ("apple gift", 5),
    ("google play card", 5),
    ("prepaid card", 4),
    ("wire transfer", 4),
)


# --- Données bancaires et codes --------------------------------------------
# Une banque ne demande jamais ça par téléphone : c'est l'argument que la
# victime entend dans l'annonce vocale.
_CREDENTIALS: tuple[tuple[str, int], ...] = (
    ("code de la carte", 5),
    ("code secret", 5),
    ("code pin", 5),
    ("code à trois chiffres", 5),
    ("cryptogramme", 5),
    ("numéro de carte", 5),
    ("code de confirmation", 5),
    ("code de sécurité", 5),
    ("code reçu par SMS", 5),
    ("mot de passe", 5),
    ("coordonnées bancaires", 5),
    ("carte bancaire", 4),
    ("carte bleue", 4),
    ("code d'accès", 4),
    ("numéro de compte", 4),
    ("iban", 4),
    ("rib", 4),
    ("password", 5),
    ("one time code", 5),
    ("verification code", 5),
    ("cvv", 5),
)


# --- Urgence et prétexte de détresse ---------------------------------------
_URGENCY: tuple[tuple[str, int], ...] = (
    ("tout de suite", 3),
    ("immédiatement", 3),
    ("dans l'heure", 3),
    ("avant ce soir", 3),
    ("le plus vite possible", 3),
    ("c'est urgent", 3),
    ("ça ne peut pas attendre", 3),
    ("maintenant ou jamais", 4),
    ("avant qu'il soit trop tard", 4),
    ("urgence", 2),
    ("urgent", 2),
    ("j'ai eu un accident", 3),
    ("j'ai un gros problème", 3),
    ("je suis à l'hôpital", 3),
    ("je suis au commissariat", 4),
    ("je suis en garde à vue", 4),
    ("j'ai besoin d'argent", 4),
    ("il me faut de l'argent", 4),
    ("avance-moi", 3),
    ("right now", 3),
    ("i need money", 4),
    ("bail money", 4),
)


# --- Secret ----------------------------------------------------------------
# Demander le silence est le marqueur le plus fiable : un vrai proche en
# difficulté n'interdit pas d'appeler la famille.
_SECRECY: tuple[tuple[str, int], ...] = (
    ("ne dis rien à", 5),
    ("ne dites rien à", 5),
    ("ne le dis à personne", 5),
    ("reste entre nous", 5),
    ("garde ça pour toi", 5),
    ("n'en parle pas", 5),
    ("n'en parlez pas", 5),
    ("c'est un secret", 4),
    ("discrètement", 3),
    ("don't tell anyone", 5),
    ("keep this between us", 5),
)


# --- Isolement de la victime ------------------------------------------------
_ISOLATION: tuple[tuple[str, int], ...] = (
    ("n'appelle pas la banque", 5),
    ("n'appelez pas la banque", 5),
    ("ne contactez personne", 5),
    ("ne raccroche pas", 4),
    ("ne raccrochez pas", 4),
    ("reste en ligne", 3),
    ("restez en ligne", 3),
    ("vous êtes seule", 3),
    ("don't hang up", 4),
)


# --- Fausse autorité -------------------------------------------------------
_FALSE_AUTHORITY: tuple[tuple[str, int], ...] = (
    ("service des fraudes", 4),
    ("service anti-fraude", 4),
    ("service sécurité de la banque", 4),
    ("votre conseiller bancaire", 3),
    ("mandat d'arrêt", 5),
    ("poursuites judiciaires", 4),
    ("le procureur", 4),
    ("la gendarmerie", 3),
    ("les impôts", 3),
    ("trésor public", 3),
    ("interpol", 4),
    ("support microsoft", 5),
    ("service technique microsoft", 5),
    ("fraud department", 4),
    ("arrest warrant", 5),
)


# --- Identité revendiquée --------------------------------------------------
# Poids faibles : « mamie » est le premier mot d'un vrai appel de petit-fils.
# Seuls les contournements d'identification (nouveau numéro, voix méconnue)
# montent, parce qu'ils servent à expliquer une voix clonée.
_CLAIMED_IDENTITY: tuple[tuple[str, int], ...] = (
    ("j'ai changé de numéro", 4),
    ("c'est mon nouveau numéro", 4),
    ("nouveau numéro", 3),
    ("tu me reconnais pas", 3),
    ("tu ne me reconnais pas", 3),
    ("ma voix est bizarre", 3),
    ("j'ai perdu mon téléphone", 3),
    ("c'est ton petit-fils", 3),
    ("c'est ta petite-fille", 3),
    ("ton petit-fils", 2),
    ("ton neveu", 2),
    ("mamie", 1),
    ("papi", 1),
    ("grand-mère", 1),
    ("it's your grandson", 3),
    ("new number", 3),
)


def _build(
    *groups: tuple[str, tuple[tuple[str, int], ...]],
) -> tuple[KeywordHit, ...]:
    return tuple(
        KeywordHit(term=term, weight=weight, category=category)
        for category, entries in groups
        for term, weight in entries
    )


LEXICON: tuple[KeywordHit, ...] = _build(
    ("unusual_payment", _UNUSUAL_PAYMENT),
    ("credentials", _CREDENTIALS),
    ("urgency", _URGENCY),
    ("secrecy", _SECRECY),
    ("isolation", _ISOLATION),
    ("false_authority", _FALSE_AUTHORITY),
    ("claimed_identity", _CLAIMED_IDENTITY),
)


def load_lexicon(path: str | Path) -> tuple[KeywordHit, ...]:
    """Charge un lexique JSON pour remplacer `LEXICON`.

    Format attendu : `[{"term": "carte cadeau", "weight": 5,
    "category": "unusual_payment"}, ...]`. `category` est libre (le dashboard
    l'affiche tel quel) ; `weight` est validé pour éviter qu'une faute de
    frappe de Biz 1 déforme silencieusement le score.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("terms", [])
    if not isinstance(raw, list):
        raise ValueError(f"{path}: attendu une liste de termes, reçu {type(raw).__name__}")

    hits: list[KeywordHit] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        try:
            term = str(entry["term"]).strip()
            weight = int(entry["weight"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path}: entrée {i} invalide ({entry!r})") from exc
        if not term:
            raise ValueError(f"{path}: entrée {i} a un terme vide")
        if not MIN_WEIGHT <= weight <= MAX_WEIGHT:
            raise ValueError(
                f"{path}: '{term}' a un poids {weight}, attendu {MIN_WEIGHT}-{MAX_WEIGHT}"
            )
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        hits.append(KeywordHit(term=term, weight=weight, category=str(entry.get("category", ""))))
    if not hits:
        raise ValueError(f"{path}: lexique vide")
    return tuple(hits)
