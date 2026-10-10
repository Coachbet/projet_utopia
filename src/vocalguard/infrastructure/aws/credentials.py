"""Une résolution AWS commune, sans exporter les secrets du fichier `.env`."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from boto3.session import Session

    from vocalguard.config import Settings


class AWSCredentialsError(ValueError):
    """Erreur de configuration lisible et garantie sans valeur de secret."""


def aws_session(
    settings: Settings, *, region_name: str | None = None, allow_metadata: bool = True
) -> Session:
    """Clés de Settings si présentes, sinon chaîne boto3 (profil inclus).

    Les outils locaux peuvent désactiver les replis EC2/ECS pour échouer vite.
    Aucun champ Settings ni variable du processus n'est modifié.
    """
    import boto3

    access = settings.aws_access_key_id.get_secret_value().strip()
    secret = settings.aws_secret_access_key.get_secret_value().strip()
    token = settings.aws_session_token.get_secret_value().strip()
    if bool(access) != bool(secret) or (token and not access):
        raise AWSCredentialsError(
            "Identifiants AWS incomplets : AWS_ACCESS_KEY_ID et AWS_SECRET_ACCESS_KEY "
            "sont requis ensemble (.env ou variables exportées)."
        )
    if access.startswith("ASIA") and not token:
        raise AWSCredentialsError(
            "Identifiants AWS temporaires : AWS_SESSION_TOKEN est requis "
            "dans .env ou les variables exportées."
        )
    kwargs = {"region_name": region_name or settings.aws_region}
    if access:
        kwargs.update(
            aws_access_key_id=access,
            aws_secret_access_key=secret,
            aws_session_token=token or None,
        )
    session = boto3.Session(**kwargs)
    if not allow_metadata and not access:
        resolver = session._session.get_component("credential_provider")
        resolver.remove("iam-role")
        resolver.remove("container-role")
    return session
