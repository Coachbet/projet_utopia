#!/usr/bin/env python3
"""H0 — les identifiants courants autorisent-ils vraiment Bedrock et Transcribe ?

Deux risques du plan se ressemblent dans les logs mais ont des plans B opposés :
le rôle du workshop qui n'autorise pas l'appel, et le réseau du lieu qui bloque
l'accès (déjà vu avec Bedrock). Ce script les sépare et dit quoi faire pour chacun.

Les deux vérifications sont de VRAIS appels : lister les modèles ou signer une
requête ne prouve rien, seule une invocation prouve l'autorisation.

La configuration Twilio est vérifiée aussi, mais sans appel réseau : c'est un
avertissement, pas un échec — elle n'empêche pas Dev 2 de travailler.

Lancer :
    make check-aws
    .venv/bin/python scripts/check_aws_permissions.py
    .venv/bin/python scripts/check_aws_permissions.py --skip-transcribe

Sortie 0 si tout passe, 1 dès qu'une vérification échoue.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Le paquet est installé par `uv sync`, mais le script doit aussi marcher lancé
# à la main depuis un clone frais, sans installation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vocalguard.config import Settings, get_settings

# Marqueurs cherchés dans le type et le message de l'exception. Le nom de classe
# seul ne suffit pas : boto3 et le SDK Transcribe n'emballent pas les mêmes erreurs
# de la même façon (Transcribe renvoie un UnknownServiceException 403 pour un refus).
AUTH_MARKERS = (
    "accessdenied",
    "unrecognizedclient",
    "invalidclienttokenid",
    "expiredtoken",
    "tokenrefreshrequired",
    "signaturedoesnotmatch",
    "nocredentials",
    "unable to locate credentials",
    "credentialsexception",
    "forbidden",
    "notauthorized",
    "unauthorized",
    "validationexception",  # modèle non activé pour le compte : c'est un refus, pas un bug
)
NETWORK_MARKERS = (
    "endpointconnectionerror",
    "connecttimeout",
    "readtimeout",
    "connectionclosed",
    "connectionerror",
    "timed out",
    "timeout",
    "name resolution",
    "dns",  # awscrt renvoie AWS_IO_DNS_INVALID_NAME, pas le libellé botocore
    "aws_io_",
    "nodename nor servname",
    "temporary failure",
    "unreachable",
    "ssl",
    "proxy",
    "certificate",
)

REFRESH_CREDS = (
    "Identifiants refusés ou expirés : ceux du workshop expirent vite.",
    "→ les rafraîchir dans ~/.aws/credentials (ou .env), puis relancer `make check-aws`.",
)
HOTSPOT = ("→ le réseau du lieu bloque peut-être AWS : basculer sur le hotspot téléphone dédié.",)
WHISPER_FALLBACK = (
    "→ repli : TRANSCRIBE_BACKEND=whisper_local dans .env (transcription locale par morceaux).",
)


@dataclass(frozen=True, slots=True)
class Result:
    name: str
    ok: bool
    detail: str
    plan_b: tuple[str, ...] = field(default=())
    warn: bool = False
    """Signalé sans faire échouer : ce qui manque n'empêche pas de travailler.
    Un badge vert sur « 4 variables manquantes » serait un mensonge, un échec
    rouge enverrait l'équipe chercher un problème AWS qui n'existe pas."""


def _classify(exc: BaseException) -> str:
    """ "auth" (autorisation refusée), "network" (on n'a pas joint AWS), ou "other"."""
    haystack = f"{type(exc).__name__} {exc}".casefold()

    # Un code HTTP est plus fiable que le texte quand il est disponible.
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return "auth"

    # L'ordre compte : un "ConnectTimeout" contient parfois le nom du service.
    if any(m in haystack for m in NETWORK_MARKERS):
        return "network"
    if any(m in haystack for m in AUTH_MARKERS):
        return "auth"
    return "other"


def _plan_b_for(kind: str, *, whisper: bool = False) -> tuple[str, ...]:
    if kind == "auth":
        return REFRESH_CREDS + (WHISPER_FALLBACK if whisper else ())
    if kind == "network":
        return HOTSPOT + (WHISPER_FALLBACK if whisper else ())
    return ("→ cause inattendue : lire le message ci-dessus avant de changer de plan.",)


def _mask(value: str, keep: int = 4) -> str:
    """Un identifiant de compte n'est pas un secret, mais on ne le met pas au tableau."""
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * (len(value) - keep) + value[-keep:]


def _short_arn(arn: str) -> str:
    """Garde la fin lisible de l'ARN (le rôle), masque le numéro de compte dedans."""
    parts = arn.split(":")
    if len(parts) >= 6 and parts[4]:
        parts[4] = _mask(parts[4])
    arn = ":".join(parts)
    return arn if len(arn) <= 72 else f"…{arn[-71:]}"


# --- Vérification 1 : qui suis-je ? ----------------------------------------


def check_identity(settings: Settings) -> tuple[Result, object | None]:
    """Résout les identifiants une seule fois, pour que les trois tests parlent des mêmes."""
    # Étape 1 : a-t-on seulement des identifiants ? Sans eux, rien n'est testable.
    try:
        import boto3
        from botocore.config import Config

        session = boto3.Session(region_name=settings.aws_region)
        credentials = session.get_credentials()
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"[:200]
        return Result("Identité AWS", False, detail, REFRESH_CREDS), None

    if credentials is None:
        return (
            Result(
                "Identité AWS",
                False,
                "aucun identifiant trouvé (env, ~/.aws/credentials, rôle)",
                REFRESH_CREDS,
            ),
            None,
        )

    # Étape 2 : l'appel STS. Son échec n'invalide pas les autres tests.
    try:
        sts = session.client(
            "sts",
            config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1}),
        )
        identity = sts.get_caller_identity()
    except Exception as exc:
        kind = _classify(exc)
        # On rend quand même la session : un rôle workshop verrouillé peut refuser
        # sts:GetCallerIdentity tout en autorisant Bedrock. Déclarer les deux autres
        # tests en échec ici serait un faux négatif qui enverrait l'équipe sur le
        # mauvais plan B. `session` ne vaut None que si on n'a aucun identifiant.
        return (
            Result("Identité AWS", False, f"{type(exc).__name__}: {exc}"[:200], _plan_b_for(kind)),
            session,
        )

    detail = f"compte {_mask(identity['Account'])} · {_short_arn(identity['Arn'])}"
    return Result("Identité AWS", True, detail), session


# --- Vérification 2 : Bedrock (part de Dev 2, mais testée à H0) -------------


def check_bedrock(settings: Settings, session: object) -> Result:
    name = f"Bedrock {settings.bedrock_region}"
    try:
        from botocore.config import Config

        client = session.client(  # type: ignore[attr-defined]
            "bedrock-runtime",
            region_name=settings.bedrock_region,
            # Timeouts courts : un réseau qui bloque doit échouer en secondes, pas
            # en minutes — on fait ce test pendant que le reste de l'équipe attend.
            config=Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 1}),
        )
        response = client.converse(
            modelId=settings.bedrock_model_id,
            messages=[{"role": "user", "content": [{"text": "ping"}]}],
            inferenceConfig={"maxTokens": 1, "temperature": 0.0},
        )
    except Exception as exc:
        kind = _classify(exc)
        return Result(name, False, f"{type(exc).__name__}: {exc}"[:240], _plan_b_for(kind))

    stop = response.get("stopReason", "?")
    model = settings.bedrock_model_id.split(".")[-1][:40]
    return Result(name, True, f"{model} a répondu (stopReason={stop})")


# --- Vérification 3 : Transcribe streaming (part de Dev 1) ------------------


async def _probe_transcribe(settings: Settings, credentials: object) -> None:
    from amazon_transcribe.auth import StaticCredentialResolver
    from amazon_transcribe.client import TranscribeStreamingClient

    frozen = credentials.get_frozen_credentials()  # type: ignore[attr-defined]
    client = TranscribeStreamingClient(
        region=settings.transcribe_region,
        # On impose les identifiants de boto3 : le SDK Transcribe a sa propre
        # chaîne de résolution (awscrt) qui peut trouver AUTRE CHOSE que boto3.
        # Deux chaînes = un « ça marche chez moi » garanti le jour de la démo.
        credential_resolver=StaticCredentialResolver(
            frozen.access_key,
            frozen.secret_key,
            frozen.token,
        ),
    )
    stream = await client.start_stream_transcription(
        language_code=settings.transcribe_language_code,
        media_sample_rate_hz=settings.transcribe_sample_rate,
        media_encoding="pcm",
    )
    # L'autorisation est tranchée à l'ouverture du stream ; on envoie quand même
    # 200 ms de silence et on referme proprement pour valider tout l'aller-retour.
    silence = b"\x00" * (settings.transcribe_sample_rate // 5 * 2)  # PCM 16 bits
    await stream.input_stream.send_audio_event(audio_chunk=silence)
    await stream.input_stream.end_stream()


def check_transcribe(settings: Settings, session: object) -> Result:
    name = f"Transcribe {settings.transcribe_region} ({settings.transcribe_language_code})"
    credentials = session.get_credentials()  # type: ignore[attr-defined]
    try:
        asyncio.run(asyncio.wait_for(_probe_transcribe(settings, credentials), timeout=25))
    except TimeoutError:
        return Result(name, False, "aucune réponse en 25 s", HOTSPOT + WHISPER_FALLBACK)
    except Exception as exc:
        kind = _classify(exc)
        code = getattr(exc, "error_code", "") or getattr(exc, "status_code", "")
        detail = f"{type(exc).__name__}{f' [{code}]' if code else ''}: {exc}"[:240]
        return Result(name, False, detail, _plan_b_for(kind, whisper=True))

    return Result(name, True, "StartStreamTranscription autorisé, stream ouvert et refermé")


# --- Vérification 4 : Twilio, sans appel réseau -----------------------------


def _is_set(value: str) -> bool:
    """Un numéro réduit à son indicatif (« +1 », « +33 ») est un gabarit, pas un numéro."""
    return bool(value.strip().lstrip("+").strip())


def check_twilio(settings: Settings) -> Result:
    """Aucun appel à Twilio : on regarde seulement si la config est remplie.

    Non bloquant — c'est la part de Dev 1, et Dev 2 travaille sans téléphone.
    """
    missing = [
        name
        for name, value in (
            ("TWILIO_ACCOUNT_SID", settings.twilio_account_sid),
            ("TWILIO_AUTH_TOKEN", settings.twilio_auth_token),
            ("TWILIO_PHONE_NUMBER", settings.twilio_phone_number),
            ("VICTIM_PHONE_NUMBER", settings.victim_phone_number),
            ("TRUSTED_CONTACT_PHONE_NUMBER", settings.trusted_contact_phone_number),
        )
        if not _is_set(value)
    ]
    if not missing:
        return Result("Twilio (config)", True, f"configuré · {settings.twilio_phone_number}")

    plan_b = [f"→ à remplir dans .env : {', '.join(missing)}"]
    if "TRUSTED_CONTACT_PHONE_NUMBER" in missing:
        # Le SMS « c'est toi qui appelles ? » est la moitié de la démo, et son
        # absence ne se voit qu'au moment où il devrait partir.
        plan_b.append(
            "→ sans TRUSTED_CONTACT_PHONE_NUMBER, le SMS de vérification "
            "ne part pas (action `sms_contact` ok=false)."
        )
    return Result(
        "Twilio (config)",
        True,
        f"{len(missing)} variable(s) manquante(s)",
        plan_b=tuple(plan_b),
        warn=True,
    )


# --- Affichage --------------------------------------------------------------


def _render(results: list[Result]) -> None:
    tty = sys.stdout.isatty()
    if tty:
        green, red, yellow, dim, reset = (
            "\033[32m",
            "\033[31m",
            "\033[33m",
            "\033[2m",
            "\033[0m",
        )
    else:
        green = red = yellow = dim = reset = ""

    width = max(len(r.name) for r in results)
    print()
    print("  VÉRIFICATIONS AWS — à passer avant tout le reste (H0)")
    print("  " + "─" * 68)
    for r in results:
        # Badges de largeur identique ("[ OK ] " fait 7 signes comme "[ÉCHEC]"),
        # sinon les colonnes se décalent dès qu'une ligne échoue.
        if r.warn:
            badge = f"{yellow}[ !  ] {reset}"
        elif r.ok:
            badge = f"{green}[ OK ] {reset}"
        else:
            badge = f"{red}[ÉCHEC]{reset}"
        print(f"  {badge}  {r.name.ljust(width)}  {r.detail}")
        for line in r.plan_b:
            print(f"          {dim}{line}{reset}")
    print("  " + "─" * 68)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Vérifie que les identifiants courants autorisent Bedrock et Transcribe. "
            "Fait de vrais appels (facturés au prix d'une broutille)."
        ),
    )
    parser.add_argument(
        "--skip-transcribe",
        action="store_true",
        help="ne teste pas Transcribe (le test ouvre un vrai flux de streaming)",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    results: list[Result] = []

    identity, session = check_identity(settings)
    results.append(identity)

    if session is None:
        # Sans identité, les deux autres tests échoueraient tous pour la même
        # raison : on ne les lance pas, ça brouillerait le diagnostic.
        results.append(Result("Bedrock", False, "non testé (pas d'identité AWS)"))
        results.append(Result("Transcribe", False, "non testé (pas d'identité AWS)"))
    else:
        results.append(check_bedrock(settings, session))
        if args.skip_transcribe:
            results.append(
                Result("Transcribe", True, "non testé (--skip-transcribe)", warn=True)
            )
        else:
            results.append(check_transcribe(settings, session))

    results.append(check_twilio(settings))

    _render(results)

    failed = [r for r in results if not r.ok]
    if failed:
        print(f"  {len(failed)} vérification(s) en échec — appliquer le plan B ci-dessus.\n")
        return 1
    warned = [r for r in results if r.warn]
    if warned:
        print(f"  Rien ne bloque, {len(warned)} point(s) à surveiller ci-dessus.\n")
        return 0
    print("  Tout est autorisé : Dev 1 peut brancher Transcribe, Dev 2 peut brancher Claude.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
