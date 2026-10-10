"""Libération de l'état d'un appel : un seul chemin, deux déclencheurs.

L'état d'un appel vit à quatre endroits qui ne se connaissent pas : le registre
de sessions (`SessionRegistry`, qui tient les streams Transcribe), le pipeline de
score (ses caches `_llm_in_flight` / `_reputation_done` / `_claimed_identity`),
le contrôleur Twilio (SID de conférence mémorisé, annonces en attente) et le
service de vérification d'identité (le code à 4 chiffres en cours et son minuteur
d'expiration — une tâche de fond qui, non annulée, se réveillerait pour parler
dans une conférence terminée). Deux événements indépendants annoncent la fin de
l'appel, et aucun des deux n'est fiable seul :

  - la fermeture du media stream — toujours émise quand l'appel se termine, mais
    pas si le `<Stream>` n'a jamais démarré ;
  - le `StatusCallback` de la jambe entrante — configuré sur le NUMÉRO dans la
    console Twilio, pas par notre code : il peut très bien ne jamais arriver.

D'où ce module : les deux chemins appellent la même fonction, la liste des états
à purger reste à un seul endroit, et un seul test la vérifie. La fonction est
idempotente parce que les deux déclencheurs arrivent souvent tous les deux, dans
un ordre imprévisible.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


async def release_call(state: Any, call_id: str) -> None:
    """Libère tout l'état de `call_id`. Ne lève jamais, même partiellement.

    `state` est le `app.state` de FastAPI (n'importe quel porteur d'attributs
    fait l'affaire : les tests passent un namespace).

    Ne jamais lever est une contrainte dure, pas de la prudence décorative : les
    deux appelants sont un `finally` de boucle média et un webhook Twilio. Une
    exception ici masquerait la vraie erreur de la boucle, ou ferait répondre 500
    à Twilio — dans les deux cas pire que la fuite qu'on cherche à éviter.
    """
    if not call_id:
        return

    registry = getattr(state, "registry", None)

    # Le registre est la seule source de vérité sur « cet appel a-t-il existé ? » :
    # les deux purges suivantes sont des `discard`/`pop`, indistinguables d'un
    # no-op. On la lit AVANT de fermer, et c'est elle qui porte l'idempotence.
    session = None
    try:
        session = registry.get(call_id) if registry is not None else None
    except Exception:
        log.warning("Lecture de la session indisponible pour %s", call_id)
    known = session is not None
    duration_s = None
    if session is not None:
        try:
            duration_s = session.t()
        except Exception:
            log.warning("Durée de la session indisponible pour %s", call_id)
    freed = ["session"] if known else []

    # Annulation avant le premier await : une réponse Bedrock ou un minuteur
    # ne doit plus provoquer d'action pendant la fermeture des flux AWS.
    for name in ("pipeline", "verifier"):
        if _forget(getattr(state, name, None), call_id, name):
            freed.append(name)

    if registry is not None:
        # `close()` retire la session du registre et la marque fermée avant
        # d'attendre AWS. Les callbacks finaux sont alors ignorés, et sa fenêtre
        # de transcript est effacée même si une tâche garde encore sa référence.
        try:
            await registry.close(call_id)
        except Exception:
            log.exception("Fermeture de la session %s en échec", call_id)

    cancel_ringing = getattr(getattr(state, "controller", None), "cancel_pending_victim", None)
    if cancel_ringing is not None:
        try:
            await cancel_ringing(call_id)
        except Exception:
            log.exception("Arrêt de la sonnerie distante en échec pour %s", call_id)

    recorder = getattr(state, "history_recorder", None)
    if recorder is not None:
        try:
            await recorder.finalize(call_id, duration_s=duration_s)
        except Exception:
            log.exception("Finalisation de l'historique en échec pour %s", call_id)

    if _forget(getattr(state, "controller", None), call_id, "controller"):
        freed.append("controller")
    _forget(getattr(state, "outgoing", None), call_id, "outgoing")
    # Dernière opération : aucun événement de fermeture ne peut rester dans la
    # mémoire de rejeu du dashboard après le nettoyage.
    _forget(getattr(state, "dashboard_hub", None), call_id, "dashboard")

    if known:
        log.info("Appel %s libéré (%s)", call_id, ", ".join(freed))


def _forget(component: Any, call_id: str, name: str) -> bool:
    """Purge l'état par appel d'un composant, s'il en tient un.

    `getattr` plutôt qu'un appel direct : `forget` n'est pas dans les ports. La
    doublure `LoggingCallController` (démo sans credentials Twilio) n'a aucun
    état par appel et donc pas de `forget` — son absence est un cas normal, pas
    une anomalie à signaler.
    """
    forget = getattr(component, "forget", None)
    if forget is None:
        return False
    try:
        forget(call_id)
    except Exception:
        log.exception("Purge de l'état %s en échec pour %s", name, call_id)
        return False
    return True
