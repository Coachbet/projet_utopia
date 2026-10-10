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


class ReportedAction(StrEnum):
    """Faits publiés dans un événement `action`, hors politique d'alerte.

    Le contrat d'événement est figé à H0 et n'accepte **aucun nouveau `type`**.
    Un fait de routage (« cet appel de la famille est passé sans être analysé »)
    ou de vérification d'identité n'a donc qu'un seul canal : le champ
    `data.action`. C'est la seule extension que `contracts/event.schema.json`
    autorise, et ces six valeurs y sont déclarées — les ajouter ici sans les
    ajouter là-bas rendrait le schéma faux.

    Séparés d'`ActionKind`, et la distinction n'est pas cosmétique. `ActionKind`
    est un **effet** que `plan_actions` prescrit et qu'`application.intervention`
    applique via l'API Twilio Participants ; ceux-ci **rapportent** ce que le
    routage ou la vérification ont constaté. Les confondre a un coût immédiat et
    déjà payé : un test qui affirme « aucune action audible » après avoir ouvert
    son appel par le webhook entrant se met à échouer sur l'annonce faite à
    l'appelant, qui n'a rien à voir avec ce qu'il mesure. D'où
    `InMemoryEventSink.actions()`, qui s'appuie sur cette liste.
    """

    TRUSTED_PASSTHROUGH = "trusted_passthrough"
    """Appel d'un contact de confiance : transmis sans annonce et sans analyse."""

    CALLER_NOTICE = "caller_notice"
    """L'annonce « cet appel est analysé » a été jouée à l'appelant inconnu.
    Trace du consentement informé : c'est elle qui date l'information."""

    SPOOF_SIMULATED = "spoof_simulated"
    """Le `From` a été réécrit par `DEMO_SPOOF_AS`. Jamais en service réel."""

    VERIFY_REQUESTED = "verify_requested"
    IDENTITY_VERIFIED = "identity_verified"
    VERIFY_EXPIRED = "verify_expired"


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
    "Warning: this call shows signs of a scam. Do not share any banking details."
)

SMS_VERIFY_CONTACT = "Cut the Line: is that you calling right now? Reply YES or NO."


# --- Vérification d'identité par code à 4 chiffres (Lot 2) -----------------
#
# Un arnaqueur peut usurper le numéro affiché ; il ne peut pas lire un SMS
# envoyé au VRAI téléphone du proche. Ces textes sont les deux seules sorties du
# code de vérification : un SMS vers un numéro du carnet, et une annonce
# entendue de la personne protégée seule. Jamais dans la conférence (l'appelant
# l'entendrait et le relirait), jamais dans un log, jamais sur le bus
# d'événements — cf. `application/verification.py`.

DIGIT_PAUSE = "... "
"""Séparateur entre les chiffres énoncés.

Sans lui, la synthèse vocale lit « quatre mille huit cent vingt et un » : la
personne protégée entend un montant, pas un code, et ce qu'elle répète ne
ressemble plus au SMS que le proche a sous les yeux. Constante et non littéral
en ligne : si le test terrain montre que la voix Twilio avale les points de
suspension, il n'y a qu'un endroit à corriger."""


def say_digits(code: str) -> str:
    """« 4821 » → « 4… 8… 2… 1 », pour que la voix les énonce un à un."""
    return DIGIT_PAUSE.join(code)


def sms_verify_code(code: str, owner: str = "") -> str:
    """Le SMS qui part sur le vrai téléphone du proche.

    `owner` est le prénom de la personne protégée, tel que le carnet le déclare.
    Vide, la phrase se passe de nom : un prénom inventé ferait douter le proche
    du message entier, alors que c'est le seul message du système dont on a
    besoin qu'il soit cru immédiatement.

    « Sinon réponds NON » est là pour une raison précise : cette réponse emprunte
    le chemin existant `/twilio/sms/inbound` → `contact_replied(denied=True)`,
    donc elle arme le coupe-circuit `trusted_contact_denied` sans une ligne de
    code de plus. Changer ce mot casserait le lexique de `webhooks.py`.
    """
    who = f" {owner}" if owner else ""
    return (
        f"Cut the Line: code {code}. If you are the one calling{who} right now, "
        "read this code to them. If not, reply NO."
    )


def announce_verify_code(contact_name: str, code: str) -> str:
    """Ce que la personne protégée entend, elle seule, quand le code est parti."""
    return (
        f"A code was sent to {contact_name}. The code is {say_digits(code)}. "
        "Never say this code. Ask them to read it to you."
    )


ANNOUNCE_VERIFY_NO_CONTACT = "I can't verify this caller: they are not in your contacts."
"""Ni le numéro appelant ni l'identité revendiquée ne désignent un contact : on
n'a aucun vrai téléphone vers qui envoyer le code. On le dit, et on ne fait rien
d'autre — surtout pas d'SMS vers le numéro de l'appel, qui serait le téléphone
de l'arnaqueur lui-même."""

ANNOUNCE_VERIFY_SAME_NUMBER = (
    "I can't verify: the code would go to the phone of the person who is calling."
)
"""Garde-fou : le destinataire retenu est le numéro depuis lequel on appelle,
alors qu'on y est arrivé par l'identité revendiquée et pas par le carnet. Envoyer
quand même donnerait le code à vérifier à celui qu'on vérifie."""

ANNOUNCE_VERIFY_SMS_FAILED = "I could not send the code. You can try again."

ANNOUNCE_VERIFY_OK = "Code correct. Identity verified."

ANNOUNCE_VERIFY_WRONG = "That is not the right code. Ask them to repeat it."
"""Dit dès qu'un code faux est entendu, sans rien conclure : la transcription se
trompe régulièrement d'un chiffre, et accuser sur une erreur de reconnaissance
mettrait en attente un vrai petit-fils. On l'informe, elle fait répéter."""

ANNOUNCE_VERIFY_EXPIRED = "The code was not read. The caller's identity is not verified."

ANNOUNCE_VERIFY_UNHEARD = (
    "I could not hear this call. If this person did not read you the code, be careful."
)
"""Fin de fenêtre sur un appel qu'on n'écoute pas (contact de confiance : aucun
`<Stream>`, donc aucun transcript) ou dont la transcription est tombée. On ne
peut rien affirmer, donc on n'affirme rien et on lui rend la décision."""


def announce_intervene(contact_name: str | None) -> str:
    who = contact_name or "your trusted contact"
    return f"We have put the call on hold and alerted {who}."


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
