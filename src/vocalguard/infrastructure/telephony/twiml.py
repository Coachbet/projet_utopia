"""Générateurs de TwiML — les réponses XML que Twilio vient chercher sur nos webhooks.

Fonctions pures, zéro réseau : c'est la brique la plus facile à tester et la plus
coûteuse à déboguer en vrai (chaque erreur se paie d'un appel téléphonique raté).

Trois contraintes du plan dictent la forme de `inbound_call_twiml` :
- `<Start><Stream>` est unidirectionnel, donc compatible avec un `<Dial>` qui suit ;
  un `<Connect><Stream>` prendrait l'appel en otage et la conférence ne se ferait pas.
- `track="both_tracks"` est ce qui nous donne les pistes `inbound` / `outbound`
  séparées, donc la capacité de savoir qui parle (appelant vs victime).
- l'annonce à l'appelant, quand il y en a une, est le PREMIER verbe du document :
  tout ce que Twilio exécute après elle est, par construction, postérieur à
  l'information de l'appelant. C'est le montage juridique du produit, pas une
  préférence de mise en page — un `<Stream>` placé avant capterait de l'audio
  qu'on n'a pas encore le droit d'analyser, et aucun test d'intégration ne
  saurait le rattraper après coup.
"""

from __future__ import annotations

from twilio.twiml.voice_response import VoiceResponse

SAY_LANGUAGE = "fr-FR"
"""Langue de toutes nos synthèses vocales. Partagée plutôt que recopiée : une
voix anglaise lisant « Cet appel est analysé » est incompréhensible au téléphone,
et le symptôme ne se voit dans aucun test — il s'entend."""

STREAM_TRACK_BOTH = "both_tracks"
"""Appelant ET personne protégée : le mode d'un appel inconnu, donc analysé.
C'est la séparation des pistes qui nous dit qui parle."""

STREAM_TRACK_VICTIM_ONLY = "outbound_track"
"""La voix de la personne protégée SEULE (Lot 3).

Sur la jambe entrante, la piste `outbound` est ce que l'appelant ENTEND, donc ce
que la personne protégée dit (`media_stream.py` mappe `outbound` → `victim`).
L'interlocuteur n'est jamais capté : c'est la seule raison pour laquelle écouter
un appel de confiance est envisageable — l'abonnée a consenti, son
correspondant non. **À valider juridiquement** avant toute mise en service."""

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
    caller_notice: str = "",
    track: str | None = STREAM_TRACK_BOTH,
) -> str:
    """Réponse au webhook de l'appel entrant (la jambe de l'appelant).

    Trois formes, décidées par le routage de `/twilio/voice/inbound` :

    - **inconnu** (`caller_notice` rempli, `track=STREAM_TRACK_BOTH`) :
      `<Say>` puis `<Start><Stream>` puis la conférence ;
    - **confiance** (`caller_notice` vide, `track=None`) : la conférence seule,
      aucune annonce et aucun octet d'audio — la famille n'est pas écoutée, et
      l'appelant n'entend rien qui trahisse qu'il a été reconnu ;
    - **confiance + Lot 3** (`track=STREAM_TRACK_VICTIM_ONLY`) : toujours aucune
      annonce, mais la piste de la personne protégée seule.

    `track=None` est la seule façon de ne pas écouter, et c'est volontairement un
    acte explicite : si l'absence d'écoute découlait d'un paramètre OUBLIÉ (une
    URL de flux vide, par exemple), un appel à analyser pourrait cesser de l'être
    en silence. Le défaut écoute, le refus se demande.

    Le flux est branché AVANT la conférence pour ne pas perdre les premières
    secondes — c'est souvent là que l'appelant annonce son identité — mais APRÈS
    l'annonce, qui doit précéder toute capture (cf. le module).
    """
    response = VoiceResponse()

    if caller_notice:
        # Premier verbe du document : l'appelant est informé avant que quoi que
        # ce soit d'autre ne commence. Twilio exécute les verbes dans l'ordre, et
        # c'est cet ordre-là, pas un commentaire, qui porte la promesse.
        response.say(caller_notice, language=SAY_LANGUAGE)

    if track is not None:
        # Pas de `<Parameter name="call_id">` : le message `start` du WebSocket
        # porte déjà le `callSid`, un paramètre de plus serait une seconde
        # source de vérité.
        stream = response.start().stream(url=stream_url, track=track)

        # `caller_number`, en revanche, n'a pas d'autre source : Twilio ne met
        # pas le `From` dans la trame `start`. Sans ce paramètre, une session
        # ouverte depuis `/ws/media` (webhook entrant pas encore passé) n'a aucun
        # numéro appelant, donc le signal de réputation reste inerte et le
        # coupe-circuit « paiement + identité revendiquée » ne peut jamais
        # s'armer — le score plafonnerait à l'alerte sans que rien ne le dise.
        if caller_number:
            stream.parameter(name="caller_number", value=caller_number)

    conference_kwargs = dict(_CONFERENCE_COMMON)
    # Chaîne vide et non `None` : `waitUrl=""` coupe la musique d'attente Twilio.
    # Sinon elle part dans le flux `outbound` et pollue le transcript de la victime
    # avant même qu'elle décroche.
    #
    # Appliqué aussi en mode confiance, où il n'y a pourtant aucun transcript à
    # polluer : une musique d'attente qui ne se déclencherait que pour les
    # appelants reconnus apprendrait à qui rappelle deux fois s'il est au carnet
    # ou non. Le routage ne doit s'entendre d'aucun côté de la ligne.
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
    victim_notice: str = "",
) -> str:
    """Réponse au webhook de la jambe sortante : la victime rejoint la même pièce.

    Pas de `<Stream>` ici : le flux de la jambe entrante capte déjà les deux pistes,
    en ouvrir un second doublerait l'audio envoyé à Transcribe (et la facture).

    `victim_notice` est dit AVANT l'entrée en conférence, donc entendu par la
    personne protégée seule : l'appelant n'est pas encore sur la même ligne
    qu'elle. C'est ce qui permet de lui annoncer qui appelle sans que l'appelant
    l'entende — son téléphone, lui, n'affiche que notre numéro Twilio.
    """
    response = VoiceResponse()

    if victim_notice:
        response.say(victim_notice, language=SAY_LANGUAGE)

    conference_kwargs = dict(_CONFERENCE_COMMON)
    if conference_status_callback:
        conference_kwargs["status_callback"] = conference_status_callback
        conference_kwargs["status_callback_event"] = _STATUS_CALLBACK_EVENTS

    response.dial().conference(conference_name, **conference_kwargs)
    return str(response)


def announce_twiml(message: str, *, language: str = SAY_LANGUAGE) -> str:
    """TwiML servi à `announce_url` : la phrase entendue par la victime seule.

    `announce_url` joue le TwiML sur UN participant, ce qui évite le `whisper`/`coach`
    qui exige l'Agent Conference (erreur 16000 quand elle n'est pas activée).
    """
    return _say_only(message, language=language)


def refusal_twiml(message: str, *, language: str = SAY_LANGUAGE) -> str:
    """TwiML d'un appel qu'on refuse d'acheminer : une phrase, puis c'est fini.

    Même XML qu'`announce_twiml`, sens opposé — d'où deux fonctions et non un
    paramètre : celle-ci répond à un appel qui n'entrera dans aucune conférence
    (la garde anti-boucle de `/twilio/voice/inbound`), alors que l'autre parle à
    quelqu'un qui est déjà en ligne. Les confondre ferait raccrocher au nez d'une
    victime le jour où l'on changerait l'une des deux.

    Pourquoi une phrase plutôt qu'`empty_twiml()` : un `<Response/>` fait
    raccrocher Twilio sans un mot, ce qui est indistinguable d'une panne du
    serveur — exactement le diagnostic qu'on ne veut pas avoir à faire en pleine
    démo, et exactement ce qu'entendrait la personne protégée si elle composait
    son propre numéro renvoyé. Ce n'est pas le raccrochage automatique que le
    projet s'interdit : il n'y a ici aucune conversation à couper, seulement une
    boucle de renvoi à ne pas créer.
    """
    return _say_only(message, language=language)


def _say_only(message: str, *, language: str) -> str:
    """`<Response><Say>` et rien d'autre."""
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
