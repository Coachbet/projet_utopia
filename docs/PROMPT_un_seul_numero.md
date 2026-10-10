# Prompt — VocalGuard : un seul numéro, liste blanche, vérification par code

> À coller tel quel dans l'IA de code qui travaille sur ce dépôt (`projet_utopia`).
> Rédigé le 10/10/2026 après relecture du code existant.

---

## 0. Qui tu es, où tu es, ce que tu ne casses pas

Tu travailles dans le dépôt `projet_utopia` (paquet `src/vocalguard/`, architecture hexagonale :
`domain/` → `application/` → `infrastructure/` → `interface/`). Lis d'abord `README.md`,
`src/vocalguard/interface/http/webhooks.py`, `infrastructure/telephony/twiml.py`,
`infrastructure/telephony/client.py`, `application/intervention.py`, `infrastructure/contacts/book.py`,
`config.py`, `scripts/lancer_appel_demo.py`.

Règles non négociables :
1. **Tous les tests existants restent verts** (`make test`). Chaque lot ajoute ses tests, écrits avant le code quand c'est possible.
2. **Le contrat d'événement est figé** (`contracts/event.schema.json`) : n'ajoute AUCUN `type`. Les nouveautés passent par des événements `action` (champ `data.action`) ou `reputation`.
3. **Jamais de raccrochage automatique**, jamais d'audio ni de transcript persistés (`PERSIST_*=false`).
4. **Aucun secret** dans le code, les logs, les commits ou tes messages. Le `.env` ne se commite pas.
5. Une branche par lot (`feat/lot-1-routage`, …), des commits petits et lisibles, un message de fin de lot qui dit : ce qui marche, ce qui a été testé pour de vrai au téléphone, ce qui reste.
6. Tout ce qui coûte de l'argent (appel réel, SMS, Lookup) : tu demandes à l'utilisateur avant, et tu dis combien à peu près.
7. Si une hypothèse ci-dessous se révèle fausse au test réel, **tu t'arrêtes et tu le dis** plutôt que de la contourner en silence.

---

## 1. Le produit qu'on construit (la cible)

La personne protégée (« la mamie ») garde **son vrai numéro**. Elle active **une fois** un renvoi d'appel
vers notre numéro Twilio. Tout appel entrant passe alors par notre serveur, qui décide :

- **appelant de confiance** (famille, médecin… dans le carnet) → **pas d'annonce, pas d'analyse**, l'appel est simplement transmis ;
- **appelant inconnu / masqué / étranger** → **annonce à l'appelant seul** (« cet appel est analysé par un service de protection contre la fraude »), puis analyse en direct (le pipeline actuel : transcription, 4 signaux, score, alertes, mise en attente, SMS au proche) ;
- **à tout moment**, la mamie peut demander une **vérification d'identité par code à 4 chiffres** : le code part par SMS au **vrai numéro** du proche que l'appelant prétend être. Un usurpateur de numéro ne peut pas le connaître.

### Les numéros et l'app (à ne jamais confondre)

| Nom | Rôle | Variable |
|---|---|---|
| Numéro Twilio | Relais technique, invisible pour tous : `+16202648948` | `TWILIO_PHONE_NUMBER` (déjà rempli) |
| Numéro public de l'utilisateur protégé | Son vrai mobile, **renvoyé** vers Twilio (`**21*+16202648948#`) | `PROTECTED_PHONE_NUMBER` (**nouveau**) |
| **App VocalGuard sur son téléphone** | Page web installable (PWA) avec le **SDK Voice JavaScript de Twilio** : c'est elle qui sonne, affiche l'appelant, porte les boutons | identité client `VOICE_CLIENT_IDENTITY` (**nouveau**, ex. `mamie`) |
| Ligne de secours (optionnel) | 2ᵉ SIM non renvoyée, si l'app n'est pas ouverte | `VICTIM_PHONE_NUMBER` (existe, devient un repli) |
| Téléphone « arnaqueur » / ami | Appelle le numéro public | `SCAMMER_PHONE_NUMBER` |
| Vrai proche (Lucas) | Reçoit le SMS / le code | `TRUSTED_CONTACT_PHONE_NUMBER` + `data/contacts.json` |

**Pourquoi une app et pas un rappel sur le numéro** : si Twilio rappelait le numéro public, l'opérateur le renverrait… vers Twilio. **Boucle.** En faisant sonner l'app (jambe `client:mamie` au lieu d'un numéro), il n'y a plus de boucle, plus besoin de double SIM, la minute coûte beaucoup moins cher qu'un appel vers un mobile français, et l'app peut afficher **le vrai numéro de l'appelant** et un **bouton « Vérifier son identité »** (déclencheur du code, Lot 2). C'est la version web du modèle CallKit ; l'app native iOS/Android (CallKit + push VoIP) reste la roadmap.

**Cas d'usage réel à faire marcher** : l'utilisateur ouvre l'app sur SON téléphone, un ami appelle son numéro habituel → l'opérateur renvoie vers Twilio → notre serveur décide (confiance / analyse) → **l'app sonne sur son téléphone** → il décroche dans l'app et parle à son ami.

```
Ami / arnaqueur ──compose──▶ n° public (vrai mobile de l'utilisateur)
                          │ renvoi opérateur (**21*)
                          ▼
                   Twilio +16202648948 ──webhook──▶ /twilio/voice/inbound
                          │                               │ décide : confiance / analyse
                          │◀── TwiML ─────────────────────┘
                          ├─ [inconnu] <Say> annonce à l'appelant ▶ <Start><Stream> ▶ <Conference>
                          └─ [confiance] <Conference> sans Stream
                          │
                          └──calls.create(to="client:mamie")──▶ APP sur le téléphone (SDK Voice JS)
                                 écran : « Appel de Lucas » / « 06… inconnu — appel protégé »
                                 boutons : Décrocher · Refuser · Vérifier son identité · Mettre en attente
```

---

## 2. Lot 0 — Vérifications terrain AVANT de coder (avec l'utilisateur)

Guide l'utilisateur pas à pas, une étape à la fois, et note chaque résultat dans `docs/terrain.md`.

1. **Permissions géographiques Twilio** : Voice et Messaging autorisés vers la **France** (console → Voice → Settings → Geo permissions ; Messaging → Settings → Geo permissions). Sans ça, les SMS et le repli téléphonique vers un mobile français échouent sans message clair.
2. **Renvoi d'appel** sur le numéro public : `**21*+16202648948#` (annulation : `##21#`, vérification : `*#21#`).
   ⚠️ **Coût** : le renvoi est facturé par l'opérateur du numéro renvoyé (donc à l'utilisateur protégé, pas à l'appelant) comme un appel vers les États-Unis, souvent **hors forfait**. Demander à l'utilisateur de vérifier son forfait, et faire des appels de test courts.
3. **Appel test** arnaqueur → numéro public. Loguer **tous** les champs du webhook entrant (au moins `From`, `To`, `ForwardedFrom`, `CallerName`, `CallSid`). Question à trancher : **le `From` est-il bien le numéro de l'arnaqueur**, ou celui de la mamie, ou vide ? Toute la liste blanche en dépend. Si ce n'est pas le bon numéro → STOP, le dire.
4. **SMS aller-retour** Twilio US ↔ mobile français (envoi + réponse qui arrive sur `/twilio/sms/inbound`). Noter le délai.
5. **Touches clavier (DTMF)** : vérifier si Twilio remonte une touche pressée par la mamie pendant l'appel (le parseur `media_stream.py` sait déjà lire des trames `dtmf`, mais avec `<Start><Stream>` unidirectionnel elles n'arrivent peut-être pas). Résultat attendu : « oui / non », il décide du déclencheur du Lot 2.
6. **L'app s'ouvre sur le téléphone** via l'URL ngrok en HTTPS (ngrok gratuit affiche une page d'avertissement : cliquer « Visit Site »), le micro est autorisé, l'app reçoit un appel de test `client:mamie`. Tester sur **le téléphone réel de l'utilisateur** (iPhone Safari et/ou Chrome Android).
7. **Annonce entendue par la victime seule** : la case du README est encore décochée. La vérifier pour de vrai (un coéquipier côté arnaqueur confirme n'avoir rien entendu).

---

## 3. Lot 1 — Routage : liste blanche vs analyse

### Carnet d'adresses
- Ajouter à chaque contact un champ optionnel `"mode": "trusted" | "analyze"`. **Défaut : `trusted` pour tout contact présent dans le carnet** (rétrocompatible avec le JSON actuel).
- `Contact` (domaine) gagne ce champ ; `JsonContactBook` le lit ; tests de chargement avec et sans le champ.

### `/twilio/voice/inbound`
1. Normaliser `From` (`normalize_number`). Considérer comme **inconnu** : absent du carnet, vide, `anonymous`/`restricted`/`unknown`, ou contact en mode `analyze`.
2. **Garde anti-boucle** : si `From` == `PROTECTED_PHONE_NUMBER` ou `VICTIM_PHONE_NUMBER` ou `TWILIO_PHONE_NUMBER` (hors mode démo, voir Lot 4) → ne pas rappeler la victime, répondre un TwiML neutre et loguer fort.
3. **Contact de confiance** : TwiML = `<Dial><Conference>` **sans `<Start><Stream>`** et sans annonce. On rappelle quand même la victime. Aucune transcription ouverte (ne pas appeler `registry.open` avec transcription, ou l'ouvrir dans un mode « sans audio » : à toi de choisir le plus propre, mais **aucun octet d'audio ne doit atteindre le serveur**). Émettre un événement `action` `{"action": "trusted_passthrough", "contact": "Lucas Martin"}` pour le dashboard.
4. **Inconnu** : TwiML = `<Say language="fr-FR">` avec le texte `CALLER_NOTICE_TEXT` (nouveau réglage, défaut : « Cet appel est analysé par un service de protection contre la fraude. »), **puis** `<Start><Stream>`, **puis** la conférence. L'annonce doit être jouée avant tout flux audio : on n'analyse que ce qui suit l'information.
5. Tests unitaires du TwiML (ordre Say → Stream → Conference ; pas de Stream en mode confiance) et tests d'intégration de la route pour les 4 cas (confiance, inconnu, masqué, boucle).

### Jambe victime (`/twilio/voice/join/{call_id}`)
- Avec l'app, l'identité de l'appelant s'affiche à l'écran (Lot 1 bis). En repli téléphonique (`VICTIM_PHONE_NUMBER`), avant `<Conference>`, un `<Say>` court entendu **par la mamie seule** : « Appel de Lucas » (contact connu) ou « Appel d'un numéro inconnu, appel protégé » (inconnu). C'est notre réponse au problème d'affichage : son téléphone montrera le numéro Twilio, pas celui de l'appelant.
- **Expérience optionnelle, une seule tentative** : passer le numéro de l'appelant comme `from_` de la jambe victime. Si Twilio refuse (numéro ni possédé ni vérifié) ou si l'opérateur français masque l'appel, revenir au numéro Twilio et noter le résultat dans `docs/terrain.md`. Ne pas insister.

## 3 bis. Lot 1 bis — L'app sur le téléphone (prioritaire, à faire avec le Lot 1)

Objectif : l'utilisateur teste depuis **son propre téléphone**, sans ordinateur dans la main.

1. **Côté Twilio** (guider l'utilisateur dans la console, ou le faire par l'API après accord) : créer une **API Key** (SID + secret) et une **TwiML App**. Nouveaux réglages `.env` : `TWILIO_API_KEY_SID`, `TWILIO_API_KEY_SECRET`, `TWILIO_TWIML_APP_SID`, `VOICE_CLIENT_IDENTITY`. Jamais commités.
2. **Serveur** :
   - `GET /app` : sert la page (HTML/JS statique dans `interface/static/`, comme le dashboard), avec `manifest.webmanifest` + icône pour « Ajouter à l'écran d'accueil » ;
   - `POST /app/token` : renvoie un **Access Token Voice** (`twilio.jwt.access_token` + `VoiceGrant(incoming_allow=True)`) pour `VOICE_CLIENT_IDENTITY`, durée courte. Protéger cette route par un code PIN simple (`APP_PIN` dans `.env`) : l'URL ngrok est publique ;
   - `dial_victim` : appeler `to=f"client:{identity}"` quand `VOICE_CLIENT_IDENTITY` est rempli, sinon repli sur `VICTIM_PHONE_NUMBER` (comportement actuel). Passer le numéro et le nom de l'appelant à l'app via des paramètres d'appel personnalisés (`<Parameter>` / paramètres de `calls.create` pris en charge par le SDK — vérifier dans la doc Twilio actuelle la bonne méthode et la tester).
3. **Page de l'app** (mobile d'abord, gros boutons, lisible par une personne âgée, français) :
   - charger le SDK `@twilio/voice-sdk` (version exacte épinglée, depuis un CDN autorisé ou servi localement) ;
   - écran d'attente « Protection active » ; écran d'appel entrant : nom du contact ou numéro + badge « Contact de confiance » / « Inconnu — appel analysé » ; sonnerie ;
   - en appel : **Vérifier son identité** (→ `POST /control/calls/{call_id}/verify`, Lot 2), **Mettre en attente / Reprendre** (routes `control.py` existantes), affichage du niveau de risque en direct (s'abonner au WebSocket du dashboard existant, filtré sur l'appel) et des alertes en grand ;
   - le `call_id` côté app = le `CallSid` de la jambe d'origine, transmis en paramètre personnalisé.
4. **Limites à écrire noir sur blanc dans le README** : une page web ne sonne **que si elle est ouverte** (iOS suspend les pages en arrière-plan, pas de push VoIP sans app native). Si l'app est fermée → repli sur `VICTIM_PHONE_NUMBER` si rempli, sinon messagerie Twilio (`<Say>` « la personne n'est pas disponible »). L'app native (CallKit iOS / ConnectionService Android + push VoIP, compte Apple Developer payant) est la roadmap.
5. Tests : génération du token (identité, grant, pas de secret dans la réponse hors JWT), `dial_victim` choisit `client:` ou le numéro, route `/app` servie, PIN requis.

---

## 4. Lot 2 — Vérification d'identité par code à 4 chiffres

### Déclencheurs (dans cet ordre de préférence, selon le résultat du Lot 0 §5)
1. La mamie appuie sur **1** (si le DTMF remonte) ;
2. La mamie **dit** « vérification » / « vérifie » / « code » (détecté sur la **piste victime** du transcript — appels analysés uniquement) ;
3. **Bouton « Vérifier son identité » de l'app** (et du dashboard) : nouvelle route `POST /control/calls/{call_id}/verify` (calquée sur les routes existantes de `control.py`). **C'est le déclencheur principal** : il marche aussi pour les appels de confiance (pas de flux audio). Le DTMF et la phrase « vérification » ne sont que des compléments.

### À qui part le code
- Si le numéro appelant est dans le carnet → au **numéro du carnet** de ce contact (c'est tout l'intérêt : en cas d'usurpation, le SMS arrive sur le VRAI téléphone du proche).
- Sinon, si Claude a extrait une identité revendiquée (`claimed_identity`) qui correspond à un contact (`by_identity`) → au numéro de ce contact.
- Sinon → annonce à la mamie : « Je ne peux pas vérifier : cette personne n'est pas dans vos contacts. » et rien d'autre.
- **Jamais** au numéro appelant. **Jamais** prononcé dans la conférence.

### Déroulé
1. Générer le code avec `secrets.randbelow(10000)` formaté sur 4 chiffres ; un seul code actif par appel ; expiration 120 s ; état stocké par appel et **purgé dans `lifecycle.release_call`**.
2. SMS au proche : « VocalGuard : code {code}. Si c'est bien toi qui appelles {owner} en ce moment, lis-lui ce code. Sinon réponds NON. » (`owner` = champ `owner` du carnet). La réponse « NON » réutilise le chemin existant `/twilio/sms/inbound` → `contact_replied(denied=True)`.
3. Annonce à la mamie seule (`announce_to_victim`) : « Un code a été envoyé à Lucas. Le code est 4… 8… 2… 1. Demandez-lui de vous le lire. » (chiffres séparés pour la synthèse vocale). Avec l'app, afficher **aussi** le code en grand à l'écran, avec un compte à rebours de 120 s.
4. **Appels analysés seulement** : écouter la piste appelant pendant 120 s ; normaliser les nombres français dits à voix haute (« quatre huit deux un », « quarante-huit vingt et un », « 4821 ») et comparer.
   - Correspond → annonce « Code correct. » + événement `action` `{"action": "identity_verified", "ok": true}` + baisse du risque (passe par le signal `reputation`, sans toucher au contrat).
   - Faux, ou expiré sans réponse, ou le proche répond NON → même effet qu'un démenti du proche (`contact_denied`) → l'escalade existante fait le reste.
5. Tests : génération/expiration, choix du destinataire (3 cas), jamais de SMS au numéro appelant, normalisation des nombres FR (table de cas), idempotence (deux déclenchements = un seul code), purge en fin d'appel.

---

## 5. Lot 3 — Mode « analyse de la voix de la mamie seule » (optionnel, si le temps le permet)

Pour les appels de confiance, option `TRUSTED_VICTIM_ONLY_ANALYSIS=false` par défaut. Si vrai : `<Start><Stream track="outbound_track">` (piste mamie uniquement) et détection de phrases à risque **dites par elle** (« mon code de carte », « je te fais un virement », « cartes cadeaux »…) → annonce douce : « Avant tout paiement, demandez une vérification. » Justification à écrire dans le README : on ne traite que la voix de l'abonnée qui a consenti. Le marquer « à valider juridiquement ».

---

## 6. Lot 4 — Démo

1. **Simulation d'usurpation** (Twilio interdit de vraiment usurper) : réglage démo `DEMO_SPOOF_AS` = un numéro du carnet (ex. celui de Lucas). Quand un appel vient de `SCAMMER_PHONE_NUMBER` et que ce réglage est rempli, le serveur **fait comme si** `From` valait ce numéro. Log en MAJUSCULES à chaque usage, valeur vide par défaut, impossible à activer sans `SCAMMER_PHONE_NUMBER`.
2. **`scripts/lancer_appel_demo.py`** doit continuer de marcher (dans ce mode, `From` = notre numéro Twilio : la garde anti-boucle ne doit pas le bloquer — passer l'identité présentée par un paramètre d'URL ou un réglage démo, et le tester).
3. **Trois scénarios scriptés** dans `docs/script_demo.md`, minutés (≤ 90 s chacun) :
   - A. Le fils appelle → passe directement, l'app affiche « Appel de Thomas · Contact de confiance », aucune annonce côté appelant, dashboard : « appel de confiance, non analysé ».
   - B. Inconnu « faux conseiller bancaire » → annonce à l'appelant, analyse, score qui monte, alerte chuchotée, mise en attente.
   - C. « Lucas » avec numéro usurpé (`DEMO_SPOOF_AS`) + voix clonée → passe la liste blanche → la mamie demande la vérification → le code part sur le VRAI téléphone de Lucas → l'arnaqueur ne peut pas le lire → alerte. **C'est le moment fort du pitch.**
4. Mettre à jour le README (architecture un seul numéro, nouveaux réglages dans `.env.example` **sans valeurs**), et `docs/index.html` si elle décrit l'état du projet.

---

## 7. Lot 5 — Business plan (`docs/business_plan.md`, pour Biz 2)

Règle absolue : **aucun chiffre inventé**. Chaque chiffre a une source (lien) ou est marqué `HYPOTHÈSE` / `À SOURCER`.

Plan attendu :
1. **Problème** — arnaques téléphoniques et au faux proche / faux conseiller en France, voix clonées par IA ; chiffres officiels à sourcer (rapports publics, plateformes de signalement, Cybermalveillance.gouv.fr, banques).
2. **Persona** — personne âgée (75 ans +) vivant seule + son enfant aidant (35–55 ans), qui paie et configure.
3. **Solution** — un seul numéro, liste blanche famille, annonce aux inconnus, analyse temps réel, vérification par code envoyée au vrai proche, alertes chuchotées, mise en attente, pas de raccrochage auto.
4. **Différenciation** — vérification par code contre l'usurpation de numéro (aucun concurrent identifié ne le fait : à vérifier) ; la famille n'est jamais écoutée ; fonctionne sur n'importe quel téléphone (renvoi d'appel) ; comparaison honnête avec la détection sur l'appareil de Google (Pixel), Samsung et des opérateurs coréens (SK Telecom, KT, LG U+).
5. **Cadre légal** — secret des correspondances (art. 226-15 Code pénal, L32-3 CPCE), directive ePrivacy art. 5, RGPD (base légale, minimisation, AIPD), information de l'appelant par annonce, aucun stockage audio, liste blanche ; l'opérateur ne voit jamais le contenu. Points ouverts à soumettre à un juriste / à la CNIL. Ne rien affirmer comme certain.
6. **Modèle économique** — B2C abonnement « famille » payé par l'aidant ; B2B2C via banques, assureurs, mutuelles, caisses de retraite (qui ont intérêt à réduire la fraude) ; opérateurs comme **canal de distribution** (activation du renvoi, numéro), pas comme analyseurs. Prix = `HYPOTHÈSE` à justifier.
7. **Coûts unitaires** — tableau par utilisateur et par mois à partir des tarifs publics Twilio (minute entrante, minute sortante vers mobile FR — le script de démo note ~0,16 USD/min —, SMS vers la France, numéro), Amazon Transcribe, Bedrock, avec hypothèses explicites (nb d'appels inconnus/mois, durée). Montrer la marge selon 2–3 hypothèses de prix.
8. **Roadmap** — numéro français (dossier réglementaire), app native CallKit / Android (sonne même app fermée grâce au push VoIP), analyse sur l'appareil quand c'est possible, partenariats bancaires.
9. **Risques** — faux positifs, délivrabilité SMS, coût du renvoi international (→ numéro FR), adoption par les seniors, dépendance Twilio/AWS.
10. **KPIs** et **demande** (ce qu'on attend du jury / des partenaires).

---

## 8. Ordre de travail et définition de « fini »

Ordre : Lot 0 → Lot 1 + Lot 1 bis (l'app) → Lot 2 → Lot 4 → Lot 5 → (Lot 3 si temps). Le Lot 5 peut être rédigé en parallèle par un autre membre.

C'est fini quand :
- `make test` et `make lint` passent ;
- les trois scénarios A, B, C ont été joués **au vrai téléphone** au moins une fois chacun, résultats notés dans `docs/terrain.md` ;
- **le test réel de l'utilisateur marche** : il ouvre l'app sur son téléphone, un ami appelle son numéro habituel, l'app sonne, il décroche et entend son ami ;
- le README et `.env.example` décrivent les nouveaux réglages (`PROTECTED_PHONE_NUMBER`, `CALLER_NOTICE_TEXT`, `DEMO_SPOOF_AS`, `TRUSTED_VICTIM_ONLY_ANALYSIS`, `VOICE_CLIENT_IDENTITY`, `TWILIO_API_KEY_SID`, `TWILIO_API_KEY_SECRET`, `TWILIO_TWIML_APP_SID`, `APP_PIN`) ;
- tu as livré un récapitulatif : ce qui marche, ce qui a été vérifié pour de vrai, coûts constatés sur le compte Twilio, limites connues.
