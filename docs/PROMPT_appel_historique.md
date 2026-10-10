# Prompt — VocalGuard : écran d'appel réaliste, historique des appels, annonce en anglais

> À coller tel quel dans l'IA de code qui travaille sur ce dépôt (`projet_utopia`).
> Contexte : les lots 1 → 4 de `docs/PROMPT_un_seul_numero.md` sont faits, 779 tests verts,
> l'app sonne sur le téléphone réel (`/app`, SDK Voice JS, `VOICE_CLIENT_IDENTITY`).

## 0. Règles (inchangées)

1. `make test` et `make lint` restent verts ; chaque lot ajoute ses tests.
2. Le contrat d'événement (`contracts/event.schema.json`) est figé : aucun nouveau `type`.
3. Jamais d'audio ni de transcript persistés. L'historique ne stocke **que des métadonnées et des chiffres** (voir Lot B).
4. Aucun secret dans le code, les logs, les commits.
5. Une branche par lot, commits petits, récapitulatif en fin de lot (ce qui marche, ce qui a été testé au vrai téléphone, ce qui reste).
6. Avant de commencer, lis `src/vocalguard/interface/static/app.html`, `interface/http/phone_app.py`, `interface/http/webhooks.py`, `infrastructure/telephony/twiml.py`, `application/scoring.py`, `interface/lifecycle.py`.

---

## Lot A — L'écran d'appel doit ressembler à un vrai appel

But : quand l'app sonne, on doit croire à l'écran d'appel natif d'un smartphone. Mobile d'abord (iPhone Safari + Chrome Android), lisible par une personne âgée.

**Appel entrant (plein écran)**
- Fond plein écran sombre et dégradé subtil ; en haut, petite ligne « VocalGuard · appel protégé ».
- Au centre : **avatar rond** avec les initiales du contact (ou une icône de silhouette pour un inconnu), **nom en très grand**, numéro dessous, puis la provenance en petit : « Mobile · France » / « Numéro masqué » / « Étranger · +44 » (déduite de l'indicatif ; type de ligne Lookup si dispo).
- Badge sous le nom : vert « Contact de confiance » ou orange « Inconnu — appel analysé ».
- En bas : deux **gros boutons ronds** comme iOS/Android : rouge « Refuser » à gauche, vert « Accepter » à droite, avec une animation de pulsation sur le vert.
- **Sonnerie** (son généré en Web Audio, deux tonalités type téléphone, en boucle) + **vibration** (`navigator.vibrate`, Android uniquement — iOS l'ignore, ne pas planter). Démarrer l'audio uniquement après une interaction (contrainte navigateur) : le bouton « Activer la protection » sert de geste de déverrouillage audio.
- **Wake Lock** (`navigator.wakeLock.request('screen')`) quand la protection est active, pour que l'écran ne se mette pas en veille ; tolérer le refus.

**Appel en cours**
- Même en-tête (avatar, nom, provenance) + **chronomètre** `00:42` qui démarre au décroché.
- **Grille de 6 boutons ronds** façon téléphone : Muet (`call.mute()`), Haut-parleur (si `setSinkId` dispo, sinon masqué), Clavier (affiche un pavé qui envoie des DTMF via `call.sendDigits`), **Vérifier** (route `/control/calls/{id}/verify`), **Attente** (hold/resume existants), Contacts (désactivé, pour plus tard).
- Bouton rouge **Raccrocher** centré en bas.
- **Bandeau de risque** discret en haut de l'écran pour un appel analysé : pastille de couleur + mot (« Normal », « Vigilance », « Alerte », « Danger ») + score. Il grandit en carte d'alerte rouge plein écran au niveau alerte/intervention, avec le texte « Ne donnez aucun code ni numéro de carte » — refermable.
- Le **code de vérification** s'affiche en grand dans une feuille qui monte du bas, avec le compte à rebours, et la consigne : « **Ne dites jamais ce code. C'est à lui de vous le lire.** » (corrige le point 8 de l'audit).
- Fin d'appel : écran « Appel terminé · 03:12 » 2 secondes, puis retour à l'écran d'attente.

**Écran d'attente**
- « Protection active » + état (connecté / micro), + accès à l'**Historique** (Lot C).

Tests : tests d'intégration qui servent `/app` et vérifient la présence des éléments clés (ids stables), et un test JS minimal si le dépôt en a l'outillage ; sinon, captures d'écran Playwright (390×844) des 4 états jointes au récapitulatif.

---

## Lot B — Stocker les métriques de chaque appel (sans audio ni transcript)

Créer un stockage local **SQLite** (`data/calls.sqlite`, gitignoré), via un nouveau port `CallHistory` dans `domain/ports.py` et un adapter `infrastructure/history/sqlite.py` (module `sqlite3` de la stdlib, appels dans `asyncio.to_thread`). Un abonné au bus (comme `InterventionRunner`) remplit la table au fil des événements ; la ligne est finalisée dans `lifecycle.release_call`.

**Une ligne par appel :**

| Colonne | Contenu |
|---|---|
| `call_id`, `started_at`, `ended_at`, `duration_s` | horodatage UTC ISO, durée |
| `caller_number` | E.164, ou vide si masqué |
| `caller_name`, `relation` | depuis le carnet, si connu |
| `origin` | **provenance** : `contact`, `inconnu`, `masqué`, `étranger`, + pays déduit de l'indicatif (`FR`, `GB`, `US`…) |
| `line_type` | Lookup (`mobile`, `nonFixedVoip`…) ou `unknown` |
| `route` | `trusted_passthrough` ou `analyzed` |
| `answered` | décroché ou non |
| `max_score`, `max_level` | pic du score 0–100 et niveau atteint |
| `watch_at_s`, `alert_at_s`, `intervene_at_s` | **les 3 seuils** : seconde de l'appel où chacun a été franchi pour la première fois (vide si jamais) |
| `peak_llm`, `peak_keywords`, `peak_voice`, `peak_reputation` | pic de chaque signal (0–1) |
| `tactics` | liste JSON des tactiques détectées par Claude (`urgence`, `paiement inhabituel`…) |
| `explanation` | la dernière explication courte de Claude (une phrase, déjà produite, pas un transcript) |
| `actions` | liste JSON des actions faites (`caller_notice`, `announce_victim`, `hold_caller`, `sms_contact`, `verify_requested`, `identity_verified`, `contact_denied`…) |
| `verification` | `none`, `verified`, `failed`, `expired` |

Règles : rien d'autre (ni texte de transcript, ni audio) ; rétention configurable `HISTORY_RETENTION_DAYS=30` avec purge au démarrage ; réglage `HISTORY_ENABLED=true`. Les appels de confiance sont enregistrés aussi (provenance, durée), mais leurs colonnes de score restent vides puisqu'ils ne sont pas analysés.

Tests : création de table, une ligne par appel, seuils horodatés au bon moment (scénario rejoué via `make replay`), aucun texte de transcript en base (test qui cherche un mot du scénario dans le fichier SQLite et doit ne rien trouver), purge.

---

## Lot C — Historique dans l'app

- Routes `POST /app/history` (liste paginée, 50 derniers) et `POST /app/history/{call_id}` (détail), **protégées par `APP_PIN`** comme `/app/token`.
- `POST /app/history/clear` pour tout effacer (bouton avec confirmation dans la page, pas de `confirm()` natif).
- Écran **Historique** façon « Récents » d'un téléphone : une ligne par appel avec avatar/initiales, nom ou numéro, **provenance** (« Mobile · France », « Masqué »…), heure, durée, et une **pastille de niveau** (vert / orange / rouge / noir pour intervention). Appel manqué en rouge.
- **Détail d'un appel** : provenance complète, durée, décroché ou non, **frise des 3 seuils** (une barre horizontale de 0 à la durée de l'appel avec les marqueurs Vigilance / Alerte / Intervention à la seconde où ils ont été franchis), pics des 4 signaux en barres, tactiques détectées, explication de Claude, actions effectuées, résultat de la vérification.

---

## Lot D — L'annonce à l'appelant en anglais

- `CALLER_NOTICE_TEXT` par défaut : « **This call is being analyzed by a fraud protection service.** »
- Nouveau réglage `CALLER_NOTICE_LANGUAGE=en-US`, utilisé par le `<Say>` de l'annonce appelant uniquement (aujourd'hui `SAY_LANGUAGE` est commun à toutes les annonces dans `twiml.py`). Les annonces faites **à la personne protégée** restent en français (`fr-FR`).
- Option `CALLER_NOTICE_BILINGUAL=false` : si `true`, la phrase anglaise est suivie de la française (« Cet appel est analysé par un service de protection contre la fraude. »).
- Le texte reste honnête : « analyzed », jamais « recorded », puisque rien n'est enregistré.
- Tests TwiML : langue `en-US` sur l'annonce appelant, `fr-FR` ailleurs, ordre Say → Stream → Conference inchangé.

---

## Lot E — Correctifs de l'audit à faire en même temps (ils touchent les mêmes fichiers)

1. **App fermée = appel perdu** (`client.py`, `dial_victim`) : `timeout=25` sur la jambe app ; si elle finit en `no-answer`/`failed`/`busy`, rappeler `VICTIM_PHONE_NUMBER` s'il est rempli, sinon dire à l'appelant « The person is not available. » et raccrocher sa jambe. L'appel apparaît « manqué » dans l'historique. Ajouter une tonalité de retour d'appel pour l'appelant pendant qu'il attend (au lieu du silence de `wait_url=""`).
2. **`/control/*` et `/ws/dashboard` protégés par `APP_PIN`** (en-tête pour les routes, paramètre de connexion pour le WebSocket), l'app passant le PIN qu'elle a déjà en mémoire. Historique du hub effacé pour un appel quand il se termine.
3. **Code de vérification** : n'accepter qu'un candidat de **4 chiffres exactement**, au plus **2 essais**, puis échec (même effet qu'un démenti du proche).
4. **PIN** : 6 chiffres minimum, blocage 10 minutes après 5 échecs.

---

## Ordre et définition de « fini »

Ordre : E1 → A → D → B → C → E2–E4.

Fini quand : tests et lint verts ; captures Playwright des écrans (entrant, en cours avec risque, code, fin, historique, détail) dans le récapitulatif ; un vrai appel test au téléphone a produit une ligne d'historique correcte avec sa provenance et ses seuils ; l'annonce entendue par l'appelant est en anglais ; README et `.env.example` mis à jour (`CALLER_NOTICE_LANGUAGE`, `CALLER_NOTICE_BILINGUAL`, `HISTORY_ENABLED`, `HISTORY_RETENTION_DAYS`).
