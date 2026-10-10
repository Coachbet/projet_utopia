"""Seuils et politique d'alerte.

La victime a le téléphone à l'oreille : on lui parle dans l'appel, pas par email.
Comme l'appel passe par une conférence Twilio, on peut agir sur chaque participant
séparément — c'est ce qui rend possible une annonce entendue de la victime seule.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum


class AlertLevel(IntEnum):
    """Ordonné pour pouvoir comparer : `level >= AlertLevel.ALERT`."""

    NONE = 0
    WATCH = 1
    ALERT = 2
    INTERVENE = 3

    @property
    def wire(self) -> str:
        return _WIRE_NAMES[self]


_WIRE_NAMES = {
    AlertLevel.NONE: "none",
    AlertLevel.WATCH: "watch",
    AlertLevel.ALERT: "alert",
    AlertLevel.INTERVENE: "intervene",
}


class ActionKind(StrEnum):
    ANNOUNCE_VICTIM = "announce_victim"
    HOLD_CALLER = "hold_caller"
    RESUME_CALLER = "resume_caller"
    SMS_CONTACT = "sms_contact"
    NOTIFY_DASHBOARD = "notify_dashboard"


@dataclass(frozen=True, slots=True)
class Thresholds:
    watch: int = 40
    alert: int = 65
    intervene: int = 85

    def level_for(self, score: int) -> AlertLevel:
        if score >= self.intervene:
            return AlertLevel.INTERVENE
        if score >= self.alert:
            return AlertLevel.ALERT
        if score >= self.watch:
            return AlertLevel.WATCH
        return AlertLevel.NONE


DEFAULT_THRESHOLDS = Thresholds()


# --- Messages prononcés dans l'appel ---------------------------------------
# Courts et sans jargon : la victime les entend une seule fois, sous stress.

ANNOUNCE_ALERT = (
    "Attention, cet appel présente des signes d'arnaque. Ne donnez aucune information bancaire."
)

SMS_VERIFY_CONTACT = "Est-ce toi qui appelles en ce moment ?"


def announce_intervene(contact_name: str | None) -> str:
    who = contact_name or "votre proche"
    return f"Nous avons mis l'appel en pause et prévenu {who}."


@dataclass(frozen=True, slots=True)
class PlannedAction:
    kind: ActionKind
    target: str
    message: str = ""


def plan_actions(
    level: AlertLevel,
    *,
    contact_name: str | None = None,
    already_held: bool = False,
    contact_already_texted: bool = False,
) -> tuple[PlannedAction, ...]:
    """Traduit un niveau en actions Twilio concrètes.

    Jamais de raccrochage automatique : un faux positif sur un vrai appel
    d'urgence serait grave. La mise en attente protège autant et laisse la
    décision à la victime.

    `already_held` et `contact_already_texted` décrivent ce qui a *déjà abouti*
    sur cet appel. Ils sont nécessaires parce que le SMS de vérification et la
    mise en attente apparaissent dans plusieurs paliers : sans eux, un score qui
    monte de 65 à 85 — le scénario exact de la démo — enverrait deux fois « Est-ce
    toi qui appelles en ce moment ? » au vrai proche, à quelques secondes
    d'intervalle. Un seul SMS par appel : c'est une question qu'on ne pose qu'une
    fois, et deux copies sont incompréhensibles pour celui qui les reçoit.

    Les deux sont keyword-only avec un défaut : le contrat est partagé avec les
    autres moitiés de l'équipe, un appelant qui ne sait rien de l'état de l'appel
    continue de recevoir le plan complet.
    """
    if level <= AlertLevel.WATCH:
        # Vigilance : rien d'audible. Le dashboard passe à l'orange.
        return ()

    if level is AlertLevel.ALERT:
        actions: list[PlannedAction] = [
            PlannedAction(ActionKind.ANNOUNCE_VICTIM, "victim", ANNOUNCE_ALERT)
        ]
        if not contact_already_texted:
            actions.append(
                PlannedAction(ActionKind.SMS_CONTACT, "trusted_contact", SMS_VERIFY_CONTACT)
            )
        return tuple(actions)

    # Intervention : le scammeur en attente d'abord, pour que l'annonce qui suit
    # ne puisse pas être entendue par lui même si l'announce_url fuite.
    actions = []
    if not already_held:
        actions.append(PlannedAction(ActionKind.HOLD_CALLER, "caller"))
    actions.append(
        PlannedAction(ActionKind.ANNOUNCE_VICTIM, "victim", announce_intervene(contact_name))
    )
    if not contact_already_texted:
        actions.append(PlannedAction(ActionKind.SMS_CONTACT, "trusted_contact", SMS_VERIFY_CONTACT))
    return tuple(actions)
