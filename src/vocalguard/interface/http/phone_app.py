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

import json
import logging
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant

from vocalguard.application.outgoing import OutgoingAuthorizations
from vocalguard.domain.history import caller_origin
from vocalguard.infrastructure.contacts.book import CONTACT_MODES, normalize_number
from vocalguard.interface.auth import require_pin

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
    sdk_call_id: str = ""


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
    require_pin(request.app.state, demande.pin)

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
                "The app is not configured on the server: "
                f"{', '.join(manquants)} missing from .env. "
                "Create an API Key and a TwiML App in the Twilio console."
            ),
        )

    grant = VoiceGrant(
        # Les appels sortants exigent en plus une autorisation /app/outgoing,
        # liée au PIN et à la destination. Le JWT seul ne permet pas de composer.
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


class DemandeSortante(DemandeApp):
    number: str = Field(default="", max_length=64)


def outgoing_authorizations(state) -> OutgoingAuthorizations:
    authorizations = getattr(state, "outgoing", None)
    if authorizations is None:
        authorizations = OutgoingAuthorizations()
        state.outgoing = authorizations
    return authorizations


def outgoing_number(raw: str, settings: Settings) -> str:
    """Valide les seuls caractères de composition et interdit les boucles."""
    if not raw or re.fullmatch(r"[0-9+().\s-]+", raw) is None:
        raise HTTPException(422, "Enter a complete phone number.")
    # Un seul +, uniquement en tête. Ne jamais normaliser silencieusement une
    # URL, un nom client, des DTMF ou plusieurs destinations vers un numéro.
    stripped = raw.strip()
    if "+" in stripped and (not stripped.startswith("+") or stripped.count("+") != 1):
        raise HTTPException(422, "Enter a complete phone number.")
    number = normalize_number(stripped)
    if re.fullmatch(r"\+[1-9][0-9]{6,14}", number) is None:
        raise HTTPException(422, "Enter a complete number with its country code.")
    if number in {normalize_number(value) for value in settings.loop_guard_numbers}:
        raise HTTPException(422, "This number belongs to your protected line.")
    return number


@router.post("/app/outgoing")
async def autoriser_appel_sortant(demande: DemandeSortante, request: Request) -> dict[str, object]:
    """Prépare un seul appel sans composer ni facturer depuis cette route."""
    state = request.app.state
    require_pin(state, demande.pin)
    settings = state.settings
    missing = reglages_manquants(settings)
    missing.extend(
        name
        for name, value in (
            ("TWILIO_AUTH_TOKEN", settings.twilio_auth_token),
            ("TWILIO_PHONE_NUMBER", settings.twilio_phone_number),
            ("PUBLIC_HOSTNAME", settings.public_hostname),
        )
        if not value
    )
    if missing:
        raise HTTPException(503, "Outgoing calls are not configured: " + ", ".join(missing))
    number = outgoing_number(demande.number, settings)
    if state.registry.active():
        raise HTTPException(409, "End the current call before starting another one.")
    authorizations = outgoing_authorizations(state)
    authorization = authorizations.issue(identity=settings.voice_client_identity, number=number)
    if authorization is None:
        raise HTTPException(409, "An outgoing call is already in progress.")
    contacts = getattr(state, "contacts", None)
    contact = contacts.by_number(number) if contacts else None
    return {
        "authorization": authorization.token,
        "number": number,
        "expires_in": authorizations.ttl_seconds,
        "caller_name": contact.name if contact else "",
        "direction": "outgoing",
        "analyzed": False,
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
    require_pin(request.app.state, demande.pin)

    sessions = request.app.state.registry.active()
    if not sessions:
        return {"call_id": None, "ambiguous": False}

    # Le SDK voit le SID de la jambe cliente. On le relie à l'appel d'origine
    # plutôt que d'attribuer les commandes au plus récent de plusieurs appels.
    resolver = getattr(request.app.state.controller, "call_id_for_leg", None)
    parent_id = resolver(demande.sdk_call_id) if resolver and demande.sdk_call_id else None
    session = request.app.state.registry.get(parent_id) if parent_id else None
    if demande.sdk_call_id and resolver and session is None:
        return {"call_id": None, "ambiguous": len(sessions) > 1}
    ambigu = session is None and len(sessions) > 1
    if ambigu:
        return {"call_id": None, "ambiguous": True}
    session = session or sessions[0]
    contact = None
    contacts = getattr(request.app.state, "contacts", None)
    if contacts is not None and session.caller_number and not ambigu:
        contact = contacts.by_number(session.caller_number)

    origin, country = caller_origin(session.caller_number, is_contact=contact is not None)
    reputation = session.signals.reputation

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
        "origin": origin,
        "country": country,
        "line_type": reputation.line_type.value if reputation else "unknown",
    }


@router.post("/app/contacts")
async def carnet_contacts(demande: DemandeApp, request: Request) -> dict[str, object]:
    """Contacts déclarés dans le carnet, sans exposer leurs alias internes."""
    require_pin(request.app.state, demande.pin)
    return _reponse_carnet(request.app.state.contacts.all())


# --- Ajouter et retirer un contact depuis l'app ----------------------------
#
# Les deux seules routes qui écrivent `data/contacts.json`. Ce qu'elles changent
# n'est pas un affichage mais le ROUTAGE des appels : à la seconde où l'ajout
# répond, un appel de ce numéro est transmis sans annonce, sans flux audio et
# sans transcription. D'où deux partis pris :
#
#   - des refus francs (400 / 404 / 409) plutôt que des corrections
#     silencieuses. Une saisie faite dans l'app est corrigeable tout de suite
#     par la personne, contrairement à un fichier rempli à la main ;
#   - le fichier relu avant d'être réécrit, et remplacé atomiquement : il
#     contient plus que la clé `contacts`, et un JSON tronqué est un carnet vide
#     au prochain démarrage, donc tous les proches devenus des inconnus.


class DemandeAjoutContact(DemandeApp):
    """Longueurs bornées : ces valeurs partent sur le disque et reviennent à
    l'écran. `mode` est une chaîne libre et non un littéral, pour que la valeur
    refusée soit nommée dans un message lisible plutôt que dans un 422."""

    name: str = Field(default="", max_length=120)
    number: str = Field(default="", max_length=64)
    relation: str = Field(default="", max_length=120)
    mode: str = Field(default="trusted", max_length=32)
    aliases: list[str] = Field(default_factory=list, max_length=20)


class DemandeRetraitContact(DemandeApp):
    number: str = Field(default="", max_length=64)


@router.post("/app/contacts/add")
async def ajouter_contact(demande: DemandeAjoutContact, request: Request) -> dict[str, object]:
    """Ajoute un contact et renvoie le carnet à jour."""
    state = request.app.state
    require_pin(state, demande.pin)
    settings = state.settings

    nom = demande.name.strip()
    if not nom:
        # Sans nom, l'app afficherait une ligne vide et le SMS de vérification
        # n'aurait personne à nommer ; le chargeur, lui, ignorerait l'entrée —
        # l'ajout semblerait avoir fonctionné et n'aurait rien fait.
        raise HTTPException(400, "Enter the contact's name.")
    numero = _numero_de_contact(demande.number, settings)
    if demande.mode not in CONTACT_MODES:
        # Pas de repli silencieux sur « trusted » ici, contrairement à
        # `contacts/book.py` qui lit un fichier écrit à la main : la personne est
        # devant son écran, autant lui dire que sa saisie n'a pas été comprise
        # plutôt que de décider pour elle qui est écouté et qui ne l'est pas.
        raise HTTPException(
            400,
            "Unknown mode: choose \"trusted\" (call passed through without analysis) "
            "or \"analyze\" (caller is notified, then the call is analyzed).",
        )

    document, entrees = _carnet_sur_disque(settings.contacts_path)
    double = _entree_du_numero(entrees, numero)
    if double is not None:
        # Deux entrées sur un même numéro rendraient le routage non
        # déterministe : l'index du carnet n'en garde qu'une, et laquelle dépend
        # de l'ordre du fichier. Le nom déjà présent est la seule information
        # utile — c'est peut-être le bon contact sous un autre prénom.
        deja = str(double.get("name") or "").strip()
        raise HTTPException(
            409,
            f"\"{deja}\" already uses this number."
            if deja
            else "This number is already in your contacts.",
        )

    entree: dict[str, object] = {"name": nom, "phone_number": numero, "mode": demande.mode}
    relation = demande.relation.strip()
    if relation:
        entree["relation"] = relation
    alias = [valeur.strip() for valeur in demande.aliases if valeur.strip()]
    if alias:
        entree["aliases"] = alias

    _ecrire_carnet(settings.contacts_path, document, [*entrees, entree])
    log.info("Carnet : un contact ajouté en mode %s (nom et numéro non journalisés).", demande.mode)
    return _recharger_puis_repondre(request)


@router.post("/app/contacts/remove")
async def retirer_contact(demande: DemandeRetraitContact, request: Request) -> dict[str, object]:
    """Retire du carnet toutes les entrées portant ce numéro."""
    state = request.app.state
    require_pin(state, demande.pin)
    settings = state.settings

    numero = normalize_number(demande.number)
    if not numero:
        raise HTTPException(400, "Enter the number of the contact to remove.")

    document, entrees = _carnet_sur_disque(settings.contacts_path)
    # Comparaison normalisée des deux côtés : le fichier est rempli à la main
    # ("06 72 01 90 48") et l'app envoie ce qu'elle affiche. Comparer les
    # chaînes brutes ne retrouverait pas l'entrée, et le retrait répondrait 404
    # sur un contact pourtant bien présent.
    restantes = [e for e in entrees if normalize_number(str(e.get("phone_number", ""))) != numero]
    if len(restantes) == len(entrees):
        # 404 et non un succès muet : « retiré » à l'écran alors que l'appel
        # continuerait de passer sans analyse est le pire des deux.
        raise HTTPException(404, "This number is not in your contacts.")

    _ecrire_carnet(settings.contacts_path, document, restantes)
    log.info(
        "Carnet : %d entrée(s) retirée(s) (numéro non journalisé).", len(entrees) - len(restantes)
    )
    return _recharger_puis_repondre(request)


_CARNET_ILLISIBLE = (
    "The server's contact book cannot be read: no change was saved. "
    "Fix data/contacts.json and try again."
)


def _reponse_carnet(contacts) -> dict[str, object]:
    """La forme unique du carnet pour l'app : lecture, ajout et retrait renvoient
    exactement ceci, pour que la page n'ait qu'un seul format à interpréter.
    Les alias n'y figurent pas : ce sont des aides de reconnaissance internes."""
    return {
        "items": [
            {
                "name": contact.name,
                "number": contact.phone_number,
                "relation": contact.relation,
                "mode": contact.mode,
            }
            for contact in sorted(contacts, key=lambda contact: contact.name.casefold())
        ]
    }


def _nos_numeros(settings: Settings) -> set[str]:
    """Nos propres numéros, normalisés — ceux qu'un contact ne peut pas porter.

    `loop_guard_numbers` (numéro public renvoyé, ligne de secours, numéro
    Twilio) et le vrai proche du `.env`. S'ajouter soi-même au carnet n'a aucun
    sens, et surtout : un appel venant de là EST la boucle du renvoi, que la
    garde refuse. Le voir apparaître comme « contact de confiance » dans l'app
    ferait croire la garde cassée et enverrait chercher très loin.

    Les valeurs vides sont écartées : normalisés, deux numéros vides sont égaux,
    et un réglage absent interdirait alors tout numéro.
    """
    brut = (*settings.loop_guard_numbers, settings.trusted_contact_phone_number)
    return {numero for numero in (normalize_number(valeur) for valeur in brut) if numero}


def _numero_de_contact(brut: str, settings: Settings) -> str:
    """Le numéro sous la forme qui sera stockée, ou un refus.

    Normalisé AVANT tout le reste et enregistré ainsi, parce que le carnet est
    indexé par numéro normalisé et comparé à l'E.164 que Twilio envoie. Un
    « 06 00 00 00 00 » écrit tel quel ne serait jamais retrouvé : le contact
    serait au carnet et son appel resterait annoncé puis analysé.
    """
    numero = normalize_number(brut)
    if not numero:
        # Un numéro vide produirait une clé vide dans l'index du carnet, donc
        # ferait passer un appel MASQUÉ (`From` absent) pour ce contact — et
        # ferait *baisser* son score. C'est le sens dangereux, celui que
        # `contacts/book.py` écarte déjà à la lecture.
        raise HTTPException(400, "Enter the contact's phone number.")
    if numero in _nos_numeros(settings):
        raise HTTPException(
            400,
            "This number is part of your protected line: it cannot be a contact.",
        )
    return numero


def _carnet_sur_disque(path: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Le fichier entier, et ses seules entrées `contacts`.

    On relit tout pour ne remplacer qu'une clé. `data/contacts.json` porte aussi
    `owner`, des `_notes` et des `_contacts_desactives` — des entrées
    volontairement mises de côté, que le chargeur ignore puisqu'il ne lit que
    `contacts`. Réécrire le fichier depuis le seul carnet en mémoire les
    effacerait toutes, définitivement et sans un mot.

    Un fichier absent est un carnet vide : le premier ajout depuis l'app doit
    marcher sur une installation neuve. Un fichier illisible, en revanche, fait
    échouer l'écriture : réécrire par-dessus ce qu'on n'a pas su lire, c'est
    perdre son contenu.
    """
    if not path.exists():
        return {}, []
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        log.warning("Carnet illisible (%s) : écriture refusée pour ne rien perdre.", path)
        raise HTTPException(503, _CARNET_ILLISIBLE) from error
    entrees = document.get("contacts", []) if isinstance(document, dict) else None
    if not isinstance(entrees, list) or not all(isinstance(e, dict) for e in entrees):
        # La lecture accepte un tableau nu de contacts (cf. `from_path`) ;
        # l'écriture, non. Transformer ce fichier en objet, ou recopier des
        # entrées qu'on n'a pas comprises, serait restructurer le carnet de
        # quelqu'un d'autre à l'occasion d'un ajout.
        log.warning("Carnet de forme inattendue (%s) : écriture refusée.", path)
        raise HTTPException(503, _CARNET_ILLISIBLE)
    return document, entrees


def _entree_du_numero(entrees: list[dict[str, object]], numero: str) -> dict[str, object] | None:
    return next(
        (e for e in entrees if normalize_number(str(e.get("phone_number", ""))) == numero), None
    )


def _ecrire_carnet(
    path: Path, document: dict[str, object], entrees: list[dict[str, object]]
) -> None:
    """Réécrit le carnet, la clé `contacts` remplacée et le reste intact.

    Écriture atomique : fichier temporaire dans LE MÊME répertoire, puis
    `os.replace`. Écrire en place laisserait un JSON tronqué si le processus
    s'arrête ou si le disque est plein au milieu de l'écriture — donc un carnet
    vide au prochain démarrage, tous les proches devenus des inconnus, en
    silence. Le même répertoire parce que `os.replace` n'est atomique qu'à
    l'intérieur d'un système de fichiers.
    """
    temporaire: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as fichier:
            temporaire = Path(fichier.name)
            # `ensure_ascii=False` et `indent=2` : ce carnet se relit et se
            # corrige beaucoup à la main. Une seule ligne pleine de « é »
            # serait techniquement valide et pratiquement inutilisable.
            json.dump({**document, "contacts": entrees}, fichier, ensure_ascii=False, indent=2)
            fichier.write("\n")
        os.replace(temporaire, path)
    except OSError as error:
        if temporaire is not None:
            temporaire.unlink(missing_ok=True)
        log.warning("Écriture du carnet impossible (%s)", path)
        raise HTTPException(
            503,
            "The contact book could not be saved (disk full or missing "
            "permissions). Nothing was changed.",
        ) from error


def _recharger_puis_repondre(request: Request) -> dict[str, object]:
    """Relit le fichier dans l'objet partagé, puis renvoie le carnet à jour.

    Sans ce rechargement, l'écriture serait sans effet jusqu'au redémarrage :
    cinq détenteurs partagent le même carnet (`app.state.contacts`, le
    pipeline de score, la vérification d'identité, l'historique et le signal de
    réputation) et aucun ne relit le JSON. Le symptôme serait muet — on ajoute
    son fils dans l'app et il reste traité comme un inconnu.

    Un rechargement raté après une écriture réussie est signalé plutôt que
    masqué : le disque est à jour, la mémoire ne l'est pas, et répondre une
    liste périmée laisserait croire que l'ajout n'a pas été enregistré.
    """
    contacts = request.app.state.contacts
    recharger = getattr(contacts, "reload", None)
    if recharger is None or not recharger():
        raise HTTPException(
            503,
            "The contact book was saved, but the server could not reload it. "
            "Restart the server for the change to take effect.",
        )
    return _reponse_carnet(contacts.all())


# --- Garde-fous ------------------------------------------------------------


class DemandeHistorique(DemandeApp):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=50)


def _historique(request: Request):
    store = getattr(request.app.state, "history", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Call history is not available.")
    return store


@router.post("/app/history")
async def historique(demande: DemandeHistorique, request: Request) -> dict[str, object]:
    """Les cinquante appels les plus récents, paginés et protégés par le PIN."""
    require_pin(request.app.state, demande.pin)
    store = _historique(request)
    try:
        result = await store.list_calls(offset=demande.offset, limit=demande.limit)
    except (sqlite3.Error, OSError) as error:
        log.warning("Lecture de l'historique indisponible")
        raise HTTPException(status_code=503, detail="Call history is not available.") from error
    return {**result.to_dict(), "enabled": store.enabled}


# La route statique doit précéder /{call_id} : sinon « clear » serait un ID.
@router.post("/app/history/clear")
async def effacer_historique(demande: DemandeApp, request: Request) -> dict[str, object]:
    require_pin(request.app.state, demande.pin)
    store = _historique(request)
    recorder = getattr(request.app.state, "history_recorder", None)
    try:
        deleted = await recorder.clear() if recorder is not None else await store.clear()
    except (sqlite3.Error, OSError) as error:
        log.warning("Effacement de l'historique indisponible")
        raise HTTPException(status_code=503, detail="Call history is not available.") from error
    return {"ok": True, "deleted": deleted}


@router.post("/app/history/{call_id}")
async def detail_historique(
    call_id: str,
    demande: DemandeApp,
    request: Request,
) -> dict[str, object]:
    require_pin(request.app.state, demande.pin)
    store = _historique(request)
    try:
        record = await store.get_call(call_id)
    except (sqlite3.Error, OSError) as error:
        log.warning("Lecture d'un appel dans l'historique indisponible")
        raise HTTPException(status_code=503, detail="Call history is not available.") from error
    if record is None:
        raise HTTPException(status_code=404, detail="Call not found in history.")
    return record.to_dict()


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
