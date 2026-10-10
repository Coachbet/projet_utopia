"""Tests du signal de contenu — aucun appel réseau.

Le client Bedrock est injecté par le constructeur : c'est la seule couture
nécessaire, `boto3` n'est jamais importé puisque `_get_client` ne construit
rien quand un client est fourni.
"""

from __future__ import annotations

import io
import json

import pytest

from vocalguard.domain.models import ContentSignal, TranscriptSegment
from vocalguard.infrastructure.aws.bedrock_analyzer import (
    BedrockContentAnalyzer,
    parse_content_signal,
)
from vocalguard.infrastructure.aws.prompts import build_user_message, format_transcript


class FakeBedrockClient:
    """Rejoue des réponses Bedrock et enregistre les requêtes reçues."""

    def __init__(self, *texts: str, raises: Exception | None = None) -> None:
        self._texts = list(texts)
        self._raises = raises
        self.calls: list[dict] = []

    def invoke_model(self, *, modelId: str, body: str) -> dict:
        # `modelId` en camelCase : c'est la signature boto3, pas un choix.
        self.calls.append({"modelId": modelId, "body": json.loads(body)})
        if self._raises is not None:
            raise self._raises
        text = self._texts.pop(0) if self._texts else ""
        payload = json.dumps({"content": [{"type": "text", "text": text}]})
        return {"body": io.BytesIO(payload.encode("utf-8"))}


def analyzer(*texts: str, raises: Exception | None = None, **kwargs) -> tuple:
    client = FakeBedrockClient(*texts, raises=raises)
    kwargs.setdefault("min_interval_seconds", 0.0)
    return BedrockContentAnalyzer(client=client, **kwargs), client


def turns(*texts: str, speaker: str = "caller", is_final: bool = True) -> list[TranscriptSegment]:
    return [
        TranscriptSegment(speaker=speaker, text=text, t=float(i), is_final=is_final)
        for i, text in enumerate(texts)
    ]


SCAM_JSON = json.dumps(
    {
        "risk": 0.93,
        "tactics": ["urgence", "paiement inhabituel", "identité revendiquée"],
        "claimed_identity": "Lucas, petit-fils",
        "explanation": "L'appelant réclame un virement immédiat.",
    },
    ensure_ascii=False,
)


# --- Chemin nominal --------------------------------------------------------


async def test_clean_json_is_parsed():
    subject, client = analyzer(SCAM_JSON)
    signal = await subject.analyze(turns("Mamie c'est Lucas, envoie 2000 euros"))

    assert isinstance(signal, ContentSignal)
    assert signal.risk == pytest.approx(0.93)
    assert "paiement inhabituel" in signal.tactics
    assert signal.claimed_identity == "Lucas, petit-fils"
    assert signal.explanation == "L'appelant réclame un virement immédiat."
    assert len(client.calls) == 1


async def test_prefilled_response_without_leading_brace():
    """Avec le préremplissage « { », le modèle ne renvoie pas l'accolade."""
    subject, _ = analyzer(SCAM_JSON[1:])
    signal = await subject.analyze(turns("envoie de l'argent"))

    assert signal is not None
    assert signal.risk == pytest.approx(0.93)


async def test_json_wrapped_in_prose():
    text = f"Voici mon analyse de l'appel.\n{SCAM_JSON}\nJ'espère que cela aide."
    subject, _ = analyzer(text)
    signal = await subject.analyze(turns("envoie de l'argent"))

    assert signal is not None
    assert signal.risk == pytest.approx(0.93)


async def test_json_in_fenced_block():
    subject, _ = analyzer(f"```json\n{SCAM_JSON}\n```")
    signal = await subject.analyze(turns("envoie de l'argent"))

    assert signal is not None
    assert signal.tactics == ("urgence", "paiement inhabituel", "identité revendiquée")


async def test_legitimate_call_keeps_low_risk():
    body = json.dumps(
        {"risk": 0.04, "tactics": [], "claimed_identity": None, "explanation": "Appel ordinaire."},
        ensure_ascii=False,
    )
    subject, _ = analyzer(body)
    signal = await subject.analyze(turns("je passe dimanche"))

    assert signal is not None
    assert signal.risk == pytest.approx(0.04)
    assert signal.tactics == ()
    assert signal.claimed_identity is None


# --- Robustesse ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(1.7, 1.0), (-0.5, 0.0), (42, 1.0), (0.5, 0.5)],
)
async def test_risk_is_clamped(raw, expected):
    subject, _ = analyzer(json.dumps({"risk": raw}))
    signal = await subject.analyze(turns("texte"))

    assert signal is not None
    assert signal.risk == pytest.approx(expected)


async def test_missing_fields_fall_back_to_defaults():
    subject, _ = analyzer(json.dumps({"risk": 0.3}))
    signal = await subject.analyze(turns("texte"))

    assert signal == ContentSignal(risk=0.3, tactics=(), claimed_identity=None, explanation="")


async def test_string_null_identity_is_none():
    subject, _ = analyzer(json.dumps({"risk": 0.3, "claimed_identity": "null"}))
    signal = await subject.analyze(turns("texte"))

    assert signal is not None
    assert signal.claimed_identity is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "pas de json ici",
        '{"risk": 0.9',  # tronqué
        '{"risk": "élevé"}',  # risk non numérique
        '{"risk": true}',  # un booléen n'est pas un risque
        "[1, 2, 3]",  # pas un objet
    ],
)
async def test_unusable_response_returns_none(text):
    subject, _ = analyzer(text)
    assert await subject.analyze(turns("texte")) is None


async def test_malformed_body_returns_none():
    class BrokenClient:
        def invoke_model(self, **_):
            return {"body": io.BytesIO(b"<html>502 Bad Gateway</html>")}

    subject = BedrockContentAnalyzer(client=BrokenClient(), min_interval_seconds=0.0)
    assert await subject.analyze(turns("texte")) is None


async def test_client_exception_returns_none():
    subject, _ = analyzer(raises=RuntimeError("ThrottlingException"))
    assert await subject.analyze(turns("texte")) is None


async def test_failure_still_consumes_the_interval():
    """Un Bedrock throttlé ne doit pas être re-sollicité à chaque segment."""
    subject, client = analyzer(raises=RuntimeError("ThrottlingException"), min_interval_seconds=60)

    assert await subject.analyze(turns("texte")) is None
    assert await subject.analyze(turns("texte")) is None
    assert len(client.calls) == 1


# --- Cadence et fenêtre ----------------------------------------------------


async def test_min_interval_gate_blocks_second_call():
    subject, client = analyzer(SCAM_JSON, SCAM_JSON, min_interval_seconds=60)

    assert await subject.analyze(turns("premier")) is not None
    assert await subject.analyze(turns("deuxième")) is None
    assert len(client.calls) == 1


async def test_window_truncates_to_configured_turns():
    subject, client = analyzer(SCAM_JSON, window_turns=3)
    await subject.analyze(turns("a", "b", "c", "d", "e"))

    user_message = client.calls[0]["body"]["messages"][0]["content"]
    assert "APPELANT: c" in user_message
    assert "APPELANT: d" in user_message
    assert "APPELANT: e" in user_message
    assert "APPELANT: a" not in user_message
    assert "APPELANT: b" not in user_message


async def test_partial_segments_are_ignored():
    subject, client = analyzer(SCAM_JSON)
    await subject.analyze(
        [
            *turns("segment stable"),
            *turns("segment en cou", is_final=False),
        ]
    )

    user_message = client.calls[0]["body"]["messages"][0]["content"]
    assert "segment stable" in user_message
    assert "segment en cou" not in user_message


async def test_no_final_segment_skips_the_call():
    subject, client = analyzer(SCAM_JSON)

    assert await subject.analyze(turns("partiel", is_final=False)) is None
    assert await subject.analyze([]) is None
    assert client.calls == []


# --- Requête ---------------------------------------------------------------


async def test_request_shape():
    subject, client = analyzer(SCAM_JSON, model_id="modele-test", max_tokens=321)
    await subject.analyze(turns("texte"))

    call = client.calls[0]
    assert call["modelId"] == "modele-test"
    body = call["body"]
    assert body["max_tokens"] == 321
    assert body["anthropic_version"] == "bedrock-2023-05-31"
    assert body["temperature"] == 0
    assert "JSON" in body["system"]
    # Le dernier tour préremplit la réponse pour forcer le JSON.
    assert body["messages"][-1] == {"role": "assistant", "content": "{"}


def test_transcript_labels_each_speaker():
    formatted = format_transcript(
        [
            TranscriptSegment(speaker="caller", text="envoie l'argent", t=0.0, is_final=True),
            TranscriptSegment(speaker="victim", text="qui est-ce ?", t=1.0, is_final=True),
        ]
    )

    assert formatted == "APPELANT: envoie l'argent\nPERSONNE APPELÉE: qui est-ce ?"


def test_user_message_contains_the_transcript():
    message = build_user_message(turns("bonjour mamie"))
    assert "APPELANT: bonjour mamie" in message


def test_parse_content_signal_is_usable_standalone():
    """Utilisé par le scénario rejoué et les fakes, sans passer par Bedrock."""
    assert parse_content_signal(SCAM_JSON) is not None
    assert parse_content_signal("rien") is None


def test_extra_nested_key_without_the_prefilled_brace():
    """Régression mesurée sur Bedrock : une réponse sur trois était perdue.

    Claude ajoute parfois une clé imbriquée de son cru. Comme le préremplissage
    « { » n'est pas renvoyé, le scanner d'objet s'arrêtait sur cette
    imbrication, renvoyait un dictionnaire valide mais sans `risk`, et le
    court-circuit du `or` empêchait le second essai : le signal du LLM — 45
    points, le plus lourd — disparaissait sans erreur visible.
    """
    observed = (
        '\n  "risk": 0.95,'
        '\n  "tactics": ["urgence", "paiement inhabituel"],'
        '\n  "claimed_identity": "Lucas, petit-fils",'
        '\n  "explanation": "Demande de virement immédiat.",'
        '\n  "budget": {'
        '\n    "tokens_used": 1247,'
        '\n    "tokens_remaining": 198753'
        "\n  }"
        "\n}"
    )
    signal = parse_content_signal(observed)
    assert signal is not None, "la clé imbriquée fait de nouveau perdre le signal"
    assert signal.risk == 0.95
    assert signal.claimed_identity == "Lucas, petit-fils"


def test_prefilled_brace_is_not_required_to_be_absent():
    """L'autre sens doit continuer de marcher : réponse complète, accolade incluse."""
    signal = parse_content_signal('{"risk": 0.4, "tactics": [], "explanation": "ok"}')
    assert signal is not None
    assert signal.risk == 0.4
