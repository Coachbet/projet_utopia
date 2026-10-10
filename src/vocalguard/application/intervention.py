"""Traduire un score en actions sur l'appel : la part Dev 1 de la boucle.

Dev 2 publie des événements `score` ; ce module les écoute et exécute ce que la
politique d'alerte (`domain.alerts.plan_actions`, déjà écrite) prescrit. Aucune
décision n'est prise ici : pas de seuil, pas de message, pas de choix d'action.

Le point délicat est l'idempotence. Le score oscille naturellement d'un segment
au suivant ; sans garde, un score qui passe de 66 à 70 à 67 répéterait l'annonce
dans l'oreille de la victime toutes les deux secondes pendant la démo.

Mais l'idempotence ne porte que sur ce qui a **réussi**. Une action ratée doit
rester rejouable, parce que l'échec le plus fréquent est purement temporel : la
victime décroche plusieurs secondes après l'appelant, et `announce_to_victim`
exige que le participant victime soit déjà dans la conférence. Il suffit que le
scammeur débite son texte pendant la sonnerie pour que le score atteigne 85
avant le décroché — l'appelant partirait en attente et la victime n'entendrait
plus jamais rien. On suit donc le détail des actions abouties
(`CallSession.actions_done`) et pas un simple palier atteint.

Le déclencheur idéal pour rejouer une annonce en attente n'est pas un score mais
l'arrivée de la victime : `/twilio/conference/status` reçoit déjà l'événement
`participant-join` (le TwiML déclare `statusCallbackEvent="start end join
leave"`) et n'en fait qu'un `log.info`. Le brancher sur une relance d'annonce
appartient à `interface/http/webhooks.py`, hors du périmètre de ce module ;
signalé, pas codé.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from vocalguard.application.sessions import CallSession, SessionRegistry
from vocalguard.config import Settings
from vocalguard.domain.alerts import (
    ActionKind,
    AlertLevel,
    PlannedAction,
    plan_actions,
)
from vocalguard.domain.events import Event, EventType
from vocalguard.domain.models import RiskAssessment

if TYPE_CHECKING:
    from vocalguard.domain.ports import CallController, EventSink

logger = logging.getLogger(__name__)

_LEVELS_BY_WIRE = {level.wire: level for level in AlertLevel}


def _as_tuple(value: Any) -> tuple[str, ...]:
    """Une chaîne au lieu d'une liste donnerait un tuple de lettres : on refuse."""
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value)
    return ()


class InterventionRunner:
    def __init__(
        self,
        *,
        controller: CallController,
        sink: EventSink,
        settings: Settings,
        registry: SessionRegistry,
    ) -> None:
        self._controller = controller
        self._sink = sink
        self._settings = settings
        self._registry = registry

    async def emit(self, event: Event) -> None:
        """Port `EventSink` : alias de `handle_event`.

        Permet de s'abonner directement au `FanOutSink` plutôt que d'exiger un
        adaptateur au point de câblage.
        """
        await self.handle_event(event)

    async def handle_event(self, event: Event) -> None:
        """Point d'entrée depuis le bus de Dev 2 : ignore tout sauf les `score`.

        Ne lève jamais : une exception ici remonterait dans le bus et couperait
        la diffusion des événements vers le dashboard.
        """
        try:
            if event.type is not EventType.SCORE:
                return

            data = event.data if isinstance(event.data, dict) else {}
            try:
                score = int(data["score"])
            except (KeyError, TypeError, ValueError):
                logger.warning("événement score sans score exploitable, ignoré : %s", data)
                return

            level = _LEVELS_BY_WIRE.get(str(data.get("level", "")))
            if level is None:
                # Tolérance d'intégration : Dev 2 produit parfois des événements
                # de test sans `level`. On le recalcule depuis les seuils plutôt
                # que de jeter un score valide.
                level = self._settings.thresholds().level_for(score)
                logger.debug("niveau absent de l'événement score, recalculé : %s", level.wire)

            session = self._registry.get(event.call_id)
            if session is None:
                logger.warning("score reçu pour un appel inconnu : %s", event.call_id)
                return

            await self.handle_score(
                session,
                RiskAssessment(
                    score=score,
                    level=level,
                    reasons=_as_tuple(data.get("reasons")),
                    breakers_fired=_as_tuple(data.get("breakers_fired")),
                    explanation=str(data.get("explanation", "")),
                ),
            )
        except Exception:
            logger.exception("événement score ingérable, ignoré")

    async def handle_score(self, session: CallSession, assessment: RiskAssessment) -> None:
        level = assessment.level
        if level <= session.acted_level:
            # Palier déjà entièrement abouti : on ne refait rien d'audible.
            return

        actions = plan_actions(
            level,
            contact_name=self._contact_name(session),
            already_held=session.caller_held,
            contact_already_texted=session.contact_texted,
        )

        done = session.actions_done.setdefault(level, set())
        # Réservation : on filtre puis on marque sans aucun `await` entre les
        # deux, donc deux événements `score` traités en parallèle ne peuvent pas
        # réserver la même action — c'est la propriété que donnait l'ancien
        # `acted_level` marqué avant exécution, en plus fin.
        #
        # `done` est aussi ce qui évite une deuxième mise en attente : il
        # mémorise HOLD_CALLER même après que l'opérateur a repris l'appel à la
        # main (`interface/http/control.py` remet `caller_held` à False sans
        # toucher `acted_level`, donc `plan_actions` replanifie le hold). La
        # décision humaine l'emporte toujours sur le score, comme documenté
        # là-bas, et elle y survit même si `acted_level` est resté en arrière à
        # cause d'une action ratée.
        pending = tuple(action for action in actions if action.kind not in done)
        done.update(action.kind for action in pending)

        if not pending:
            # Rien à faire : tout a abouti, ou tout est en vol dans une autre
            # tâche. On ne touche pas à `acted_level`, c'est à la tâche qui
            # exécute réellement de dire si le palier est complet.
            return

        logger.info(
            "appel %s → %s (score %d) : %d action(s) sur %d à exécuter",
            session.call_id,
            level.wire,
            assessment.score,
            len(pending),
            len(actions),
        )

        all_ok = True
        for action in pending:
            ok = await self._execute(session, action)
            if not ok:
                # On rend la réservation : un score suivant rejouera cette seule
                # action. C'est ainsi que l'annonce finit par être entendue quand
                # la victime a rejoint la conférence.
                done.discard(action.kind)
                all_ok = False
            await session.emit(
                EventType.ACTION,
                {"action": action.kind.value, "target": action.target, "ok": ok},
            )

        if all_ok:
            # Le palier n'avance que si tout a réussi. Sinon il reste en arrière
            # pour qu'un prochain score repasse ici — avec, grâce à `done`, les
            # seules actions manquantes.
            session.acted_level = max(session.acted_level, level)

    def _contact_name(self, session: CallSession) -> str | None:
        """Le nom prononcé dans l'annonce d'intervention (« prévenu Lucas »).

        On réutilise l'identité revendiquée par l'appelant : c'est précisément la
        personne dont on vient de vérifier le vrai numéro par SMS. On ne garde que
        ce qui précède la virgule, Claude renvoyant « Lucas, petit-fils ».
        """
        content = session.signals.content
        if content is None or not content.claimed_identity:
            return None
        return content.claimed_identity.split(",")[0].strip() or None

    async def _execute(self, session: CallSession, action: PlannedAction) -> bool:
        """Une action en échec n'empêche pas les suivantes : si la mise en attente
        rate, on veut quand même que la victime soit avertie."""
        try:
            match action.kind:
                case ActionKind.HOLD_CALLER:
                    ok = await self._controller.hold_caller(session.call_id)
                    if ok:
                        session.caller_held = True
                    return ok
                case ActionKind.RESUME_CALLER:
                    ok = await self._controller.resume_caller(session.call_id)
                    if ok:
                        session.caller_held = False
                    return ok
                case ActionKind.ANNOUNCE_VICTIM:
                    return await self._controller.announce_to_victim(
                        session.call_id, action.message
                    )
                case ActionKind.SMS_CONTACT:
                    number = self._settings.trusted_contact_phone_number
                    if not number:
                        logger.warning(
                            "TRUSTED_CONTACT_PHONE_NUMBER vide : pas de SMS de vérification"
                        )
                        return False
                    ok = await self._controller.send_sms(number, action.message)
                    if ok:
                        # Seul un SMS *parti* ferme la porte : un envoi refusé par
                        # Twilio doit pouvoir être retenté au score suivant.
                        session.contact_texted = True
                    return ok
                case ActionKind.NOTIFY_DASHBOARD:
                    # Le dashboard reçoit déjà tous les événements : rien à pousser.
                    return True
                case _:
                    logger.warning("action inconnue %s, ignorée", action.kind)
                    return False
        except Exception:
            logger.exception("action %s impossible sur %s", action.kind.value, session.call_id)
            return False
