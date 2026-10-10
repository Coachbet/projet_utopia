"""Configuration lue depuis `.env` — une seule source de vérité.

Couvre toutes les clés de `.env.example`, y compris celles de la téléphonie, pour
qu'il n'y ait jamais deux lecteurs de config dans le projet. Tout a un défaut :
les tests et `make replay` tournent sans `.env`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from vocalguard.domain.alerts import Thresholds
from vocalguard.domain.scoring.weights import Weights

TranscribeBackend = Literal["aws", "whisper_local", "fake"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- Serveur -----------------------------------------------------------
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"
    public_hostname: str = ""

    # --- Twilio ------------------------------------------------------------
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""
    victim_phone_number: str = ""
    trusted_contact_phone_number: str = ""
    twilio_conference_name: str = "utopia-guard"
    twilio_agent_conference_enabled: bool = False
    twilio_lookup_enabled: bool = False

    # --- AWS ---------------------------------------------------------------
    aws_region: str = "us-west-2"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_session_token: str = ""

    # --- Transcribe --------------------------------------------------------
    transcribe_region: str = "us-west-2"
    transcribe_language_code: str = "fr-FR"
    transcribe_sample_rate: int = 8000
    transcribe_backend: TranscribeBackend = "aws"

    # --- Bedrock / Claude --------------------------------------------------
    bedrock_region: str = "us-west-2"
    bedrock_model_id: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    bedrock_max_tokens: int = 512
    llm_min_interval_seconds: float = 4.0
    llm_transcript_window_turns: int = 12

    # --- Voix synthétique --------------------------------------------------
    voice_detector_enabled: bool = False
    voice_detector_model_id: str = "MelodyMachine/Deepfake-audio-detection-V2"
    voice_detector_window_seconds: float = 4.0
    voice_detector_target_sample_rate: int = 16000

    # --- Scoring -----------------------------------------------------------
    score_weight_llm: int = 45
    score_weight_keywords: int = 20
    score_weight_voice: int = 20
    score_weight_reputation: int = 15

    threshold_watch: int = 40
    threshold_alert: int = 65
    threshold_intervene: int = 85

    # --- Données -----------------------------------------------------------
    contacts_path: str = "data/contacts.json"
    persist_transcripts: bool = False
    persist_audio: bool = False

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
