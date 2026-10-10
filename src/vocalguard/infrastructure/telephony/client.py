"""Actions sur l'appel en cours, via l'API Twilio Participants.

Implémente le port `CallController`. C'est le seul endroit du projet qui parle au
réseau Twilio en écriture ; tout ce qui décide *quand* agir vit dans le domaine.

Trois partis pris :
- Le SDK Twilio est synchrone. Chaque appel réseau part dans `asyncio.to_thread`,
  sinon on bloque la boucle qui reçoit l'audio (50 trames/s) et le transcript décroche.
- Une action qui échoue renvoie `False` et ne lève pas, QUELLE QUE SOIT la panne.
  Chaque appel réseau rattrape donc `TwilioRestException` — pour en journaliser le
  code, qui est le seul diagnostic utile pendant une démo — *et* `Exception`, parce
  que le SDK ne lève pas que des erreurs Twilio : un timeout HTTP sur le wifi du
  lieu remonte en `ConnectionError`. Pendant un appel réel, une annonce ratée ne
  doit pas tuer la session : le dashboard et le score continuent.
  Cette promesse est vérifiée par `tests/integration/test_port_semantics.py`.
- Aucune méthode de raccrochage, même privée. Un faux positif sur un vrai appel
  d'urgence serait grave ; la mise en attente protège autant et laisse la décision
  à la victime (cf. `domain.alerts.plan_actions`).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from vocalguard.infrastructure.telephony.announcements import AnnouncementStore

if TYPE_CHECKING:
    from vocalguard.config import Settings

logger = logging.getLogger(__name__)


class TwilioCallController:
    """Agit sur les participants de la conférence d'un appel."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: Client | None = None,
        announcements: AnnouncementStore | None = None,
    ) -> None:
        self._settings = settings
        self._announcements = announcements or AnnouncementStore()
        # Le SID de conférence est mémorisé par appel : le retrouver par nom coûte
        # un aller-retour HTTP, et on agit parfois trois fois en deux secondes.
        self._conference_sids: dict[str, str] = {}

        if client is not None:
            self._client: Client | None = client
        elif settings.twilio_configured:
            self._client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        else:
            # Démarrage sans credentials assumé : Dev 2 et Biz 1 travaillent sur de
            # faux événements tant que la téléphonie n'est pas prête.
            self._client = None
            logger.warning(
                "Twilio non configuré (TWILIO_ACCOUNT_SID/AUTH_TOKEN vides) : "
                "les actions d'appel seront ignorées."
            )

    @property
    def announcements(self) -> AnnouncementStore:
        return self._announcements

    # --- Garde-fous --------------------------------------------------------

    def _ready(self, action: str) -> bool:
        """Vérifie qu'on peut réellement appeler Twilio avant de le tenter."""
        if self._client is None:
            logger.warning("%s ignoré : client Twilio absent.", action)
            return False
        if not self._settings.public_hostname:
            # Sans hostname public, `hold_url` et `announce_url` pointeraient vers
            # `https:///...` et Twilio échouerait côté serveur, pas chez nous.
            logger.warning(
                "%s ignoré : PUBLIC_HOSTNAME vide, Twilio ne pourrait pas "
                "rappeler nos URLs (ngrok lancé ?).",
                action,
            )
            return False
        return True

    # --- Résolution des participants --------------------------------------

    async def _conference_sid(self, call_id: str) -> str | None:
        """SID de la conférence de cet appel, par son nom (`conference_name_for`)."""
        cached = self._conference_sids.get(call_id)
        if cached:
            return cached

        friendly_name = self._settings.conference_name_for(call_id)
        try:
            conferences = await asyncio.to_thread(
                lambda: self._client.conferences.list(  # type: ignore[union-attr]
                    friendly_name=friendly_name, status="in-progress", limit=1
                )
            )
        except TwilioRestException as exc:
            logger.warning(
                "Conférence %s introuvable (code Twilio %s) : %s",
                friendly_name,
                exc.code,
                exc.msg,
            )
            return None
        except Exception:
            # Le SDK ne lève pas que des `TwilioRestException` : un timeout HTTP
            # (le wifi du lieu, pas une hypothèse) remonte en `ConnectionError`.
            logger.warning("Conférence %s illisible (panne réseau ?)", friendly_name, exc_info=True)
            return None

        if not conferences:
            # Cas normal avant que la victime décroche : la conférence n'existe pas
            # encore côté Twilio.
            logger.info("Conférence %s pas encore en cours.", friendly_name)
            return None

        sid = conferences[0].sid
        self._conference_sids[call_id] = sid
        return sid

    async def _participants(self, call_id: str) -> tuple[Any | None, Any | None]:
        """Renvoie `(appelant, victime)`.

        L'appelant est la jambe entrante, donc le participant dont le `call_sid`
        est le `CallSid` de l'appel d'origine — qui est aussi notre `call_id`.
        Tout autre participant est la victime (jambe sortante).
        """
        conference_sid = await self._conference_sid(call_id)
        if conference_sid is None:
            return None, None

        try:
            participants = await asyncio.to_thread(
                lambda: self._client.conferences(  # type: ignore[union-attr]
                    conference_sid
                ).participants.list()
            )
        except TwilioRestException as exc:
            logger.warning(
                "Participants illisibles pour %s (code Twilio %s) : %s",
                call_id,
                exc.code,
                exc.msg,
            )
            return None, None
        except Exception:
            logger.warning(
                "Participants illisibles pour %s (panne réseau ?)", call_id, exc_info=True
            )
            return None, None

        caller = next((p for p in participants if p.call_sid == call_id), None)
        victim = next((p for p in participants if p.call_sid != call_id), None)
        if caller is None or victim is None:
            logger.info(
                "Conférence %s incomplète : appelant=%s victime=%s",
                call_id,
                caller is not None,
                victim is not None,
            )
        return caller, victim

    async def _update_participant(
        self, call_id: str, which: str, action: str, **fields: Any
    ) -> bool:
        """Applique un `update` sur l'appelant ou la victime, sans jamais lever."""
        caller, victim = await self._participants(call_id)
        participant = caller if which == "caller" else victim
        if participant is None:
            logger.warning("%s ignoré : participant %s absent de la conférence.", action, which)
            return False

        conference_sid = self._conference_sids.get(call_id)
        try:
            await asyncio.to_thread(
                lambda: (
                    self._client.conferences(  # type: ignore[union-attr]
                        conference_sid
                    )
                    .participants(participant.call_sid)
                    .update(**fields)
                )
            )
        except TwilioRestException as exc:
            # 16000 = whisper/coach indisponible hors Agent Conference. On ne s'en
            # sert pas, mais le code apparaît si quelqu'un réactive cette piste.
            logger.warning("%s échoué (code Twilio %s) : %s", action, exc.code, exc.msg)
            return False
        except Exception:
            logger.warning("%s échoué (panne réseau ?)", action, exc_info=True)
            return False
        return True

    # --- Port CallController ----------------------------------------------

    async def dial_victim(
        self,
        call_id: str,
        *,
        to_number: str | None = None,
        caller_number: str = "",
        caller_name: str = "",
    ) -> str | None:
        """Fait sonner la personne protégée et la fait entrer dans la conférence.

        Renvoie le SID de la jambe créée, `None` si l'appel n'a pas pu être passé.

        Deux destinations possibles, dans cet ordre :

          1. **l'app** (`client:<VOICE_CLIENT_IDENTITY>`) dès qu'elle est
             configurée. C'est le montage « un seul numéro » : la personne
             protégée renvoie sa ligne vers notre numéro Twilio, donc rappeler
             son numéro public nous le ferait renvoyer vers Twilio — une boucle
             facturée à chaque tour. Une jambe `client:` n'a pas ce problème, ne
             demande pas de seconde SIM, coûte bien moins qu'une minute vers un
             mobile français, et c'est elle qui permet d'afficher le vrai numéro
             de l'appelant à l'écran ;
          2. **le téléphone** (`VICTIM_PHONE_NUMBER`), le comportement d'avant
             l'app, qui reste le repli indispensable : une page web ne sonne que
             si elle est ouverte (cf. `interface/http/phone_app.py`).

        `caller_number` et `caller_name` sont optionnels et par défaut vides,
        pour que les appelants existants — `webhooks.py` n'en passe pas encore —
        gardent exactement le comportement actuel. Ils voyagent dans la query
        string de l'URL de la jambe : c'est le seul canal que `calls.create`
        laisse disponible. La méthode documentée par Twilio pour atteindre
        `Call.customParameters` côté navigateur est la noun `<Parameter>` de
        `<Dial><Client>`, que notre jambe n'emprunte pas, et l'API REST ne
        documente aucun paramètre personnalisé pour `to="client:…"`.

        À quoi ils servent, concrètement, et à quoi ils ne servent PAS : ils
        laissent une trace dans le journal Twilio et dans le nôtre, et ils sont
        le point d'accroche d'un futur `<Dial><Client><Parameter>`. Ils ne
        nourrissent pas l'annonce du repli téléphonique — `voice_join`
        reconstruit « Appel de Lucas Martin » depuis la session et le carnet, à
        la demande, exprès pour ne pas créer une seconde source de vérité. Et
        l'app, elle, lit l'appelant sur `POST /app/incoming`, qui interroge ces
        mêmes sources. Ne rien câbler dessus sans vérifier d'abord au vrai
        téléphone que Twilio transmet quelque chose.
        """
        if not self._ready("dial_victim"):
            return None

        app_target = self._settings.voice_client_target
        destination = to_number or app_target or self._settings.victim_phone_number
        if not destination:
            # L'ancien message ne parlait que de `VICTIM_PHONE_NUMBER` : depuis
            # que l'app existe, il y a deux façons de faire sonner la personne
            # protégée et une seule suffit. Nommer les deux évite de chercher
            # pendant dix minutes un numéro dont on n'a plus besoin.
            logger.warning(
                "dial_victim ignoré : personne à faire sonner. Renseigner "
                "VOICE_CLIENT_IDENTITY (l'app sur le téléphone) ou, à défaut, "
                "VICTIM_PHONE_NUMBER (le repli téléphonique)."
            )
            return None

        vers_app = not to_number and bool(app_target)
        if vers_app:
            logger.info("Jambe victime vers l'app (%s) pour l'appel %s", destination, call_id)
        else:
            logger.info(
                "Jambe victime vers le téléphone (repli) (%s) pour l'appel %s",
                destination,
                call_id,
            )

        base = self._settings.public_base_url
        url = f"{base}/twilio/voice/join/{call_id}"
        # Query string et non corps POST : c'est Twilio qui vient chercher ce
        # TwiML, nous ne contrôlons que l'URL. Les valeurs vides sont écartées
        # pour que l'URL d'un appel sans appelant connu reste celle d'avant —
        # `?caller_number=` ne veut rien dire de plus que son absence.
        extras = {
            cle: valeur
            for cle, valeur in (("caller_number", caller_number), ("caller_name", caller_name))
            if valeur
        }
        if extras:
            url = f"{url}?{urlencode(extras)}"

        try:
            call = await asyncio.to_thread(
                lambda: self._client.calls.create(  # type: ignore[union-attr]
                    to=destination,
                    from_=self._settings.twilio_phone_number,
                    url=url,
                    method="POST",
                    status_callback=f"{base}/twilio/voice/status",
                    status_callback_method="POST",
                )
            )
        except TwilioRestException as exc:
            # En compte d'essai, un numéro non vérifié tombe ici (code 21219).
            logger.warning("Appel vers la victime échoué (code Twilio %s) : %s", exc.code, exc.msg)
            return None
        except Exception:
            logger.warning("Appel vers la victime échoué (panne réseau ?)", exc_info=True)
            return None
        return call.sid

    async def hold_caller(self, call_id: str) -> bool:
        """Met le scammeur en attente, avec musique — il n'entend plus la victime."""
        if not self._ready("hold_caller"):
            return False
        base = self._settings.public_base_url
        return await self._update_participant(
            call_id,
            "caller",
            "hold_caller",
            hold=True,
            hold_url=f"{base}/twilio/voice/hold-music",
            hold_method="POST",
        )

    async def resume_caller(self, call_id: str) -> bool:
        """Sort le scammeur de l'attente : la victime peut vouloir reprendre l'appel."""
        if not self._ready("resume_caller"):
            return False
        return await self._update_participant(call_id, "caller", "resume_caller", hold=False)

    async def announce_to_victim(self, call_id: str, message: str) -> bool:
        """Fait entendre `message` à la victime seule.

        Le texte est déposé dans la file avant l'appel API, parce que Twilio peut
        venir chercher `announce_url` immédiatement.
        """
        if not self._ready("announce_to_victim"):
            return False

        self._announcements.queue(call_id, message)
        base = self._settings.public_base_url
        ok = await self._update_participant(
            call_id,
            "victim",
            "announce_to_victim",
            announce_url=f"{base}/twilio/voice/announce/{call_id}",
            announce_method="POST",
        )
        if not ok:
            # Ne pas laisser une annonce orpheline : elle serait jouée au prochain
            # `announce_url` réussi, avec un décalage incompréhensible.
            # `discard_latest` et non `pop` : `pop` dépile la plus ancienne, donc
            # annuler celle-ci supprimerait une annonce précédente encore en
            # attente de réclamation par Twilio.
            self._announcements.discard_latest(call_id, message)
        return ok

    async def send_sms(self, to_number: str, message: str) -> bool:
        """SMS au vrai proche : « Est-ce toi qui appelles en ce moment ? »."""
        if self._client is None:
            logger.warning("send_sms ignoré : client Twilio absent.")
            return False
        if not to_number:
            logger.warning("send_sms ignoré : numéro de destination vide.")
            return False

        try:
            await asyncio.to_thread(
                lambda: self._client.messages.create(  # type: ignore[union-attr]
                    to=to_number,
                    from_=self._settings.twilio_phone_number,
                    body=message,
                )
            )
        except TwilioRestException as exc:
            logger.warning("SMS échoué (code Twilio %s) : %s", exc.code, exc.msg)
            return False
        except Exception:
            logger.warning("SMS échoué (panne réseau ?)", exc_info=True)
            return False
        return True

    def forget(self, call_id: str) -> None:
        """Fin d'appel : libère le SID de conférence et les annonces en attente."""
        self._conference_sids.pop(call_id, None)
        self._announcements.clear(call_id)
