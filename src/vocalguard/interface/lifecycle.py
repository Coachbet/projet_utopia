"""Libération de l'état d'un appel : un seul chemin, deux déclencheurs.

L'état d'un appel vit à trois endroits qui ne se connaissent pas : le registre de
sessions (`SessionRegistry`, qui tient les streams Transcribe), le pipeline de
score (ses caches `_llm_in_flight` / `_reputation_done` / `_claimed_identity`) et
le contrôleur Twilio (SID de conférence mémorisé, annonces en attente). Deux
événements indépendants annoncent la fin de l'appel, et aucun des deux n'est
fiable seul :

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
    known = registry is not None and registry.get(call_id) is not None

    if registry is not None:
        # Le registre d'abord : `close()` ferme les streams Transcribe, la seule
        # ressource réseau en jeu. Les deux purges suivantes ne touchent que des
        # dictionnaires en mémoire et peuvent bien attendre une milliseconde.
        try:
            await registry.close(call_id)
        except Exception:
            log.exception("Fermeture de la session %s en échec", call_id)

    freed = ["session"] if known else []
    for name in ("pipeline", "controller"):
        if _forget(getattr(state, name, None), call_id, name):
            freed.append(name)

    # On n'attend délibérément PAS `pipeline.aclose()` : il attendrait les
    # requêtes Bedrock en vol, soit plusieurs secondes, et Twilio coupe le
    # webhook bien avant. Conséquence assumée : une analyse LLM en vol peut
    # encore émettre un `score` pour un appel déjà oublié. L'événement part au
    # dashboard et s'arrête là — l'intervention ne trouvera plus de session, et
    # un point de frise en trop sur un appel terminé ne coûte rien.

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
