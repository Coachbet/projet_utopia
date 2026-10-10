"""Configuration unique du service, lue depuis l'environnement (`.env`).

Un seul endroit pour tous les réglages : sans ça, chaque brique relit `os.environ`
à sa façon et on passe la démo à chercher pourquoi deux modules ne voient pas la
même région AWS. Les champs sont groupés par rôle, voir `.env.example`.

Tout a un défaut : les tests et `make replay` tournent sans `.env`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from vocalguard.domain.alerts import Thresholds
from vocalguard.domain.scoring.weights import Weights

TranscribeBackend = Literal["aws", "whisper_local", "fake"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Serveur -----------------------------------------------------------
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"
    public_hostname: str = ""
    """Hôte ngrok sans schéma. Twilio doit joindre ce serveur depuis Internet :
    tant que c'est vide, aucun `<Stream>` ni callback ne peut fonctionner."""

    # --- Twilio ------------------------------------------------------------
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""
    victim_phone_number: str = ""
    trusted_contact_phone_number: str = ""
    twilio_conference_name: str = "utopia-guard"
    twilio_agent_conference_enabled: bool = False

    twilio_lookup_enabled: bool = False
    """Faux par défaut : Lookup v2 est facturé à la requête. C'est `.env` qui
    l'active pour la démo, pour qu'un test ou un rejeu ne puisse pas facturer."""

    # --- AWS ---------------------------------------------------------------
    aws_region: str = "us-west-2"

    # --- Transcribe (Dev 1) ------------------------------------------------
    transcribe_region: str = "us-west-2"
    transcribe_language_code: str = "fr-FR"
    transcribe_sample_rate: int = 8000
    transcribe_backend: TranscribeBackend = "aws"

    # --- Bedrock / Claude (Dev 2) ------------------------------------------
    bedrock_region: str = "us-west-2"
    bedrock_model_id: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    bedrock_max_tokens: int = 512
    llm_min_interval_seconds: float = 4.0
    llm_transcript_window_turns: int = 12

    # --- Voix synthétique (Dev 2) ------------------------------------------
    voice_detector_enabled: bool = False
    """Faux par défaut : torch n'est installé que par `make install-voice`, et le
    signal est de toute façon plafonné (il n'intervient jamais seul)."""
    voice_detector_model_id: str = "MelodyMachine/Deepfake-audio-detection-V2"
    voice_detector_window_seconds: float = 4.0
    voice_detector_target_sample_rate: int = 16000

    # --- Scoring (Dev 2) ---------------------------------------------------
    score_weight_llm: int = 45
    score_weight_keywords: int = 20
    score_weight_voice: int = 20
    score_weight_reputation: int = 15

    threshold_watch: int = 40
    threshold_alert: int = 65
    threshold_intervene: int = 85

    # --- Données -----------------------------------------------------------
    contacts_path: Path = Path("data/contacts.json")
    persist_transcripts: bool = False
    persist_audio: bool = False

    # --- Dérivés -----------------------------------------------------------

    @property
    def public_base_url(self) -> str:
        """URL https publique, pour les webhooks et callbacks Twilio."""
        return f"https://{self.public_hostname}"

    @property
    def media_stream_url(self) -> str:
        """URL wss du `<Stream>` Twilio. En clair ça ne marche pas : Twilio exige wss."""
        return f"wss://{self.public_hostname}/ws/media"

    @property
    def twilio_configured(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token)

    def conference_name_for(self, call_sid: str) -> str:
        """Une conférence par appel : deux démos en parallèle ne doivent pas
        se retrouver dans la même pièce, et le lookup par nom resterait ambigu."""
        return f"{self.twilio_conference_name}-{call_sid}"

    def weights(self) -> Weights:
        return Weights(
            llm=self.score_weight_llm,
            keywords=self.score_weight_keywords,
            voice=self.score_weight_voice,
            reputation=self.score_weight_reputation,
        )

    def thresholds(self) -> Thresholds:
        return Thresholds(
            watch=self.threshold_watch,
            alert=self.threshold_alert,
            intervene=self.threshold_intervene,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Mise en cache : relire `.env` à chaque requête webhook serait gâché."""
    return Settings()
