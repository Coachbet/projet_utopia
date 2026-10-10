#!/usr/bin/env python3
"""H0 — le rôle du workshop autorise-t-il Bedrock et Transcribe ?

C'est la première vérification du hackathon : si Transcribe streaming est
refusé, le plan B (Whisper local) doit être lancé tout de suite, pas découvert
à deux heures du rendu. Les identifiants du workshop expirent — relancer ce
script juste avant de passer devant le jury.

    make check-aws

Sortie non nulle si un service nécessaire est indisponible.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vocalguard.config import get_settings

OK = "\033[32m✓\033[0m"
BAD = "\033[31m✗\033[0m"
WARN = "\033[33m!\033[0m"
DIM = "\033[2m"
RESET = "\033[0m"


def _err(exc: Exception) -> str:
    """Message court : le code d'erreur AWS est ce qui nous intéresse."""
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    message = getattr(exc, "response", {}).get("Error", {}).get("Message", "")
    return f"{code}: {message}" if code else f"{type(exc).__name__}: {exc}"


def check_identity() -> bool:
    try:
        import boto3

        identity = boto3.client("sts").get_caller_identity()
    except Exception as exc:
        print(f"  {BAD} identité AWS introuvable — {_err(exc)}")
        print(f"      {DIM}identifiants absents ou expirés : rafraîchir ~/.aws/credentials{RESET}")
        return False
    arn = identity.get("Arn", "?")
    print(f"  {OK} identité  {DIM}{arn}{RESET}")
    return True


def check_bedrock() -> bool:
    settings = get_settings()
    try:
        import boto3

        client = boto3.client("bedrock-runtime", region_name=settings.bedrock_region)
        response = client.invoke_model(
            modelId=settings.bedrock_model_id,
            body=json.dumps(
                {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ping"}],
                }
            ),
        )
        json.loads(response["body"].read())
    except Exception as exc:
        print(f"  {BAD} bedrock   {settings.bedrock_model_id}")
        print(f"      {DIM}{_err(exc)}{RESET}")
        print(f"      {DIM}sans Bedrock : le score retombe sur les mots-clés seuls{RESET}")
        return False
    print(f"  {OK} bedrock   {DIM}{settings.bedrock_model_id} @ {settings.bedrock_region}{RESET}")
    return True


async def _probe_transcribe() -> None:
    """Ouvre un flux et le referme aussitôt : seule la permission nous intéresse."""
    from amazon_transcribe.client import TranscribeStreamingClient

    settings = get_settings()
    client = TranscribeStreamingClient(region=settings.transcribe_region)
    stream = await client.start_stream_transcription(
        language_code=settings.transcribe_language_code,
        media_sample_rate_hz=settings.transcribe_sample_rate,
        media_encoding="pcm",
    )
    # Un peu de silence suffit à prouver que le flux est accepté.
    await stream.input_stream.send_audio_event(audio_chunk=b"\x00" * 640)
    await stream.input_stream.end_stream()


def check_transcribe() -> bool:
    settings = get_settings()
    try:
        asyncio.run(asyncio.wait_for(_probe_transcribe(), timeout=20))
    except Exception as exc:
        print(
            f"  {BAD} transcribe {settings.transcribe_language_code} @ "
            f"{settings.transcribe_sample_rate} Hz"
        )
        print(f"      {DIM}{_err(exc)}{RESET}")
        print(f"      {DIM}plan B : TRANSCRIBE_BACKEND=whisper_local{RESET}")
        return False
    print(
        f"  {OK} transcribe {DIM}{settings.transcribe_language_code} @ "
        f"{settings.transcribe_sample_rate} Hz, région {settings.transcribe_region}{RESET}"
    )
    return True


def check_twilio() -> bool:
    """Pas d'appel réseau : on vérifie seulement que la config est remplie."""
    settings = get_settings()
    missing = [
        name
        for name, value in (
            ("TWILIO_ACCOUNT_SID", settings.twilio_account_sid),
            ("TWILIO_AUTH_TOKEN", settings.twilio_auth_token),
            ("TWILIO_PHONE_NUMBER", settings.twilio_phone_number.strip("+")),
            ("VICTIM_PHONE_NUMBER", settings.victim_phone_number.strip("+")),
        )
        if not value
    ]
    if missing:
        print(f"  {WARN} twilio    {DIM}non configuré : {', '.join(missing)}{RESET}")
        return True  # Dev 1 s'en occupe, ce n'est pas bloquant pour Dev 2.
    print(f"  {OK} twilio    {DIM}{settings.twilio_phone_number}{RESET}")
    return True


def main() -> int:
    print("\n\033[1mVérification H0 — accès aux services\033[0m\n")
    identity_ok = check_identity()

    if not identity_ok:
        print("\n  Rien d'autre à tester sans identité valide.\n")
        return 1

    results = {
        "bedrock": check_bedrock(),
        "transcribe": check_transcribe(),
        "twilio": check_twilio(),
    }
    print()

    blocking = [name for name, ok in results.items() if not ok]
    if blocking:
        print(f"  {BAD} à traiter maintenant : {', '.join(blocking)}\n")
        return 1
    print(f"  {OK} tout est accessible\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
