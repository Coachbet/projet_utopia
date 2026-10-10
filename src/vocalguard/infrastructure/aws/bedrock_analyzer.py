"""Signal de contenu : Claude Sonnet 4.5 sur Bedrock.

Le signal le plus lourd du score (poids 45), donc aussi celui dont la panne
doit être la plus douce : toute erreur renvoie `None`, ce que le port définit
comme « garde le signal précédent ». Le score continue alors de vivre sur les
mots-clés, qui sont instantanés (cf. coupe-circuit `keywords_alone_watch`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Sequence
from typing import Any

from vocalguard.config import get_settings
from vocalguard.domain.models import ContentSignal, TranscriptSegment
from vocalguard.infrastructure.aws.prompts import SYSTEM_PROMPT, build_user_message

logger = logging.getLogger(__name__)

# On préremplit le tour de l'assistant avec « { » : Claude enchaîne directement
# sur le JSON au lieu de l'introduire par une phrase.
_ASSISTANT_PREFILL = "{"


class BedrockContentAnalyzer:
    """Implémente le port `ContentAnalyzer`."""

    def __init__(
        self,
        *,
        client: Any | None = None,
        model_id: str | None = None,
        region: str | None = None,
        max_tokens: int | None = None,
        min_interval_seconds: float | None = None,
        window_turns: int | None = None,
    ) -> None:
        settings = get_settings()
        self._client = client
        self._model_id = model_id or settings.bedrock_model_id
        self._region = region or settings.bedrock_region
        self._max_tokens = max_tokens or settings.bedrock_max_tokens
        self._min_interval = (
            settings.llm_min_interval_seconds
            if min_interval_seconds is None
            else min_interval_seconds
        )
        self._window_turns = window_turns or settings.llm_transcript_window_turns
        self._last_call_at: float | None = None

    # --- Client ------------------------------------------------------------

    def _get_client(self) -> Any:
        """Construit le client à la première utilisation seulement.

        Jamais à l'import : `make replay` et les tests tournent sans
        identifiants AWS, et boto3 échouerait au chargement du module.
        """
        if self._client is None:
            import boto3

            self._client = boto3.client("bedrock-runtime", region_name=self._region)
        return self._client

    # --- Port --------------------------------------------------------------

    async def analyze(self, window: Sequence[TranscriptSegment]) -> ContentSignal | None:
        recent = self._window(window)
        if not recent:
            return None
        if not self._may_call():
            return None

        # Le compteur est armé avant l'appel : un Bedrock en panne ou throttlé
        # ne doit pas être re-sollicité à chaque segment.
        self._last_call_at = time.monotonic()

        body = self._request_body(recent)
        try:
            client = self._get_client()
            response = await asyncio.to_thread(
                client.invoke_model, modelId=self._model_id, body=body
            )
            text = _response_text(response)
        # Volontairement large : toute panne dégrade le signal, aucune ne casse l'appel.
        except Exception as exc:
            logger.warning("Bedrock indisponible, signal de contenu ignoré : %s", exc)
            return None

        signal = parse_content_signal(text)
        if signal is None:
            logger.warning("Réponse Claude non exploitable : %r", text[:200])
        return signal

    # --- Interne -----------------------------------------------------------

    def _window(self, turns: Sequence[TranscriptSegment]) -> list[TranscriptSegment]:
        """Derniers `window_turns` segments stabilisés.

        Les résultats partiels de Transcribe sont écartés : ils changent à
        chaque mot et feraient osciller le risque.
        """
        finals = [t for t in turns if t.is_final and t.text.strip()]
        return finals[-self._window_turns :]

    def _may_call(self) -> bool:
        if self._last_call_at is None:
            return True
        return (time.monotonic() - self._last_call_at) >= self._min_interval

    def _request_body(self, window: Sequence[TranscriptSegment]) -> str:
        return json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": self._max_tokens,
                "temperature": 0,
                "system": SYSTEM_PROMPT,
                "messages": [
                    {"role": "user", "content": build_user_message(window)},
                    {"role": "assistant", "content": _ASSISTANT_PREFILL},
                ],
            }
        )


# --- Lecture de la réponse -------------------------------------------------


def _response_text(response: Any) -> str:
    """Extrait le texte du corps Bedrock, que `body` soit un flux ou des octets."""
    body = response["body"]
    raw = body.read() if hasattr(body, "read") else body
    if isinstance(raw, bytes | bytearray):
        raw = raw.decode("utf-8")
    payload = json.loads(raw)
    return "".join(
        block.get("text", "") for block in payload.get("content", []) if isinstance(block, dict)
    )


def _first_json_object(text: str) -> dict[str, Any] | None:
    """Premier objet JSON équilibré du texte.

    Claude encadre parfois le JSON de prose ou d'un bloc ```json. On balaie
    donc en comptant les accolades, en ignorant celles qui sont dans une
    chaîne, plutôt que de faire confiance au format.
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        char = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def parse_content_signal(text: str) -> ContentSignal | None:
    """Texte brut de Claude -> `ContentSignal`, ou `None` si inexploitable."""
    if not text:
        return None

    # Le préremplissage « { » n'est pas renvoyé par le modèle : on tente le
    # texte tel quel, puis le texte recollé à son accolade ouvrante.
    payload = _first_json_object(text) or _first_json_object(_ASSISTANT_PREFILL + text)
    if payload is None:
        return None

    risk = _clamped_risk(payload.get("risk"))
    if risk is None:
        return None

    return ContentSignal(
        risk=risk,
        tactics=_string_tuple(payload.get("tactics")),
        claimed_identity=_optional_string(payload.get("claimed_identity")),
        explanation=_optional_string(payload.get("explanation")) or "",
    )


def _clamped_risk(value: Any) -> float | None:
    """`risk` est la seule donnée obligatoire : sans elle, pas de signal."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return max(0.0, min(1.0, float(value)))


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    # Claude écrit parfois la chaîne "null" au lieu du littéral JSON.
    return None if not stripped or stripped.casefold() == "null" else stripped
