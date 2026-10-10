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

from vocalguard.domain.alerts import DEFAULT_THRESHOLDS, Thresholds
from vocalguard.domain.scoring.weights import DEFAULT_WEIGHTS, Weights

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

    protected_phone_number: str = ""
    """Le VRAI numéro de la personne protégée, celui qu'elle donne à tout le monde.

    Elle garde ce numéro et active une fois un renvoi d'appel vers
    `TWILIO_PHONE_NUMBER` (`**21*<numéro Twilio>#`). On ne l'appelle JAMAIS :
    l'opérateur renverrait l'appel vers Twilio, qui rappellerait ce numéro, qui
    serait renvoyé… C'est la boucle que `loop_guard_numbers` existe pour
    empêcher. Il sert à deux choses seulement : reconnaître cette boucle, et
    l'afficher dans le README / le dashboard."""

    # --- L'app VocalGuard sur le téléphone (Lot 1 bis) ---------------------
    # La jambe « victime » n'est pas un numéro mais une identité client du SDK
    # Voice JavaScript : `calls.create(to="client:mamie")`. C'est ce qui évite la
    # boucle de renvoi décrite ci-dessus, affiche le vrai numéro de l'appelant à
    # l'écran, et coûte bien moins qu'une minute vers un mobile français.

    voice_client_identity: str = ""
    """Identité du client Voice JS (ex. `mamie`). Vide = repli téléphonique sur
    `VICTIM_PHONE_NUMBER`, soit le comportement d'avant le Lot 1 bis."""

    twilio_api_key_sid: str = ""
    twilio_api_key_secret: str = ""
    """API Key Twilio (SID + secret), nécessaire pour SIGNER un Access Token
    Voice. Le compte-token (`TWILIO_AUTH_TOKEN`) ne sait pas le faire : les JWT
    du SDK Voice se signent avec une API Key, pas avec le token de compte."""

    twilio_twiml_app_sid: str = ""
    """TwiML App (`AP…`) portée par le `VoiceGrant`. Nous ne l'utilisons que pour
    les appels ENTRANTS vers l'app, donc son Voice URL n'a pas besoin de servir
    quoi que ce soit — mais le grant la réclame."""

    app_pin: str = ""
    """Code d'accès à `POST /app/token`. L'URL ngrok est publique : sans lui,
    n'importe qui obtient un jeton capable de décrocher les appels de la personne
    protégée. Vide = la route refuse tout, plutôt que de distribuer des jetons."""

    app_token_ttl_seconds: int = 3600
    """Durée de vie du jeton Voice. Court par principe, mais pas trop : il est
    demandé à l'ouverture de la page, et l'app doit encore pouvoir sonner une
    heure plus tard sans que la personne protégée resaisisse son PIN."""

    # --- Vérification d'identité par code (Lot 2) --------------------------
    verify_code_ttl_seconds: int = 120
    """Fenêtre pendant laquelle le code à 4 chiffres est valable. Deux minutes :
    assez pour que le proche lise son SMS et le dicte, trop court pour qu'un
    arnaqueur tente les 10 000 combinaisons à voix haute."""

    # --- Analyse de la voix de la personne protégée seule (Lot 3) ----------
    trusted_victim_only_analysis: bool = False
    """Sur un appel de CONFIANCE, n'écouter que la piste de la personne protégée
    (`track="outbound_track"`), pour l'avertir si c'est ELLE qui s'apprête à
    donner un code de carte ou à faire un virement.

    Faux par défaut, et ce n'est pas de la frilosité : la promesse du produit est
    que la famille n'est jamais écoutée. L'activer ne traite que la voix de
    l'abonnée qui a consenti, jamais celle de son interlocuteur — mais ça reste
    **à valider juridiquement** avant toute mise en service réelle."""

    # --- Démo (Lot 4) ------------------------------------------------------
    scammer_phone_number: str = ""
    """Téléphone qui joue l'arnaqueur pendant la démo. Hors service nominal :
    il ne sert qu'à armer `DEMO_SPOOF_AS` et `scripts/lancer_appel_demo.py`."""

    demo_spoof_as: str = ""
    """Numéro du carnet que l'appel de démo doit FAIRE SEMBLANT d'afficher.

    Twilio interdit d'usurper un numéro qu'on ne possède pas, et c'est une bonne
    chose : le scénario C de la démo (« Lucas » appelle depuis un numéro usurpé)
    serait donc indémontrable. On simule donc la seule chose qui compte pour la
    démonstration — ce que notre serveur CROIT du `From` — et uniquement quand
    l'appel vient de `SCAMMER_PHONE_NUMBER`.

    Vide par défaut, inerte sans `SCAMMER_PHONE_NUMBER` (cf.
    `demo_spoof_active`), et chaque usage est logué en MAJUSCULES : un réglage
    qui ment sur l'identité de l'appelant ne doit jamais pouvoir agir en
    silence."""

    caller_notice_text: str = (
        "Cet appel est analysé par un service de protection contre la fraude."
    )
    """Phrase dite à l'APPELANT inconnu, à lui seul, avant tout flux audio.

    C'est la pièce centrale du montage juridique : on n'analyse que ce qui suit
    l'information de l'appelant (ePrivacy art. 5, secret des correspondances).
    D'où l'ordre imposé dans `inbound_call_twiml` : `<Say>` puis `<Start><Stream>`
    puis la conférence, jamais l'inverse."""

    twilio_validate_signature: bool = False
    """Vérifie `X-Twilio-Signature` sur les routes `/twilio/**`. FAUX par défaut,
    et c'est une décision, pas un oubli.

    Allumée, elle fait répondre 403 à TOUS les webhooks dès que l'URL signée par
    Twilio ne correspond pas exactement à celle qu'on reconstruit. C'est le cas
    normal derrière un tunnel ngrok : l'hôte est réécrit, le schéma n'arrive que
    dans `X-Forwarded-Proto`, et le serveur ne voit plus qu'un
    `http://localhost:8000/...`. À une heure du jury, le remède serait plus
    dangereux que le mal — on livre le code prêt, éteint.

    À allumer quand `PUBLIC_HOSTNAME` et l'URL collée dans la console Twilio sont
    identiques au caractère près, et que `TWILIO_AUTH_TOKEN` est renseigné (sans
    lui, la garde refuse tout plutôt que de laisser passer en silence)."""

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
    llm_min_interval_seconds: float = 2.5
    """Mesuré, pas supposé : Bedrock répond en 2,85 s (p50) et 3,59 s (p95) sur
    le vrai prompt. Un seul appel est en vol à la fois, donc la cadence réelle
    vaut `max(intervalle, latence)` : en dessous de ~2,8 s c'est la latence qui
    commande, et toute valeur plus basse ne change rien d'autre que le coût.
    L'ancien 4.0 ajoutait donc 1,2 s de retard pur sur le score."""
    llm_transcript_window_turns: int = 12

    # --- Voix synthétique (Dev 2) ------------------------------------------
    voice_detector_enabled: bool = False
    """Faux par défaut : torch n'est installé que par `make install-voice`, et le
    signal est de toute façon plafonné (il n'intervient jamais seul)."""
    voice_detector_model_id: str = "MelodyMachine/Deepfake-audio-detection-V2"
    voice_detector_window_seconds: float = 4.0
    voice_detector_target_sample_rate: int = 16000

    # --- Scoring (Dev 2) ---------------------------------------------------
    # Défauts dérivés du domaine, jamais recopiés. Le piège qu'on évite ici :
    # ces valeurs écrasent systématiquement celles de `weights.py` et
    # `alerts.py`, puisque `build_pipeline` passe `settings.weights()`. Deux
    # listes en parallèle, et modifier le fichier qui s'appelle « weights »
    # n'avait aucun effet en production — ce qui a déjà trompé une relecture.
    score_weight_llm: int = DEFAULT_WEIGHTS.llm
    score_weight_keywords: int = DEFAULT_WEIGHTS.keywords
    score_weight_voice: int = DEFAULT_WEIGHTS.voice
    score_weight_reputation: int = DEFAULT_WEIGHTS.reputation

    threshold_watch: int = DEFAULT_THRESHOLDS.watch
    threshold_alert: int = DEFAULT_THRESHOLDS.alert
    threshold_intervene: int = DEFAULT_THRESHOLDS.intervene

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

    @property
    def loop_guard_numbers(self) -> tuple[str, ...]:
        """Numéros depuis lesquels un appel entrant est forcément une boucle.

        Renvoyés **bruts** : la comparaison appartient à l'appelant, qui doit les
        passer par `normalize_number` comme il le fait du `From` de Twilio (le
        `.env` est saisi à la main, Twilio répond en E.164 strict).

        Le montage « un seul numéro » rend cette garde indispensable. La personne
        protégée renvoie sa ligne vers notre numéro Twilio : si nous rappelions
        son numéro public, l'opérateur nous le renverrait, et chaque tour coûte
        deux jambes facturées. Les trois cas à refuser :

          - `PROTECTED_PHONE_NUMBER` : la boucle du renvoi, le cas réel ;
          - `VICTIM_PHONE_NUMBER` : le repli téléphonique s'appelant lui-même ;
          - `TWILIO_PHONE_NUMBER` : notre propre numéro, donc nous.

        Les valeurs vides sont écartées ici : normalisés, deux numéros vides sont
        égaux, et un `From` absent (appel masqué) passerait pour une boucle —
        exactement l'appel qu'on veut analyser, et non refuser.
        """
        return tuple(
            number
            for number in (
                self.protected_phone_number,
                self.victim_phone_number,
                self.twilio_phone_number,
            )
            if number
        )

    @property
    def voice_client_target(self) -> str:
        """Destination `to=` de la jambe victime quand l'app porte l'appel.

        `client:<identité>` est la forme qu'attend `calls.create` pour sonner un
        client du SDK Voice JS plutôt qu'un numéro. Chaîne vide = pas d'app
        configurée, l'appelant retombe sur `VICTIM_PHONE_NUMBER`.
        """
        return f"client:{self.voice_client_identity}" if self.voice_client_identity else ""

    @property
    def app_token_configured(self) -> bool:
        """Tout ce qu'il faut pour signer un Access Token Voice est présent.

        Les quatre valeurs sont exigées ensemble : il manque toujours la dernière
        quand on croit avoir fini, et le symptôme d'une API Key absente est un
        `401` opaque côté navigateur, pas un message lisible.
        """
        return bool(
            self.twilio_account_sid
            and self.twilio_api_key_sid
            and self.twilio_api_key_secret
            and self.twilio_twiml_app_sid
            and self.voice_client_identity
        )

    @property
    def demo_spoof_active(self) -> bool:
        """L'usurpation simulée est-elle réellement armée ?

        Deux conditions, jamais une : le numéro à imiter ET le téléphone de
        l'arnaqueur. `DEMO_SPOOF_AS` seul ne doit rien pouvoir faire — sinon un
        `.env` oublié après la démo réécrirait le `From` d'appels réels, et le
        service croirait de confiance n'importe quel inconnu.
        """
        return bool(self.demo_spoof_as and self.scammer_phone_number)

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
