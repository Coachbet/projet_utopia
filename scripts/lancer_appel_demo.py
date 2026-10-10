#!/usr/bin/env python3
"""Démarre un appel de démo en faisant appeler Twilio, au lieu d'appeler Twilio.

POURQUOI CE SCRIPT EXISTE

Le montage nominal veut que le « scammeur » compose le numéro Twilio. Depuis la
France vers un numéro américain, c'est l'opérateur du mobile qui facture, à un
tarif qui dépend du forfait et qu'on découvre sur la facture.

Recevoir un appel en France est gratuit. On inverse donc le sens : Twilio appelle
le téléphone du « scammeur » et le branche sur `/twilio/voice/inbound`, qui se
comporte exactement comme pour un appel entrant — il ouvre la session, démarre
le `<Stream>`, rappelle la victime et monte la conférence. Le coût bascule
entièrement sur le compte Twilio, où le tarif est connu d'avance
(~0,16 USD/min par jambe vers un mobile français).

LE CHEMIN DE CODE RESTE CELUI DE LA PRODUCTION : `voice_inbound` prend le
`CallSid` du webhook comme `call_id`, et `TwilioCallController._participants`
identifie l'appelant par `participant.call_sid == call_id`. Les deux restent
vrais pour une jambe sortante.

Seul `From` change de sens : il vaut notre propre `TWILIO_PHONE_NUMBER` plutôt
que le téléphone du scammeur. Depuis le Lot 1 ce n'est plus anodin — la garde
anti-boucle de `/twilio/voice/inbound` refuse justement les appels venant de nos
propres numéros (sinon le renvoi d'appel de la ligne protégée nous ferait
rappeler nous-mêmes, à deux jambes facturées par tour). Sans précaution, ce
script se ferait donc refuser par notre propre protection.

D'où les deux paramètres ajoutés à l'URL du webhook :

    ?demo_outbound=1&presented_from=<téléphone du scammeur>

`demo_outbound=1` désarme la garde anti-boucle POUR CET APPEL seulement — un
réglage `.env` la désarmerait pour les vrais appels aussi, ce qui est exactement
le risque qu'elle existe pour couvrir. `presented_from` dit au serveur quelle
identité l'appelant présente réellement sur le plateau : le téléphone du
scammeur, pas notre relais. Le routage voit alors le même `From` qu'un vrai appel
entrant, donc la liste blanche, l'annonce à l'appelant et `DEMO_SPOOF_AS`
(scénario C : « Lucas » appelle depuis le numéro de Lucas) se jouent sans aucune
branche de code spéciale.

USAGE

    uv run python scripts/lancer_appel_demo.py                 # vers SCAMMER_PHONE_NUMBER ou --to
    uv run python scripts/lancer_appel_demo.py --to +33600000000
    uv run python scripts/lancer_appel_demo.py --dry-run       # tout vérifier, ne rien appeler

Le script refuse de partir si un prérequis manque, plutôt que de brûler du
crédit sur un montage incomplet.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vocalguard.config import Settings, get_settings
from vocalguard.infrastructure.contacts.book import normalize_number

TARIF_MOBILE_FR = 0.1603
"""USD par minute et par jambe vers un mobile français, relevé sur le compte."""


def _verifier(settings: Settings, destination: str) -> list[str]:
    """Tout ce qui doit être vrai avant de dépenser un centime."""
    problemes: list[str] = []

    if not settings.twilio_configured:
        problemes.append("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN manquants dans .env")
    if not settings.twilio_phone_number:
        problemes.append("TWILIO_PHONE_NUMBER vide : aucun numéro d'où appeler")
    if not settings.public_hostname:
        problemes.append(
            "PUBLIC_HOSTNAME vide : Twilio ne pourrait joindre ni le <Stream> ni "
            "les webhooks (tunnel lancé ?)"
        )
    if not settings.victim_phone_number:
        problemes.append(
            "VICTIM_PHONE_NUMBER vide : la jambe victime ne partirait pas et il "
            "n'y aurait personne dans la conférence"
        )
    if not destination:
        problemes.append(
            "aucun numéro de destination : passer --to, ou renseigner "
            "SCAMMER_PHONE_NUMBER dans .env"
        )

    for nom, valeur in (
        ("TWILIO_PHONE_NUMBER", settings.twilio_phone_number),
        ("VICTIM_PHONE_NUMBER", settings.victim_phone_number),
        ("destination", destination),
    ):
        if valeur and valeur != normalize_number(valeur):
            problemes.append(
                f"{nom} n'est pas en E.164 strict ({len(valeur)} caractères) : "
                f"Twilio le refuserait avec un code 21211 opaque"
            )

    if destination and settings.victim_phone_number:
        if normalize_number(destination) == normalize_number(settings.victim_phone_number):
            problemes.append(
                "le « scammeur » et la victime sont le MÊME numéro : le second "
                "appel tomberait sur une ligne occupée"
            )

    return problemes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fait appeler Twilio vers le téléphone du « scammeur » et le branche "
            "sur le flux de démo. Évite de payer un appel international depuis un "
            "mobile français."
        )
    )
    parser.add_argument(
        "--to",
        default="",
        help="numéro du « scammeur » en E.164 (défaut : SCAMMER_PHONE_NUMBER du .env)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="vérifie tout et affiche ce qui serait fait, sans passer l'appel",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    # `SCAMMER_PHONE_NUMBER` est devenu un champ de `Settings` avec le Lot 4 : le
    # serveur en a besoin pour armer `DEMO_SPOOF_AS`. On le lit donc là et plus
    # dans `os.environ` — deux lectures du même réglage finissent par diverger,
    # et ici ça se traduirait par un script qui appelle un numéro que le serveur
    # ne reconnaît pas comme celui de l'arnaqueur (donc pas d'usurpation simulée,
    # sans un mot d'explication).
    destination = normalize_number(args.to or settings.scammer_phone_number)

    problemes = _verifier(settings, destination)
    if problemes:
        print("\n  Prérequis manquants — rien n'a été appelé :\n")
        for p in problemes:
            print(f"    - {p}")
        print()
        return 1

    base = settings.public_base_url
    # Les deux paramètres sont expliqués en tête de fichier. En résumé :
    # `demo_outbound=1` désarme la garde anti-boucle pour cette jambe (son `From`
    # est notre propre numéro Twilio), et `presented_from` donne au routage
    # l'identité réellement au bout du fil — sans quoi la démo montrerait la
    # liste blanche en train de refuser notre propre relais.
    url_inbound = f"{base}/twilio/voice/inbound?" + urlencode(
        {"demo_outbound": "1", "presented_from": destination}
    )

    print()
    print("  APPEL DE DÉMO")
    print(f"    Twilio appelle    {destination}        (le « scammeur », décroche)")
    print(f"    depuis            {settings.twilio_phone_number}")
    print(f"    puis rappelle     {settings.victim_phone_number}   (la victime)")
    print(f"    vu par le routage comme un appel entrant de {destination}")
    print(f"    TwiML             {url_inbound}")
    print(f"    coût estimé       ~{2 * TARIF_MOBILE_FR:.2f} USD/min (deux jambes FR)")
    # Dit à voix haute, jamais en silence : ce réglage fait croire au serveur que
    # l'appel vient d'un contact du carnet. L'opérateur de la démo doit savoir
    # quel scénario il est en train de lancer (A/B ou C).
    if settings.demo_spoof_active and normalize_number(destination) == normalize_number(
        settings.scammer_phone_number
    ):
        print(
            f"    USURPATION SIMULÉE  l'appel sera traité comme venant de "
            f"{settings.demo_spoof_as} (DEMO_SPOOF_AS)"
        )
    elif settings.demo_spoof_as:
        # L'autre moitié du même principe : un réglage renseigné qui ne
        # s'appliquera PAS doit se dire aussi, sinon le scénario C se joue sans
        # usurpation et personne ne comprend pourquoi la liste blanche a
        # fonctionné « normalement ».
        print(
            "    DEMO_SPOOF_AS est renseigné mais ne s'appliquera pas : il exige "
            "SCAMMER_PHONE_NUMBER, et que ce soit bien lui qu'on appelle"
        )
    print()

    if args.dry_run:
        print("  --dry-run : aucun appel passé. Tout est prêt.\n")
        return 0

    from twilio.base.exceptions import TwilioRestException
    from twilio.rest import Client

    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    try:
        call = client.calls.create(
            to=destination,
            from_=settings.twilio_phone_number,
            url=url_inbound,
            method="POST",
            status_callback=f"{base}/twilio/voice/status",
            status_callback_method="POST",
        )
    except TwilioRestException as exc:
        print(f"  Appel refusé par Twilio (code {exc.code}) : {exc.msg}")
        if exc.code == 21219:
            print("  → numéro non vérifié : le compte est-il repassé en trial ?")
        elif exc.code == 21211:
            print("  → numéro invalide : vérifier la forme E.164 (+33 puis 9 chiffres)")
        print()
        return 1

    print(f"  Appel créé : {call.sid}")
    print("  Décroche sur le téléphone « scammeur ». La victime sonnera juste après.")
    print()
    print("  Pour suivre ce qui se passe :")
    print("    curl -s localhost:8001/healthz")
    print("    (active_calls doit contenir l'appel pendant la conversation)")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
