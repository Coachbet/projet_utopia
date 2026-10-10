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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from vocalguard.infrastructure.telephony.announcements import AnnouncementStore
from vocalguard.infrastructure.telephony.twiml import unavailable_twiml, voicemail_twiml

if TYPE_CHECKING:
    from vocalguard.config import Settings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _VictimLeg:
    leg: str
    sid: str | None = None
    answered: bool = False
    terminal: bool = False
    ready: asyncio.Event = field(default_factory=asyncio.Event)


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
        self._victim_legs: dict[str, _VictimLeg] = {}
        self._leg_parents: dict[str, str] = {}
        self._caller_numbers: dict[str, str] = {}
        self._caller_streams: set[str] = set()
        self._voicemail: Any = None

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

        # Réserver avant le premier await : un webhook rejoué ne fait pas
        # sonner deux fois la personne protégée.
        if not to_number and call_id in self._victim_legs:
            return self._victim_legs[call_id].sid

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
            await self._notify_unavailable(call_id)
            return None

        vers_app = not to_number and bool(app_target)
        pending = _VictimLeg(leg="app" if vers_app else "phone")
        self._victim_legs[call_id] = pending
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
                    status_callback=(
                        f"{base}/twilio/voice/status?"
                        f"{urlencode({'call_id': call_id, 'leg': pending.leg})}"
                    ),
                    status_callback_method="POST",
                    status_callback_event=["initiated", "ringing", "answered", "completed"],
                    timeout=25,
                )
            )
        except TwilioRestException as exc:
            # En compte d'essai, un numéro non vérifié tombe ici (code 21219).
            logger.warning("Appel vers la victime échoué (code Twilio %s) : %s", exc.code, exc.msg)
            await self._failed_victim_leg(call_id, pending)
            current = self._victim_legs.get(call_id)
            return current.sid if current is not None and current is not pending else None
        except Exception:
            logger.warning("Appel vers la victime échoué (panne réseau ?)", exc_info=True)
            await self._failed_victim_leg(call_id, pending)
            current = self._victim_legs.get(call_id)
            return current.sid if current is not None and current is not pending else None
        # L'appelant a pu raccrocher pendant le create REST. Ne pas réinsérer
        # une jambe oubliée ; annuler celle qui vient d'être créée.
        if self._victim_legs.get(call_id) is not pending:
            await self._cancel_unanswered_leg(call.sid)
            return None
        pending.sid = call.sid
        self._leg_parents[call.sid] = call_id
        pending.ready.set()
        return call.sid

    def call_id_for_leg(self, call_sid: str) -> str | None:
        """Associe le CallSid du SDK victime à l'appel entrant correspondant."""
        return self._leg_parents.get(call_sid)

    async def victim_status(self, call_id: str, call_sid: str, *, leg: str, status: str) -> str:
        """Traite uniquement le statut de la jambe attendue, une seule fois.

        Le retour informe le webhook : `answered`, `fallback`, `unavailable`,
        `completed` ou `ignored`. Aucun échec de vérification ne termine un appel.
        """
        pending = self._victim_legs.get(call_id)
        if pending is not None and pending.leg == leg:
            # Le callback peut précéder le retour REST. Attendre son SID réel
            # avant comparaison : le paramètre call_id seul ne suffit jamais.
            await pending.ready.wait()
        if (
            pending is None
            or self._victim_legs.get(call_id) is not pending
            or pending.leg != leg
            or not call_sid
            or pending.terminal
            or pending.sid != call_sid
        ):
            return "ignored"
        if status == "in-progress":
            if pending.answered:
                return "ignored"
            pending.answered = True
            return "answered"
        if status in {"no-answer", "failed", "busy", "canceled"}:
            if pending.answered:
                pending.terminal = True
                return "completed"
            return await self._failed_victim_leg(call_id, pending)
        if status == "completed":
            pending.terminal = True
            return "completed"
        return "ignored"

    async def _failed_victim_leg(self, call_id: str, pending: _VictimLeg) -> str:
        if self._victim_legs.get(call_id) is not pending or pending.terminal:
            return "ignored"
        pending.terminal = True
        pending.ready.set()
        fallback = self._settings.victim_phone_number
        if pending.leg == "app" and fallback:
            logger.info("App injoignable pour %s : repli téléphonique", call_id)
            sid = await self.dial_victim(call_id, to_number=fallback)
            return "fallback" if sid else "unavailable"
        await self._notify_unavailable(call_id)
        return "unavailable"

    async def _notify_unavailable(self, call_id: str) -> bool:
        """Fin d'une tentative sans réponse, avant toute conversation."""
        if not self._ready("unavailable"):
            return False
        twiml = unavailable_twiml()
        if self._voicemail is not None and self._voicemail.enabled:
            try:
                expected = await self._voicemail.expect(
                    call_id, caller_number=self._caller_numbers.get(call_id, "")
                )
                if expected:
                    base = self._settings.public_base_url
                    twiml = voicemail_twiml(
                        done_url=f"{base}/twilio/voicemail/done",
                        status_url=f"{base}/twilio/voicemail/status",
                        stop_analysis=call_id in self._caller_streams,
                    )
            except Exception:
                logger.warning("Messagerie indisponible pour %s", call_id)
        try:
            await asyncio.to_thread(
                lambda: self._client.calls(call_id).update(  # type: ignore[union-attr]
                    twiml=twiml
                )
            )
        except Exception:
            logger.warning("Annonce d'indisponibilité impossible pour %s", call_id)
            return False
        return True

    def configure_voicemail(self, service: Any) -> None:
        self._voicemail = service

    def remember_caller(self, call_id: str, caller_number: str, *, has_stream: bool = True) -> None:
        self._caller_numbers[call_id] = caller_number
        if has_stream:
            self._caller_streams.add(call_id)
        else:
            self._caller_streams.discard(call_id)

    async def _cancel_unanswered_leg(self, call_sid: str) -> None:
        try:
            await asyncio.to_thread(
                lambda: self._client.calls(call_sid).update(status="canceled")  # type: ignore[union-attr]
            )
        except Exception:
            logger.warning("Annulation de la jambe sans réponse impossible")

    async def cancel_pending_victim(self, call_id: str) -> None:
        """Arrête la sonnerie si l'appelant est parti avant le décroché.

        Retirer la réservation avant le réseau empêche les callbacks en retard
        de créer un repli sur un appel terminé. Une jambe déjà en conversation
        n'est jamais annulée ici ; son départ dépend de la conférence.
        """
        pending = self._victim_legs.pop(call_id, None)
        if pending is None:
            return
        pending.ready.set()
        for sid, parent in tuple(self._leg_parents.items()):
            if parent == call_id:
                self._leg_parents.pop(sid, None)
        if pending.sid and not pending.answered and not pending.terminal:
            await self._cancel_unanswered_leg(pending.sid)

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
            # Le détail fournisseur peut refléter le body, qui contient parfois
            # un code privé. Seul le diagnostic numérique est journalisé.
            logger.warning("SMS échoué (code Twilio %s)", exc.code)
            return False
        except Exception:
            logger.warning("SMS échoué (panne réseau ?)")
            return False
        return True

    def forget(self, call_id: str) -> None:
        """Fin d'appel : libère le SID de conférence et les annonces en attente."""
        self._conference_sids.pop(call_id, None)
        self._announcements.clear(call_id)
        self._caller_numbers.pop(call_id, None)
        self._caller_streams.discard(call_id)
        pending = self._victim_legs.pop(call_id, None)
        if pending is not None:
            pending.ready.set()
        for sid, parent in tuple(self._leg_parents.items()):
            if parent == call_id:
                self._leg_parents.pop(sid, None)
