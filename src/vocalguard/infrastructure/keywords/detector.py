"""Moteur de mots-clés : le filet anti-latence.

Réagit instantanément, là où Claude demande quelques secondes. Synchrone et
sans dépendance réseau, donc toujours disponible même si Bedrock est bloqué.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from math import exp

from vocalguard.domain.events import Speaker
from vocalguard.domain.models import KeywordHit, KeywordSignal, TranscriptSegment
from vocalguard.infrastructure.keywords.lexicon import LEXICON

# Le risque sature au lieu de croître indéfiniment, et c'est la *gravité* qui
# domine la *quantité* : on somme les poids au carré, pas les poids. Un seul
# terme de poids 5 (« carte cadeau ») vaut 25 et donne 0,63 — déjà alarmant ;
# dix termes de poids 1 ne valent que 10, soit 0,33 — du contexte, pas une
# preuve. L'échelle 25 est précisément calée pour que « un terme décisif » et
# « le seuil de vigilance » tombent au même endroit.
_ENERGY_SCALE = 25.0

_CALLER: Speaker = "caller"

# Les segments sont recollés avec un point : `[\s-]+` ne le traverse pas, donc
# une expression ne peut pas se former par accident à la jonction de deux
# segments (« ne dis » + « rien à »).
_SEGMENT_JOIN = " . "

# Les caractères « ambigus » sont précisément ce qu'on vient normaliser ici.
_APOSTROPHES = str.maketrans({"’": "'", "ʼ": "'", "‘": "'", "`": "'"})  # noqa: RUF001


def normalize(text: str) -> str:
    """Minuscules, sans accents, apostrophes unifiées.

    Transcribe n'est pas fiable sur les accents : « ca » et « ça », « probleme »
    et « problème » doivent matcher le même terme.
    """
    folded = unicodedata.normalize("NFD", text.translate(_APOSTROPHES).casefold())
    return "".join(c for c in folded if not unicodedata.combining(c))


def _term_pattern(term: str) -> str:
    """Un terme -> un fragment de regex tolérant au pluriel et aux traits d'union.

    `[sx]?` couvre les deux pluriels français (« cartes cadeaux ») et `[\\s-]+`
    rend le trait d'union optionnel, l'ASR ne le plaçant pas de façon fiable.
    """
    words = [w for w in re.split(r"[\s-]+", normalize(term)) if w]
    body = r"[\s-]+".join(re.escape(w) + "[sx]?" for w in words)
    return rf"\b{body}\b"


class KeywordDetector:
    """Conforme au protocole `KeywordEngine`."""

    def __init__(self, lexicon: Sequence[KeywordHit] = LEXICON) -> None:
        # Les termes longs passent d'abord dans l'alternance : « carte bancaire »
        # doit gagner contre « carte », et chaque portion de texte ne compte
        # alors qu'une seule fois.
        self._hits: tuple[KeywordHit, ...] = tuple(
            sorted(lexicon, key=lambda h: len(h.term), reverse=True)
        )
        if not self._hits:
            raise ValueError("lexique vide")
        self._regex = re.compile("|".join(f"({_term_pattern(h.term)})" for h in self._hits))

    # --- API publique ------------------------------------------------------

    def scan(self, text: str) -> KeywordSignal:
        return self._signal(self._matches(text))

    def scan_call(self, turns: Sequence[TranscriptSegment]) -> KeywordSignal:
        """Signal cumulé sur tout l'appel, pour l'appelant seulement.

        La victime qui répète « carte bancaire » après l'appelant n'est pas une
        preuve contre elle : ses tours pèsent zéro.
        """
        spoken = [
            seg.text
            for seg in turns
            if seg.is_final and seg.speaker == _CALLER and seg.text.strip()
        ]
        if not spoken:
            return KeywordSignal(risk=0.0)
        return self._signal(self._matches(_SEGMENT_JOIN.join(spoken)))

    # --- Interne -----------------------------------------------------------

    def _matches(self, text: str) -> list[KeywordHit]:
        """Termes distincts trouvés. Une répétition ne s'empile pas."""
        if not text.strip():
            return []
        found: dict[int, KeywordHit] = {}
        for m in self._regex.finditer(normalize(text)):
            idx = m.lastindex
            if idx is not None:
                found.setdefault(idx - 1, self._hits[idx - 1])
        return sorted(found.values(), key=lambda h: (-h.weight, h.term))

    @staticmethod
    def _signal(hits: list[KeywordHit]) -> KeywordSignal:
        energy = sum(h.weight**2 for h in hits)
        risk = 1.0 - exp(-energy / _ENERGY_SCALE)
        return KeywordSignal(risk=round(risk, 4), hits=tuple(hits))
