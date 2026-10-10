# Lot 0 — Vérifications terrain

> **Ce document se déroule avec un vrai téléphone en main et la console Twilio ouverte.**
> Aucune de ces sept vérifications ne peut être faite depuis le code : trois coûtent de
> l'argent, toutes exigent un combiné physique et un réseau mobile français.
>
> Mode d'emploi : on descend dans l'ordre. Chaque section porte une case à cocher, un
> emplacement **Résultat observé** à remplir à la main (date + ce qu'on a vraiment vu,
> pas ce qu'on espérait), et une **décision** si le résultat est mauvais.
>
> Règle : **rien n'est coché tant que ça n'a pas été observé.** Un « ça devrait marcher »
> n'est pas un résultat.

## 0. Récapitulatif

| # | Vérification | Coût | État | Résultat |
|---|---|---|---|---|
| 1 | Permissions géographiques Twilio (Voice + Messaging) vers la France | 0 | **à faire** | |
| 2 | Renvoi d'appel sur le numéro public (`**21*`) | facture opérateur | **à faire** | |
| 3 | **Que vaut `From` sur l'appel renvoyé ?** | ~1 appel | **à faire** | |
| 4 | SMS aller-retour Twilio US ↔ mobile FR | ~2 SMS | **à faire** | |
| 5 | Les touches clavier (DTMF) remontent-elles ? | ~1 appel | **à faire** | |
| 6 | L'app s'ouvre sur le téléphone réel et sonne | ~1 appel | **à faire** | |
| 7 | L'annonce n'est entendue que par la personne protégée | ~1 appel | **à faire** | |

Les vérifications 3, 5, 6 et 7 peuvent être groupées **dans un seul appel de test** si
on prépare les points d'observation avant de décrocher (voir §8, « Faire les quatre d'un
coup »). C'est ce qu'on fera si le budget ou le temps serre.

## 0 bis. Ce qui bloque quoi

| Si cette vérification échoue | … alors ceci est bloqué |
|---|---|
| **1 — Geo permissions Voice** | tout repli téléphonique `VICTIM_PHONE_NUMBER`, le script `lancer_appel_demo.py`, **les trois scénarios de démo** si on ne passe pas par l'app |
| **1 — Geo permissions Messaging** | Lot 2 en entier (le code part par SMS), l'action `SMS_CONTACT` des niveaux alerte et intervention, le chemin `/twilio/sms/inbound` |
| **2 — Renvoi d'appel** | le montage « un seul numéro » tel qu'il est pitché. Repli : on fait composer le numéro Twilio directement, mais on perd l'argument produit |
| **3 — `From` ≠ numéro de l'appelant** | **Lot 1 en entier** (liste blanche, garde anti-boucle, `identity_mismatch`), donc aussi le Lot 2 (choix du destinataire du code) et les scénarios A et C. **C'est le point de non-retour du projet** |
| **4 — SMS aller-retour** | le démenti du proche par SMS. Le coupe-circuit `trusted_contact_denied` retombe sur son seul autre déclencheur, le bouton `POST /control/calls/{id}/contact-denied` |
| **5 — DTMF** | le déclencheur n°1 du Lot 2 (« appuyez sur 1 »). Les déclencheurs 2 (phrase « vérification ») et 3 (bouton de l'app) restent. **Non bloquant**, mais il faut le savoir avant d'écrire le code |
| **6 — L'app** | Lot 1 bis, l'affichage du vrai numéro de l'appelant, le bouton « Vérifier son identité », et la critère de fin « le test réel de l'utilisateur marche » |
| **7 — Annonce isolée** | les niveaux alerte et intervention. Si l'arnaqueur entend l'annonce, il raccroche et comprend le système : repli sur « mettre en attente avant chaque annonce », ou Agent Conference (`TWILIO_AGENT_CONFERENCE_ENABLED`) |

## Avant de commencer — pré-requis communs

```bash
make run                  # terminal 1, laisser tourner, c'est le journal qu'on lira
make tunnel               # terminal 2 ; noter l'hôte ngrok dans PUBLIC_HOSTNAME du .env
curl -s localhost:8000/healthz   # terminal 3
```

`healthz` doit renvoyer `twilio_configured: true` et un `public_hostname` non nul. S'il
renvoie `null`, `PUBLIC_HOSTNAME` n'est pas renseigné : aucun `<Stream>` ni callback ne
pourra fonctionner et toutes les vérifications suivantes échoueront pour cette seule
raison. **Après avoir modifié `.env`, redémarrer `make run`** (les réglages sont mis en
cache par processus via `get_settings`).

Console Twilio, les deux URLs à coller sur le numéro
(Phone Numbers → Manage → Active numbers → *le numéro* → onglet Configure) :

| Champ | Valeur |
|---|---|
| Voice → **A CALL COMES IN** → Webhook | `https://<hôte ngrok>/twilio/voice/inbound` — **HTTP POST** |
| Messaging → **A MESSAGE COMES IN** → Webhook | `https://<hôte ngrok>/twilio/sms/inbound` — **HTTP POST** |

L'hôte ngrok gratuit change à chaque redémarrage du tunnel : **les deux champs sont à
recoller à chaque fois**, et `PUBLIC_HOSTNAME` avec. C'est la cause n°1 de « ça marchait
hier ».

---

## 1. Permissions géographiques Twilio vers la France

- [ ] **Vérifié**

**Objectif** — autoriser le compte à appeler et à écrire vers la France (indicatif `+33`).

**Pourquoi ça peut tout casser** — les permissions géographiques sont **restrictives par
défaut** sur un compte neuf, et leur refus est muet du côté du code : `dial_victim` et
`send_sms` attrapent l'exception et la journalisent, l'appel part « normalement » et
simplement personne ne sonne, personne ne reçoit le SMS. On passerait l'heure suivante à
chercher un bug dans le TwiML.

**Manipulation**

1. Console Twilio → **Voice** → **Settings** → **Geo permissions** (parfois intitulé
   « Voice Geographic Permissions »). Vérifier que **France** est cochée côté *Low Risk*.
   Sauvegarder.
2. Console Twilio → **Messaging** → **Settings** → **Geo permissions**. Vérifier que
   **France** est cochée. Sauvegarder.
3. Vérifier aussi, tant qu'on y est, que le compte n'est pas en **trial** : Console →
   Account → Billing. Un compte d'essai n'appelle et n'écrit que vers des numéros
   **vérifiés** (Phone Numbers → Manage → Verified Caller IDs). Si on est en trial,
   ajouter là les trois téléphones de la démo (personne protégée, arnaqueur, vrai Lucas),
   ou créditer le compte.

**Résultat attendu** — France cochée dans les deux écrans, et les téléphones de la démo
présents dans Verified Caller IDs si le compte est en trial.

**Résultat observé** (date, qui, ce qui a été vu)

```
Voice geo FR      :
Messaging geo FR  :
Compte trial ?    :
Numéros vérifiés  :
```

**Si c'est mauvais** — cocher et sauvegarder, c'est gratuit et immédiat. Si la case
France est grisée, le compte est restreint : il faut le créditer. **Ne pas continuer**
les vérifications 2 à 7 avant que celle-ci soit verte, elles échoueraient toutes pour
cette raison et on tirerait les mauvaises conclusions.

---

## 2. Renvoi d'appel sur le numéro public

- [ ] **Vérifié**

**Objectif** — faire que tout appel vers le vrai numéro de la personne protégée arrive
sur notre numéro Twilio, sans qu'elle change de numéro.

**Pourquoi ça peut tout casser** — c'est le montage « un seul numéro » tout entier. Sans
renvoi, il n'y a pas de produit : il reste une démo où on compose un numéro américain.

> ### ⚠️ Avertissement de coût — à lire à l'utilisateur avant de composer le code
>
> Le renvoi d'appel est **facturé par l'opérateur du numéro renvoyé**, c'est-à-dire **à la
> personne protégée**, pas à l'appelant. Comme la destination est notre numéro Twilio
> américain, chaque appel renvoyé est facturé **comme un appel vers les États-Unis**,
> et ce type d'appel est **très souvent hors forfait** (ordre de grandeur courant :
> quelques dizaines de centimes d'euro la minute — `À VÉRIFIER` sur le forfait concerné).
>
> Avant d'activer : **faire ouvrir l'espace client de l'opérateur** et vérifier la ligne
> « appels vers l'international / zone États-Unis » du forfait. Puis **faire des tests
> courts** (30 à 60 s, on raccroche dès que le point est observé) et **désactiver le
> renvoi entre deux sessions de test** avec `##21#`.
>
> La roadmap répond à ce problème par un **numéro français** (dossier réglementaire
> Twilio : pièce d'identité + adresse en France) — c'est noté au Lot 5, pas pour le
> hackathon.

**Manipulation** — sur le combiné de la personne protégée, dans l'application Téléphone,
composer et appuyer sur appeler :

| But | Code GSM | Ce qu'on attend à l'écran |
|---|---|---|
| **Activer** le renvoi inconditionnel | `**21*<numéro Twilio>#` | « Renvoi d'appel activé » / « Service activé » |
| **Vérifier** l'état du renvoi | `*#21#` | « Renvoi activé vers +1… » avec le numéro Twilio lisible |
| **Annuler** le renvoi | `##21#` | « Renvoi d'appel désactivé » / « Suppression effectuée » |

Le numéro Twilio est à saisir en **E.164 complet, avec le `+`** (`**21*+1XXXXXXXXXX#`).
Sans le `+`, certains opérateurs acceptent le code puis renvoient vers un numéro
inatteignable — le renvoi est « actif » et les appels tombent dans le vide.

**Résultat attendu** — `*#21#` affiche le numéro Twilio. Un appel de test vers le numéro
public ne fait **pas** sonner le combiné et apparaît dans Twilio (Monitor → Logs → Calls).

**Résultat observé**

```
Opérateur / forfait            :
Tarif États-Unis du forfait    :
Code **21* accepté ?           :
*#21# affiche                  :
Le combiné sonne-t-il encore ? :
L'appel apparaît dans Twilio ? :
##21# a bien annulé ?          :
```

**Si c'est mauvais** — selon le symptôme :

- *le code est refusé* : certains opérateurs exigent l'activation du service côté espace
  client, ou utilisent `*21*<numéro>#` (un seul astérisque). Essayer la variante, puis
  appeler le service client.
- *le renvoi s'active mais l'appel n'arrive pas chez Twilio* : l'opérateur bloque les
  renvois vers l'international. **C'est bloquant pour le montage nominal.** Repli démo :
  on fait composer le numéro Twilio directement par l'arnaqueur, et on explique au jury
  que le renvoi est l'étape d'installation qu'on n'a pas pu tester sur ce forfait —
  honnêtement, sans prétendre l'avoir fait.
- *le coût est prohibitif* : on garde le renvoi actif seulement pendant les répétitions
  et le passage devant le jury, `##21#` le reste du temps.

---

## 3. Que vaut `From` sur l'appel renvoyé ? — **LA question du document**

- [ ] **Vérifié**

**Objectif** — savoir quel numéro Twilio nous présente dans `From` quand l'appel nous
arrive *par renvoi* : celui de l'appelant, celui de la personne protégée, ou rien.

**Pourquoi ça peut tout casser** — **toute la liste blanche en dépend**, et avec elle le
reste du projet :

- `From` = numéro de l'appelant → tout fonctionne comme prévu ;
- `From` = numéro de la personne protégée → **la garde anti-boucle
  (`loop_guard_numbers`) refuserait tous les appels**, et on n'aurait de toute façon
  aucun moyen de distinguer le fils de l'arnaqueur : plus de liste blanche, plus de
  `identity_mismatch`, plus de choix de destinataire du code (Lot 2), plus de scénario A
  ni C ;
- `From` vide ou masqué → tous les appels tombent dans la branche « inconnu, analysé »,
  y compris ceux de la famille : la promesse « la famille n'est jamais écoutée » devient
  fausse.

Dans les deux derniers cas : **STOP, on arrête le Lot 1 et on repense le routage** (voir
la décision plus bas). Ne pas contourner en silence.

**Manipulation**

1. Renvoi actif (§2), `make run` et `make tunnel` lancés, webhook Voice collé sur le
   numéro.
2. Depuis le téléphone « arnaqueur », appeler le **numéro public** de la personne
   protégée. Laisser 10 secondes, raccrocher.
3. **Point d'observation A — le journal de `make run`.** Il affiche le `CallSid` et le
   statut, mais pas forcément tous les champs du formulaire. Si besoin, relancer avec
   `LOG_LEVEL=DEBUG make run`.
4. **Point d'observation B — la console Twilio, celui qui fait foi.** Console →
   **Monitor** → **Logs** → **Calls** → cliquer l'appel → section **Request Inspector**
   (ou « Webhook requests »). Elle montre la requête POST exacte que Twilio a envoyée à
   `/twilio/voice/inbound`, **tous les paramètres du formulaire**. Noter au minimum :

   | Champ | Valeur relevée |
   |---|---|
   | `From` | |
   | `To` | |
   | `ForwardedFrom` | |
   | `CallerName` | |
   | `CallSid` | |
   | `Direction` | |
   | `CallerCity` / `FromCountry` | |

5. Comparer : `From` doit valoir le numéro du téléphone arnaqueur, et `ForwardedFrom`
   le numéro public de la personne protégée (c'est la signature d'un appel renvoyé).

> **Note** — un `curl` local ne répond PAS à cette question, il ne fait que simuler ce
> qu'on croit déjà. Il sert seulement à vérifier que la route encaisse la forme du
> formulaire :
> ```bash
> curl -s -X POST localhost:8000/twilio/voice/inbound \
>   -d 'CallSid=CAtest000000000000000000000000000' \
>   -d 'From=+336XXXXXXXX' -d 'To=<numéro Twilio>' \
>   -d 'ForwardedFrom=+336YYYYYYYY'
> ```
> Le vrai résultat, c'est le Request Inspector.

**Résultat attendu** — `From` = **numéro de l'appelant**, `ForwardedFrom` = numéro public
de la personne protégée.

**Résultat observé**

```
Date / heure            :
From                    :
To                      :
ForwardedFrom           :
CallerName              :
CallSid                 :
Verdict : From est bien le numéro de l'APPELANT ?   oui / non / vide
```

**Si c'est mauvais**

- **`From` = numéro de la personne protégée** → STOP. Deux pistes, dans cet ordre :
  (a) utiliser `ForwardedFrom` comme source de vérité pour la liste blanche quand il est
  présent — à confirmer sur ce même relevé ; (b) si aucun champ ne porte l'appelant,
  abandonner le renvoi opérateur et faire composer le numéro Twilio directement. Dans les
  deux cas, **le dire à l'orchestrateur avant d'écrire une ligne de Lot 1**.
- **`From` vide** → vérifier d'abord que l'appelant n'a pas activé le secret d'appel sur
  son combiné (`#31#`). Si c'est bien vide sans secret, le routage ne peut pas distinguer
  la famille : même décision que ci-dessus.
- **`From` présent mais pas en E.164** → noter la forme exacte observée : `normalize_number`
  devra la ramener à celle du carnet, et c'est un test unitaire à ajouter.

---

## 4. SMS aller-retour Twilio US ↔ mobile français

- [ ] **Vérifié**

**Objectif** — un SMS envoyé depuis le numéro Twilio arrive sur un mobile français, et la
réponse arrive sur `/twilio/sms/inbound`.

**Pourquoi ça peut tout casser** — c'est le canal du Lot 2 (le code à 4 chiffres) **et**
du signal le plus décisif du système (le vrai proche qui dit « non »). Son échec est
**muet** : le SMS part côté Twilio, n'arrive jamais, et aucune erreur n'apparaît nulle
part. C'est exactement pour ça que la route
`POST /control/calls/{id}/contact-denied` existe.

**Manipulation**

1. **Rappel** : le champ **A MESSAGE COMES IN** du numéro doit contenir
   `https://<hôte ngrok>/twilio/sms/inbound` en **POST**. Sans ça, le SMS part, la
   réponse arrive chez Twilio et **n'est transmise à personne** — aucun message d'erreur.
2. Aller : Console → Messaging → **Try it out** → **Send an SMS**, ou en ligne de
   commande :
   ```bash
   # ~0,08 USD par SMS vers la France (HYPOTHÈSE, à relever sur la facture)
   uv run python -c "
   from twilio.rest import Client; from vocalguard.config import get_settings
   s = get_settings()
   m = Client(s.twilio_account_sid, s.twilio_auth_token).messages.create(
       to='+336XXXXXXXX', from_=s.twilio_phone_number, body='VocalGuard test terrain')
   print(m.sid, m.status)"
   ```
   **Noter l'heure d'envoi et l'heure de réception sur le combiné.**
3. Retour : répondre `NON` depuis le mobile. Observer, dans le journal de `make run`, la
   ligne du type `Réponse du proche reçue hors appel (« NON », sid=SM…)` — c'est le
   message attendu *hors appel*, et il prouve que la boucle est fermée.
4. Vérifier aussi le chemin de lecture sans dépenser un SMS :
   ```bash
   curl -s -X POST localhost:8000/twilio/sms/inbound \
     -d 'From=<TRUSTED_CONTACT_PHONE_NUMBER>' -d 'Body=NON' -d 'MessageSid=SMtest'
   ```
   Un `From` qui n'est pas exactement `TRUSTED_CONTACT_PHONE_NUMBER` (après
   `normalize_number`) est **ignoré volontairement** : c'est la seule authentification de
   cette route. Si le journal dit « seul le contact de confiance peut répondre », le
   `.env` et le carnet ne concordent pas.

**Résultat attendu** — aller reçu en quelques secondes ; retour visible dans le journal.

**Résultat observé**

```
Aller   — envoyé à        :        reçu à        → délai :
Statut Twilio du message  : queued / sent / delivered / undelivered
Retour  — répondu à       :        vu dans le log à        → délai :
Le log a-t-il lu « NON » comme un démenti ?
```

**Si c'est mauvais**

- *statut `undelivered` ou `failed`* → ouvrir Monitor → Logs → **Errors** et relever le
  code. Les causes connues : geo permissions Messaging (§1), compte en trial et numéro
  non vérifié, ou filtrage opérateur d'un long code américain vers la France (fréquent,
  et hors de notre contrôle).
- *l'aller arrive, le retour n'apparaît pas* → le champ **A MESSAGE COMES IN** est vide
  ou pointe vers un ancien hôte ngrok. Recoller l'URL. Vérifier aussi dans Monitor →
  Logs → Messaging que le message entrant est bien enregistré côté Twilio.
- *rien ne passe du tout* → **ce n'est pas bloquant pour la démo**, mais il faut le
  décider maintenant : le scénario C passe alors par le bouton
  `POST /control/calls/{id}/contact-denied` du dashboard (déterministe, pas de réseau),
  et le code du Lot 2 s'affiche sur l'app au lieu d'être lu par SMS. Le pitch doit le
  dire honnêtement : « le SMS est le canal de production, ici on le simule ».

---

## 5. Les touches clavier (DTMF) remontent-elles ?

- [ ] **Vérifié**

**Objectif** — répondre par **oui** ou **non** à : « quand la personne protégée appuie
sur une touche pendant l'appel, est-ce que nous le voyons ? »

**Pourquoi ça compte** — c'est le **déclencheur n°1** de la vérification d'identité du
Lot 2 (« appuyez sur 1 »), le seul qui marche sans app et sans parler. Le parseur sait
déjà lire des trames `dtmf` (`infrastructure/telephony/media_stream.py`, classe `Dtmf`),
mais `<Start><Stream>` est **unidirectionnel** : rien ne garantit que Twilio y pousse les
événements DTMF. Si la réponse est non, le Lot 2 repose sur les déclencheurs 2 (phrase
« vérification » sur la piste victime) et 3 (bouton de l'app). **Non bloquant, mais à
savoir avant de coder.**

**Manipulation**

1. Monter un appel de test (celui du §3 ou du §7 convient : on cumule).
2. Pendant l'appel, sur le combiné de la personne protégée, **appuyer sur `1`, puis `2`,
   puis `#`**, en espaçant d'une seconde.
3. Observer le journal de `make run`. Les trames `dtmf` sont **parsées puis ignorées** au
   niveau de la session (`application/sessions.py` : « connected / mark / dtmf : rien à
   faire au niveau de la session »), donc il n'y a **pas encore de ligne de log dédiée**.
   Deux façons de voir quelque chose :
   - `LOG_LEVEL=DEBUG make run` et chercher toute mention de `dtmf` dans le flux ;
   - sinon, **demander à l'agent qui tient `media_stream.py` / `sessions.py` un log
     temporaire d'une ligne** sur la branche `dtmf`. C'est une modification de code :
     elle ne relève pas de ce document.
4. Refaire l'essai en appuyant depuis le **téléphone appelant** : les deux pistes ne se
   comportent pas forcément pareil. Noter les deux résultats séparément.

**Résultat attendu** — un **oui / non** franc pour chaque côté. Pas de « peut-être ».

**Résultat observé**

```
Touche pressée côté personne protégée → vue dans le flux ?   oui / non
Touche pressée côté appelant          → vue dans le flux ?   oui / non
Trace exacte relevée (coller la ligne) :
```

**Si c'est non** — ce n'est pas un échec, c'est une réponse. Conséquences à acter :

1. le déclencheur « appuyez sur 1 » **ne sera pas implémenté** (ne pas écrire du code
   mort ni l'annoncer au jury) ;
2. le déclencheur principal du Lot 2 reste `POST /control/calls/{call_id}/verify`,
   appelé par le bouton de l'app et du dashboard — ce qui est de toute façon le seul qui
   fonctionne aussi sur les **appels de confiance**, où aucun flux audio n'est ouvert ;
3. la phrase « vérification » reste un complément, limité aux appels analysés.

Si l'on voulait vraiment le DTMF, il faudrait un `<Gather>` sur la jambe victime, qui
**sort la personne de la conférence** le temps de l'écoute : à ne pas faire pendant un
appel en cours.

---

## 6. L'app s'ouvre sur le téléphone réel et y sonne

- [ ] **Vérifié**

**Objectif** — sur **le téléphone de l'utilisateur** (pas un simulateur, pas le
navigateur du portable), la page `GET /app` s'ouvre, le micro est autorisé, et un appel
de test la fait sonner.

**Pourquoi ça peut tout casser** — c'est le Lot 1 bis et le **critère de fin du projet**
(« il ouvre l'app sur son téléphone, un ami appelle son numéro habituel, l'app sonne »).
C'est aussi ce qui évite la boucle de renvoi : sans l'app, la jambe victime retombe sur
`VICTIM_PHONE_NUMBER`, donc sur une deuxième SIM, donc sur ~0,16 USD la minute.

**Manipulation**

1. `.env` renseigné et `make run` **redémarré** :
   `TWILIO_API_KEY_SID`, `TWILIO_API_KEY_SECRET`, `TWILIO_TWIML_APP_SID`,
   `VOICE_CLIENT_IDENTITY` (ex. `mamie`), `APP_PIN`. Contrôle en une commande :
   ```bash
   uv run python -c "from vocalguard.config import get_settings; \
     print('app_token_configured =', get_settings().app_token_configured)"
   ```
   Ce doit être `True`. `False` = il manque au moins une des cinq valeurs (c'est
   systématiquement l'API Key, et le symptôme côté navigateur est un `401` opaque).
2. Création des objets Twilio, si ce n'est pas fait : Console → **Account** → **API keys
   & tokens** → *Create API key* (type **Standard**) — **le secret ne s'affiche qu'une
   fois**. Puis Console → **Voice** → **TwiML** → **TwiML Apps** → *Create new TwiML App*
   (le SID commence par `AP…`). Sa Voice URL peut rester vide : nous n'utilisons cette
   app que pour les appels **entrants** vers le client, mais le `VoiceGrant` la réclame.
3. Sur le téléphone, ouvrir `https://<hôte ngrok>/app`.
   **ngrok gratuit affiche d'abord une page d'avertissement : appuyer sur « Visit
   Site ».** En HTTPS obligatoirement — en HTTP, le navigateur refuse l'accès au micro
   et le SDK Voice ne se connecte pas.
4. Saisir le PIN (`APP_PIN`). L'app demande `POST /app/token` ; vérifier que l'écran
   passe à « Protection active ».
5. **Autoriser le micro** quand le navigateur le demande. Sur iOS Safari :
   Réglages → Safari → Microphone doit être sur « Demander » ou « Autoriser ».
6. Ajouter à l'écran d'accueil (Safari : Partager → *Sur l'écran d'accueil* ; Chrome :
   menu → *Ajouter à l'écran d'accueil*) et **réouvrir depuis l'icône**. Vérifier que le
   PIN et le micro sont toujours acceptés depuis ce mode.
7. Faire sonner l'app : lancer un appel de test (le téléphone arnaqueur appelle le numéro
   public, ou `uv run python scripts/lancer_appel_demo.py`). L'app doit sonner, afficher
   l'appelant, et laisser décrocher. **Parler** pour vérifier l'audio dans les deux sens.
8. **Tester la limite, elle sera dans le pitch** : mettre l'app en arrière-plan (ou
   verrouiller le téléphone), refaire un appel. Noter ce qui se passe réellement.

À faire sur **iPhone Safari** et/ou **Chrome Android**, selon le téléphone de
l'utilisateur. Noter le modèle et la version de l'OS : le comportement en arrière-plan
en dépend entièrement.

**Résultat attendu** — l'app s'ouvre, le micro est accordé, elle sonne, l'audio passe
dans les deux sens, et le nom ou le numéro de l'appelant s'affiche. App en arrière-plan :
on s'attend à ce qu'elle **ne sonne pas** (iOS suspend les pages), et le repli
`VICTIM_PHONE_NUMBER` doit alors prendre le relais.

**Résultat observé**

```
Téléphone / OS / navigateur       :
app_token_configured              :
Page /app servie (après Visit Site) ?
PIN accepté ?
Micro autorisé ?
Ajout à l'écran d'accueil OK ?
L'app sonne (au premier plan) ?
Nom/numéro affiché à l'écran      :
Audio dans les deux sens ?
En arrière-plan / écran verrouillé : sonne ? repli déclenché ?
```

**Si c'est mauvais**

- *page blanche ou SDK absent* → le CDN `@twilio/voice-sdk` est bloqué par le réseau du
  lieu. Repli : servir le SDK localement depuis `interface/static/`, ou passer sur un
  hotspot dédié (déjà prévu dans le README pour Bedrock).
- *`401` sur `/app/token`* → `APP_PIN` vide (la route refuse tout par construction), ou
  l'API Key absente / mal recopiée.
- *l'app ne sonne pas alors qu'elle est ouverte* → vérifier que `dial_victim` a bien
  visé `client:<identité>` : chercher dans le journal `Jambe victime créée`, et comparer
  l'identité à `VOICE_CLIENT_IDENTITY`. Vérifier que le `VoiceGrant` porte
  `incoming_allow=True`.
- *ça ne marche pas du tout* → laisser `VOICE_CLIENT_IDENTITY` vide : le code retombe sur
  `VICTIM_PHONE_NUMBER` (comportement d'avant le Lot 1 bis), la démo tient, on perd
  l'affichage de l'appelant et le bouton de vérification côté personne protégée (il reste
  sur le dashboard).

---

## 7. L'annonce n'est entendue que par la personne protégée

- [ ] **Vérifié** — *(case correspondante du README : encore décochée)*

**Objectif** — vérifier **pour de vrai**, avec un coéquipier à l'autre bout, que la
phrase d'alerte jouée par `announce_url` n'est entendue **que** par la personne protégée.

**Pourquoi ça peut tout casser** — si l'arnaqueur entend « Attention, cet appel présente
des signes d'arnaque », il raccroche, comprend le dispositif, et la démo se retourne
contre nous devant le jury. C'est le premier risque listé dans le README, et il n'a
**jamais été vérifié**.

**Manipulation**

1. Deux personnes, deux téléphones, chacun avec une consigne écrite :
   - *côté personne protégée* : noter mot pour mot ce qui est entendu et à quelle
     seconde ;
   - *côté appelant* : **noter uniquement « entendu » ou « rien »**. C'est ce coéquipier
     qui tranche. Lui demander de ne pas parler pendant les 10 secondes qui suivent le
     déclenchement, pour ne pas couvrir une annonce faible.
2. Monter l'appel, décrocher des deux côtés, attendre que la conférence soit établie
   (chercher `join` dans le journal — `/twilio/conference/status` les journalise).
3. Déclencher une annonce **sans attendre que le score monte**, pour maîtriser l'instant.
   Le plus simple : le niveau *alerte* passe par `announce_to_victim`, et on peut le
   provoquer en jouant le dialogue du scénario `scam_02` jusqu'à « donnez-moi les seize
   chiffres ». Si le score est capricieux, le coéquipier au clavier peut déclencher le
   chemin par le dashboard (bouton « le proche dément », `POST /control/calls/{id}/contact-denied`).
4. **Tester les deux niveaux séparément**, ils ne se ressemblent pas :
   - **alerte (65–84)** : annonce **sans** mise en attente. C'est le cas risqué —
     l'appelant est toujours en ligne quand la phrase est jouée.
   - **intervention (≥ 85)** : `plan_actions` met l'appelant **en attente d'abord**, puis
     annonce. Même si l'annonce fuyait, il n'entendrait que la musique d'attente. Ce
     niveau-là est protégé par construction.
5. Noter aussi ce que l'appelant entend **au moment de la mise en attente** : il doit
   entendre de la musique (`/twilio/voice/hold-music`) et croire à un aléa réseau, pas
   un silence ni un bip. Le TwiML de conférence pose déjà `beep=False` et `waitUrl=""` :
   c'est précisément ce qu'on vérifie ici.

**Résultat attendu** — niveau alerte : la personne protégée entend la phrase, l'appelant
n'entend **rien**. Niveau intervention : l'appelant entend la musique d'attente.

**Résultat observé**

```
Niveau alerte (65–84)
  Personne protégée a entendu   :
  Appelant a entendu            : rien / « … »
Niveau intervention (≥ 85)
  Ordre observé (attente puis annonce ?) :
  Appelant a entendu            : musique / silence / l'annonce
Confirmé par (nom du coéquipier côté appelant) :
```

**Si c'est mauvais** — l'annonce fuit vers l'appelant. Dans l'ordre :

1. **Mettre en attente avant chaque annonce**, y compris au niveau *alerte* — c'est le
   repli déjà écrit dans le README, et il est à portée de main : le niveau *intervention*
   le fait déjà (`plan_actions` : `HOLD_CALLER` puis `ANNOUNCE_VICTIM`). Coût : l'appelant
   entend quelques secondes de musique, ce qui est beaucoup moins grave que de l'alerter.
2. **Agent Conference** (`whisper` / `coach`) : Console → Voice → Settings, activer
   l'Agent Conference, puis `TWILIO_AGENT_CONFERENCE_ENABLED=true`. Sans activation, Twilio
   répond **erreur 16000** — c'est pour ça qu'on a choisi `announce_url` d'abord.
3. En dernier recours, **supprimer l'annonce audible du niveau alerte** et ne garder que
   le dashboard et le SMS au proche jusqu'à l'intervention.

---

## 8. Faire les quatre d'un coup (si le temps ou le budget serre)

Un seul appel de test peut servir aux §3, §5, §6 et §7 — à condition de **préparer tous
les points d'observation avant de décrocher**, parce qu'on ne refera pas l'appel pour
relire un champ.

Avant de composer :

- [ ] `make run` lancé, `make tunnel` lancé, `PUBLIC_HOSTNAME` à jour, `make run` **redémarré** après
- [ ] les deux webhooks (Voice, Messaging) recollés sur le numéro avec l'hôte ngrok courant
- [ ] renvoi actif, vérifié par `*#21#`
- [ ] `curl -s localhost:8000/healthz` → `twilio_configured: true`, `public_hostname` non nul
- [ ] app ouverte sur le téléphone de l'utilisateur, écran « Protection active », micro accordé
- [ ] console Twilio ouverte sur Monitor → Logs → Calls, prête à rafraîchir
- [ ] un coéquipier côté appelant, avec sa consigne écrite (§7)
- [ ] ce document ouvert, pour remplir les blocs « Résultat observé » **pendant** l'appel

Déroulé : appeler → relever `From` au Request Inspector (§3) → décrocher dans l'app (§6)
→ appuyer sur `1` et `2` (§5) → jouer le dialogue jusqu'au déclenchement de l'annonce
(§7) → raccrocher. Puis remplir les quatre sections, **tant que c'est frais**.

## 9. Coûts constatés

À remplir après chaque session de test, depuis Console → **Monitor** → **Usage**. C'est
ce tableau qui alimentera les coûts unitaires du Lot 5 — il vaut plus qu'une estimation.

| Date | Appels | Minutes | SMS | Lookups | Solde Twilio avant → après | Facture opérateur (renvoi) |
|---|---|---|---|---|---|---|
| | | | | | | |
| | | | | | | |

Repères connus : **~0,16 USD/min par jambe vers un mobile français** (relevé sur le
compte, cf. `scripts/lancer_appel_demo.py`). Les autres tarifs (minute entrante vers un
numéro US, minute de client WebRTC, SMS vers la France, requête Lookup) sont des
`HYPOTHÈSE` à relever ici plutôt qu'à deviner.
