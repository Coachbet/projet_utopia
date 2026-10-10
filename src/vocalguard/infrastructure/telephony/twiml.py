"""Générateurs de TwiML — les réponses XML que Twilio vient chercher sur nos webhooks.

Fonctions pures, zéro réseau : c'est la brique la plus facile à tester et la plus
coûteuse à déboguer en vrai (chaque erreur se paie d'un appel téléphonique raté).

Deux contraintes du plan dictent la forme de `inbound_call_twiml` :
- `<Start><Stream>` est unidirectionnel, donc compatible avec un `<Dial>` qui suit ;
  un `<Connect><Stream>` prendrait l'appel en otage et la conférence ne se ferait pas.
- `track="both_tracks"` est ce qui nous donne les pistes `inbound` / `outbound`
  séparées, donc la capacité de savoir qui parle (appelant vs victime).
"""

from __future__ import annotations

from twilio.twiml.voice_response import VoiceResponse

DEFAULT_HOLD_MUSIC_URL = "http://com.twilio.music.classical.s3.amazonaws.com/BusyStrings.wav"
"""Musique d'attente hébergée par Twilio : rien à servir nous-mêmes, et elle reste
disponible même si notre tunnel ngrok tombe pendant la mise en attente du scammeur."""

_CONFERENCE_COMMON = {
    # La conférence démarre dès que quelqu'un entre et meurt quand une jambe part :
    # sans ça, la victime se retrouverait seule en ligne après le départ du scammeur.
    "start_conference_on_enter": True,
    "end_conference_on_exit": True,
    # Un bip annoncerait notre présence à l'appelant.
    "beep": False,
}

_STATUS_CALLBACK_EVENTS = "start end join leave"
"""`join` est l'événement qui nous dit que la victime est entrée : c'est à ce
moment-là que les actions par participant deviennent possibles."""


def inbound_call_twiml(
    *,
    stream_url: str,
    conference_name: str,
    conference_status_callback: str | None = None,
    caller_number: str = "",
) -> str:
    """Réponse au webhook de l'appel entrant (la jambe du scammeur).

    On branche le flux audio AVANT d'entrer en conférence, pour ne pas perdre les
    premières secondes — c'est souvent là que l'appelant annonce son identité.
    """
    response = VoiceResponse()

    # Pas de `<Parameter name="call_id">` : le message `start` du WebSocket porte
    # déjà le `callSid`, un paramètre de plus serait une seconde source de vérité.
    stream = response.start().stream(url=stream_url, track="both_tracks")

    # `caller_number`, en revanche, n'a pas d'autre source : Twilio ne met pas le
    # `From` dans la trame `start`. Sans ce paramètre, une session ouverte depuis
    # `/ws/media` (webhook entrant pas encore passé) n'a aucun numéro appelant,
    # donc le signal de réputation reste inerte et le coupe-circuit
    # « paiement + identité revendiquée » ne peut jamais s'armer — le score
    # plafonnerait à l'alerte sans que rien ne le dise.
    if caller_number:
        stream.parameter(name="caller_number", value=caller_number)

    conference_kwargs = dict(_CONFERENCE_COMMON)
    # Chaîne vide et non `None` : `waitUrl=""` coupe la musique d'attente Twilio.
    # Sinon elle part dans le flux `outbound` et pollue le transcript de la victime
    # avant même qu'elle décroche.
    conference_kwargs["wait_url"] = ""
    if conference_status_callback:
        conference_kwargs["status_callback"] = conference_status_callback
        conference_kwargs["status_callback_event"] = _STATUS_CALLBACK_EVENTS

    response.dial().conference(conference_name, **conference_kwargs)
    return str(response)


def join_conference_twiml(
    *,
    conference_name: str,
    conference_status_callback: str | None = None,
) -> str:
    """Réponse au webhook de la jambe sortante : la victime rejoint la même pièce.

    Pas de `<Stream>` ici : le flux de la jambe entrante capte déjà les deux pistes,
    en ouvrir un second doublerait l'audio envoyé à Transcribe (et la facture).
    """
    response = VoiceResponse()

    conference_kwargs = dict(_CONFERENCE_COMMON)
    if conference_status_callback:
        conference_kwargs["status_callback"] = conference_status_callback
        conference_kwargs["status_callback_event"] = _STATUS_CALLBACK_EVENTS

    response.dial().conference(conference_name, **conference_kwargs)
    return str(response)


def announce_twiml(message: str, *, language: str = "fr-FR") -> str:
    """TwiML servi à `announce_url` : la phrase entendue par la victime seule.

    `announce_url` joue le TwiML sur UN participant, ce qui évite le `whisper`/`coach`
    qui exige l'Agent Conference (erreur 16000 quand elle n'est pas activée).
    """
    response = VoiceResponse()
    # Le SDK échappe `&` et `<` : l'explication vient de Claude, donc de texte libre.
    response.say(message, language=language)
    return str(response)


def hold_music_twiml(*, music_url: str = DEFAULT_HOLD_MUSIC_URL) -> str:
    """TwiML servi à `hold_url` quand le scammeur passe en attente.

    `loop="0"` = boucle sans fin : si la lecture s'arrêtait, Twilio sortirait le
    participant de l'attente et il réentendrait la victime.
    """
    response = VoiceResponse()
    response.play(music_url, loop=0)
    return str(response)


def empty_twiml() -> str:
    """`<Response/>` : accuse réception sans rien faire.

    Utile pour les callbacks de statut, qui attendent du XML valide et pas un 200 vide.
    """
    return str(VoiceResponse())
