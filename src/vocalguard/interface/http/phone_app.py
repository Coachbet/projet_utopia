"""L'app VocalGuard : la page installée sur le téléphone de la personne protégée.

Pourquoi une page web et pas un rappel téléphonique. La personne protégée garde
son vrai numéro et le renvoie une fois pour toutes vers notre numéro Twilio. Si
nous rappelions ce numéro public pour la faire sonner, l'opérateur nous le
renverrait vers Twilio, qui le rappellerait… **boucle**, deux jambes facturées
par tour et aucun appel qui aboutit. On fait donc sonner une page installée sur
son téléphone via le SDK Voice JavaScript : la jambe « victime » devient
`client:<identité>` au lieu d'un numéro. Plus de boucle, plus besoin d'une
seconde SIM, la minute coûte bien moins qu'un appel vers un mobile français, et
l'écran peut afficher le VRAI numéro de l'appelant et porter les boutons.

**LIMITE À CONNAÎTRE AVANT DE COMPTER DESSUS** : une page web ne sonne **que si
elle est ouverte**. iOS suspend les pages passées en arrière-plan et il n'existe
aucun push VoIP sans application native : téléphone verrouillé, onglet fermé ou
changé, l'app ne sonnera pas. Le repli est `VICTIM_PHONE_NUMBER` quand il est
rempli (cf. `TwilioCallController.dial_victim`) ; sinon personne ne décroche.
L'app native — CallKit sur iOS, ConnectionService sur Android, donc un compte
Apple Developer payant — est la roadmap, pas ce lot. Ne pas promettre à
l'utilisateur que son téléphone sonnera tout seul.

Quatre routes, et chacune existe pour une raison précise :

  - `GET /app` sert la page, sur le modèle exact de `interface/ws/dashboard.py` ;
  - `GET /app/manifest.webmanifest` et `GET /app/icon.svg` servent « Ajouter à
    l'écran d'accueil », pour que la personne protégée lance l'app d'un geste
    plutôt qu'en retrouvant une URL ngrok dans son navigateur ;
  - `POST /app/token` signe l'Access Token Voice. **Protégée par `APP_PIN`** : un
    jeton Voice permet de DÉCROCHER les appels de la personne protégée, et l'URL
    ngrok est publique. Le distribuer sans code reviendrait à poser son téléphone
    sur Internet ;
  - `POST /app/incoming` donne à l'écran le `call_id`, le numéro et le nom de
    l'appelant.

Cette dernière mérite son explication, parce qu'elle a l'air d'un contournement
et n'en est pas un. La seule méthode **documentée** pour passer des paramètres
personnalisés à un client Voice JS entrant est la noun `<Parameter>` imbriquée
dans `<Dial><Client>` (doc TwiML Client : « It is possible to include additional
key value pairs that will be passed to the Voice SDK device instance (Web or
Mobile). You can do this by using the nested `<Parameter>` TwiML noun »), lue
côté navigateur dans `Call.customParameters`. Or notre jambe victime n'est pas
créée par un `<Dial><Client>` mais par `calls.create(to="client:…")`, et la doc
de la ressource Call ne documente AUCUN paramètre personnalisé pour cette forme.
Le serveur, lui, en sait de toute façon plus que Twilio : il a le carnet
d'adresses et le registre des appels. La page tente donc `customParameters`
d'abord — gratuit, et correct le jour où la jambe passera par `<Dial><Client>` —
et retombe sur cette route.

Ce module répond en JSON, jamais en TwiML : ce n'est pas un webhook Twilio mais
notre propre page qui l'interroge en `fetch()`. Même parti pris que son voisin
`control.py`.
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant

if TYPE_CHECKING:
    from vocalguard.config import Settings

log = logging.getLogger(__name__)

# `interface/http/phone_app.py` -> `interface/static/…`
_STATIC = Path(__file__).parent.parent / "static"
APP_HTML = _STATIC / "app.html"
APP_MANIFEST = _STATIC / "manifest.webmanifest"
APP_ICON = _STATIC / "icon.svg"

router = APIRouter(tags=["app"])


class DemandeApp(BaseModel):
    """Corps commun aux deux routes protégées : le code saisi sur le téléphone.

    Défaut vide et non obligatoire : un corps absent doit produire le même refus
    qu'un mauvais code, et pas un 422 de validation — la page ne doit avoir qu'un
    seul chemin d'erreur à afficher à une personne âgée (« code incorrect »).
    """

    pin: str = ""


@router.get("/app", include_in_schema=False)
async def page() -> FileResponse:
    """La page de l'app. Contrairement au dashboard, elle charge un CDN : le SDK
    Voice de Twilio n'existe qu'en JavaScript et nous ne le recopions pas dans le
    dépôt. Conséquence assumée et écrite dans la page : si le réseau du lieu
    bloque jsdelivr, l'app est inerte."""
    return FileResponse(APP_HTML, media_type="text/html")


@router.get("/app/manifest.webmanifest", include_in_schema=False)
async def manifest() -> FileResponse:
    """Manifeste de l'installation sur l'écran d'accueil.

    Le type MIME est explicite : servi en `text/plain`, Chrome ignore le
    manifeste en silence et le bouton « Installer » n'apparaît jamais — un
    symptôme sans message d'erreur, donc impossible à deviner sur scène.
    """
    return FileResponse(APP_MANIFEST, media_type="application/manifest+json")


@router.get("/app/icon.svg", include_in_schema=False)
async def icon() -> FileResponse:
    """Icône de l'app. En SVG : une seule taille pour tous les écrans.

    Limite connue : iOS n'accepte pas le SVG pour `apple-touch-icon`, donc sur
    iPhone l'icône de l'écran d'accueil sera une capture de la page. Corriger
    demanderait un PNG de 180 px binaire dans le dépôt ; ça ne change rien au
    fonctionnement de l'app.
    """
    return FileResponse(APP_ICON, media_type="image/svg+xml")


@router.post("/app/token")
async def jeton_voice(demande: DemandeApp, request: Request) -> dict[str, object]:
    """Access Token Voice pour `VOICE_CLIENT_IDENTITY`, contre le bon `APP_PIN`.

    La réponse porte le JWT, l'identité et la durée de vie — rien d'autre.
    Surtout pas `TWILIO_API_KEY_SECRET` ni `TWILIO_AUTH_TOKEN` : ce sont eux qui
    signent, les livrer au navigateur rendrait le PIN décoratif. Le JWT est par
    nature un porteur de droits, c'est normal et c'est tout l'objet de la route ;
    il n'est pour autant jamais journalisé, parce qu'un log de démo se partage et
    se copie-colle.
    """
    settings = request.app.state.settings
    _exiger_le_code(settings, demande.pin)

    manquants = reglages_manquants(settings)
    if manquants:
        # 503 et un message qui NOMME le réglage absent. Sans ça, le symptôme
        # d'une API Key oubliée est un 401 opaque du navigateur, à cinq minutes
        # de la démo, sans rien pour savoir lequel des cinq réglages manque.
        # Après la vérification du code, jamais avant : la liste des réglages
        # d'un serveur ne se raconte pas à un inconnu.
        raise HTTPException(
            status_code=503,
            detail=(
                "L'app n'est pas configurée côté serveur : "
                f"{', '.join(manquants)} manque(nt) dans .env. "
                "Créer une API Key et une TwiML App dans la console Twilio."
            ),
        )

    grant = VoiceGrant(
        # C'est la seule permission dont l'app a réellement besoin : elle décroche
        # des appels, elle n'en passe pas.
        incoming_allow=True,
        # La TwiML App est pourtant exigée par le grant, même pour du seul
        # entrant : sans `outgoing_application_sid`, Twilio refuse le jeton.
        outgoing_application_sid=settings.twilio_twiml_app_sid,
    )
    token = AccessToken(
        settings.twilio_account_sid,
        settings.twilio_api_key_sid,
        settings.twilio_api_key_secret,
        identity=settings.voice_client_identity,
        ttl=settings.app_token_ttl_seconds,
    )
    token.add_grant(grant)

    jwt = token.to_jwt()
    if isinstance(jwt, bytes):
        # `to_jwt()` renvoyait des octets avant twilio 7 ; `JSONResponse` ne sait
        # pas sérialiser des octets et la page recevrait une 500 sans rapport
        # avec son problème.
        jwt = jwt.decode("utf-8")

    log.info(
        "Jeton Voice émis pour l'identité « %s » (ttl %d s). Le jeton lui-même "
        "n'est pas journalisé.",
        settings.voice_client_identity,
        settings.app_token_ttl_seconds,
    )
    return {
        "token": jwt,
        "identity": settings.voice_client_identity,
        "ttl_seconds": settings.app_token_ttl_seconds,
        # Le prénom de la personne protégée, pour que l'écran d'attente dise
        # « Protection active — Jeanne » plutôt qu'un titre générique. Vide si le
        # carnet ne le déclare pas : on n'invente pas un nom.
        "owner": _owner(request),
    }


@router.post("/app/incoming")
async def appel_en_cours(demande: DemandeApp, request: Request) -> dict[str, object]:
    """Qui appelle, en ce moment, et sous quel `call_id`.

    Même code d'accès que le jeton, et pour une raison qui n'est pas la symétrie :
    cette réponse contient le numéro et le nom de la personne qui appelle une
    personne âgée. Ouverte, elle ferait de l'URL ngrok un journal d'appels
    public.

    `call_id` à `null` signifie « aucun appel actif » et ce n'est pas une erreur :
    la page interroge cette route au moment où le SDK sonne, et il arrive que le
    webhook entrant n'ait pas encore ouvert la session.
    """
    settings = request.app.state.settings
    _exiger_le_code(settings, demande.pin)

    sessions = request.app.state.registry.active()
    if not sessions:
        return {"call_id": None, "ambiguous": False}

    # Deux appels en vol : rien ici ne permet de savoir lequel fait sonner l'app
    # (le `CallSid` que voit le SDK est celui de SA jambe, pas celui de l'appel
    # d'origine). On prend le plus récent — le dictionnaire du registre garde
    # l'ordre d'insertion — mais on efface le nom et la confiance plutôt que de
    # risquer d'afficher « Contact de confiance » au-dessus du numéro d'un
    # inconnu. L'écran dira « Inconnu », ce qui est le sens prudent : au pire la
    # personne protégée se méfie d'un proche, jamais l'inverse.
    ambigu = len(sessions) > 1
    session = sessions[-1]
    if ambigu:
        log.warning(
            "%d appels actifs : l'app ne peut pas savoir lequel la fait sonner, "
            "l'appelant est présenté comme inconnu.",
            len(sessions),
        )

    contact = None
    contacts = getattr(request.app.state, "contacts", None)
    if contacts is not None and session.caller_number and not ambigu:
        contact = contacts.by_number(session.caller_number)

    return {
        "call_id": session.call_id,
        # Chaîne vide pour un appel masqué : la page écrit « Numéro masqué », ce
        # qui est une information, là où un `null` affiché serait un bug visible.
        "caller_number": session.caller_number or "",
        "caller_name": contact.name if contact else "",
        "relation": contact.relation if contact else "",
        "trusted": bool(contact and contact.is_trusted),
        "caller_held": bool(session.caller_held),
        "ambiguous": ambigu,
    }


# --- Garde-fous ------------------------------------------------------------


def _exiger_le_code(settings: Settings, pin_recu: str) -> None:
    """Refuse la requête si le code ne correspond pas exactement à `APP_PIN`.

    `APP_PIN` vide fait refuser TOUT, et c'est le seul comportement défendable :
    accepter n'importe quel code quand aucun n'est configuré distribuerait des
    jetons capables de décrocher les appels de la personne protégée à quiconque
    trouve l'URL ngrok. On préfère une app inutilisable à une app ouverte.
    """
    attendu = settings.app_pin
    if not attendu:
        log.error(
            "APP_PIN est vide : /app/token et /app/incoming refusent tout. "
            "Renseigner APP_PIN dans .env pour que l'app puisse se connecter."
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "APP_PIN n'est pas configuré sur le serveur : l'app ne peut pas "
                "obtenir de jeton. Renseigner APP_PIN dans .env puis relancer."
            ),
        )

    # `compare_digest` et non `==` : une comparaison naïve s'arrête au premier
    # caractère faux, donc son temps d'exécution révèle la longueur du préfixe
    # correct. Sur un code à quatre chiffres derrière une URL publique, ça se
    # devine chiffre par chiffre.
    if not secrets.compare_digest(pin_recu.encode("utf-8"), attendu.encode("utf-8")):
        # Ni le code attendu ni le code reçu : un journal de démo se partage, se
        # copie-colle et finit dans une capture d'écran. Le code reçu est tout
        # aussi sensible que l'attendu — à un chiffre près, c'est le même.
        log.warning(
            "Code refusé sur %s (aucun code n'est journalisé, ni l'attendu ni le reçu).",
            "/app",
        )
        raise HTTPException(status_code=403, detail="Code incorrect.")


def reglages_manquants(settings: Settings) -> list[str]:
    """Les noms `.env` qu'il manque pour signer un Access Token Voice.

    Double délibérément la liste de `Settings.app_token_configured`, qui répond
    « oui / non » quand la question utile est « lequel manque ». Les deux listes
    sont tenues cohérentes par un test, pas par la bonne volonté.
    """
    return [
        nom
        for nom, valeur in (
            ("TWILIO_ACCOUNT_SID", settings.twilio_account_sid),
            ("TWILIO_API_KEY_SID", settings.twilio_api_key_sid),
            ("TWILIO_API_KEY_SECRET", settings.twilio_api_key_secret),
            ("TWILIO_TWIML_APP_SID", settings.twilio_twiml_app_sid),
            ("VOICE_CLIENT_IDENTITY", settings.voice_client_identity),
        )
        if not valeur
    ]


def _owner(request: Request) -> str:
    """Nom de la personne protégée, tel que le carnet le déclare. `getattr` parce
    qu'un montage de test peut ne pas avoir de carnet du tout."""
    contacts = getattr(request.app.state, "contacts", None)
    return getattr(contacts, "owner", "") or ""
