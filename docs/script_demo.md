# Script de démo — trois scénarios minutés

> Trois passages de **90 secondes maximum** chacun, à jouer debout devant le jury.
> Format : une colonne temps, qui fait quoi, ce qui s'affiche, ce qui s'entend.
>
> Les dialogues ne sont **pas inventés** : ils reprennent les scénarios déjà écrits et
> déjà étalonnés dans `scenarios/*.json`, ceux que `make replay` rejoue. On les joue à
> la voix, mais le texte et l'ordre des répliques sont ceux des fichiers — c'est ce qui
> garantit que le score se comporte comme en répétition.
>
> Les noms viennent de `data/contacts.example.json`, à ne pas changer en route :
>
> | Rôle | Nom exact | Numéro au carnet |
> |---|---|---|
> | Personne protégée | **Jeanne Moreau** (champ `owner`) | *(son numéro public, `PROTECTED_PHONE_NUMBER`)* |
> | Fils | **Thomas Moreau** | `+33678901234` → à remplacer par le vrai téléphone du coéquipier |
> | Petit-fils | **Lucas Martin** | `+33612345678` → à remplacer par le vrai téléphone du coéquipier |
> | Fille | **Camille Martin** | |
> | Médecin | **Docteur Laurent** | |
> | Banque | **Banque Crédit Mutuel** | |

## Distribution — quatre rôles, trois téléphones

| Rôle | Qui tient quoi | Ce qu'il fait |
|---|---|---|
| **Narrateur** | rien, il parle au jury | tient le minutage, dit les phrases clés, ne touche à aucun clavier |
| **Jeanne** | le **téléphone protégé**, app VocalGuard ouverte sur `/app` | décroche, appuie sur « Vérifier son identité » |
| **L'appelant** | le **téléphone « arnaqueur »** (`SCAMMER_PHONE_NUMBER`), *et* celui de Thomas au scénario A | compose, joue le dialogue |
| **Le vrai Lucas** | son **vrai téléphone** (`TRUSTED_CONTACT_PHONE_NUMBER`), **visible du jury mais à l'écart** | ne fait rien… jusqu'au scénario C, où il lève son téléphone |
| *(écran)* | portable avec `/dashboard` au projecteur | personne ne clique dessus sauf plan B |

Le rôle du **vrai Lucas** est le plus important du spectacle et il n'a qu'un geste à
faire. Le placer **à l'écart de l'appelant**, pour que le jury voie bien que ce sont deux
téléphones différents.

## `.env` commun aux trois scénarios

```ini
PUBLIC_HOSTNAME=<hôte ngrok courant>      # recollé dans la console Twilio juste avant
TWILIO_PHONE_NUMBER=<numéro Twilio>
PROTECTED_PHONE_NUMBER=+336XXXXXXXX        # le vrai numéro de Jeanne, renvoyé vers Twilio
VOICE_CLIENT_IDENTITY=mamie                # l'app sonne ; vide = repli téléphonique
APP_PIN=<code à 4+ chiffres>
VICTIM_PHONE_NUMBER=+336ZZZZZZZZ           # repli si l'app ne sonne pas (plan B)
TRUSTED_CONTACT_PHONE_NUMBER=+336LLLLLLLL  # le VRAI téléphone de Lucas
SCAMMER_PHONE_NUMBER=+336AAAAAAAA          # le téléphone de l'appelant de démo
TWILIO_LOOKUP_ENABLED=true                 # réglage de démo, facturé à la requête
CONTACTS_PATH=data/contacts.json
```

Et dans `data/contacts.json` (copié de `contacts.example.json`, **jamais commité avec de
vrais numéros**) : le numéro de **Lucas Martin** doit être **exactement**
`TRUSTED_CONTACT_PHONE_NUMBER`, en E.164 strict. Si les deux diffèrent, le SMS du
scénario C part à un numéro dont le dashboard annonce le nom de quelqu'un d'autre — et
c'est le scénario C qui s'écroule.

`DEMO_SPOOF_AS` **reste vide** pour A et B, et n'est rempli que pour C.

## T-10 minutes — la liste qu'on coche en silence

- [ ] `make run` (terminal 1) et `make tunnel` (terminal 2) lancés ; hôte ngrok noté
- [ ] `PUBLIC_HOSTNAME` à jour **et `make run` redémarré** après modification du `.env`
- [ ] les deux webhooks recollés sur le numéro : `A CALL COMES IN` → `/twilio/voice/inbound`,
      `A MESSAGE COMES IN` → `/twilio/sms/inbound`, **POST** tous les deux
- [ ] `curl -s localhost:8000/healthz` → `twilio_configured: true`, `public_hostname` non nul,
      `active_calls: []`
- [ ] identifiants AWS rafraîchis dans le shell de `make run`, puis `make check-aws` vert
      *(les creds du workshop expirent — c'est la panne la plus bête possible)*
- [ ] renvoi d'appel actif sur le téléphone de Jeanne, vérifié par `*#21#`
- [ ] app ouverte sur `/app`, PIN saisi, écran « Protection active », **micro accordé**,
      téléphone en **ne pas déranger** mais **écran allumé** (une page web ne sonne que si
      elle est au premier plan)
- [ ] `/dashboard` ouvert au projecteur, zoom à 150 %, une seule ligne d'appel visible
- [ ] volume du téléphone de Jeanne à fond + **haut-parleur**, sinon le jury n'entend rien
- [ ] le vrai Lucas est en place, son téléphone déverrouillé, sonnerie **forte**
- [ ] un appel blanc fait et raccroché, pour confirmer que la chaîne entière vit
- [ ] vidéo de secours (§Rejeu) ouverte dans un onglet, prête à jouer

---

# Scénario A — « Le fils appelle » : la famille n'est jamais écoutée

**Durée : 50 s.** Le plus court et le plus important à placer **en premier** : il désarme
l'objection « vous écoutez les appels de ma mère » avant qu'elle soit posée.

### Pourquoi Thomas et pas Lucas

Le scénario C usurpe l'identité de **Lucas**. Si A et C mettaient tous deux Lucas en
scène, le jury ne saurait plus lequel est le vrai. On prend donc **Thomas Moreau** (le
fils, déjà au carnet) pour A, et on garde Lucas pour C.

### Prérequis spécifiques

| Élément | Valeur |
|---|---|
| `data/contacts.json` | `Thomas Moreau` → le **vrai** numéro du coéquipier qui appelle, en E.164 |
| `.env` | `DEMO_SPOOF_AS=` **vide** |
| Qui appelle | le coéquipier, **depuis son propre téléphone**, vers le numéro public de Jeanne |
| Qui décroche | Jeanne, **dans l'app** |

Vérification 20 secondes avant de monter sur scène :

```bash
uv run python -c "
from vocalguard.config import get_settings
from vocalguard.infrastructure.contacts.book import JsonContactBook, normalize_number
b = JsonContactBook.from_path(get_settings().contacts_path)
print(b.owner, '|', b.by_number(normalize_number('+336XXXXXXXX')))"
# doit afficher « Jeanne Moreau | … Thomas Moreau … »
```

### Déroulé

| t | Qui / quoi | À l'écran (app + dashboard) | Ce qu'on entend |
|---|---|---|---|
| 0:00 | **Narrateur** | app : « Protection active » | « Jeanne a 82 ans. Elle a gardé son numéro. Son fils l'appelle. » |
| 0:08 | **Thomas** compose le numéro public de Jeanne | dashboard : rien encore | tonalité |
| 0:12 | l'opérateur renvoie vers Twilio, `/twilio/voice/inbound` décide | dashboard : nouvel appel, bandeau **vert**, `action: trusted_passthrough`, « **appel de confiance, non analysé** » | **silence côté Thomas — aucune annonce** |
| 0:15 | l'app sonne | app, en grand : « **Appel de Thomas Moreau** · *Contact de confiance* » | sonnerie |
| 0:20 | **Jeanne** décroche dans l'app | app : écran en appel, **pas de niveau de risque** | « Allô ? » |
| 0:24 | **Thomas** *(d'après `legit_01_famille.json`, « Lucas » → « Thomas »)* | | « Salut maman, je ne te réveille pas ? » |
| 0:28 | **Jeanne** | | « Non non mon grand, ça me fait plaisir » |
| 0:32 | **Thomas** | | « Je passe dimanche avec Camille, on apporte le gâteau. » |
| 0:38 | **Narrateur** désigne le dashboard | **aucun transcript, aucun score, aucune ligne de texte** | « Regardez l'écran. Il n'y a rien. » |
| 0:44 | **Narrateur — le message** | | « Thomas est dans le carnet. Son appel n'est **pas annoncé**, **pas transcrit**, **pas envoyé à un modèle**. Aucun octet d'audio n'a atteint notre serveur. La famille n'est jamais écoutée — ce n'est pas un réglage, c'est le routage. » |
| 0:50 | Jeanne raccroche | dashboard : appel libéré | |

### Ce qui peut mal tourner

| Symptôme | Cause probable | Plan B, en direct |
|---|---|---|
| Le dashboard affiche « inconnu — analysé » | le numéro de Thomas au carnet n'est pas sous la même forme que le `From` de Twilio | **enchaîner quand même** : « il est traité comme un inconnu, vous allez voir ce que ça donne » → on glisse sur le scénario B. Corriger le carnet en E.164 entre deux passages |
| L'app ne sonne pas | app en arrière-plan, écran verrouillé, ou token expiré | le repli `VICTIM_PHONE_NUMBER` sonne sur la 2ᵉ SIM : décrocher là, le scénario tient, on perd l'affichage « Contact de confiance » → le montrer **sur le dashboard** à la place |
| Rien n'arrive du tout | renvoi d'appel désactivé | `*#21#` pour vérifier, `**21*<numéro Twilio>#` pour réactiver. Sinon : faire composer **directement le numéro Twilio** |
| Thomas entend une annonce | la branche « confiance » n'a pas été prise | ne pas insister, enchaîner sur B |

### Coût du passage

| Poste | Estimation |
|---|---|
| Jambe entrante vers le numéro Twilio (renvoi depuis la France) | ~0,01 USD/min — `HYPOTHÈSE`, tarif entrant US à relever |
| Jambe victime vers l'app (client WebRTC) | ~0,005 USD/min — `HYPOTHÈSE` |
| Lookup (`TWILIO_LOOKUP_ENABLED=true`) | ~0,01 USD, 1 requête — `HYPOTHÈSE` |
| **Total ~50 s** | **≈ 0,03 USD** |
| *Hors Twilio* | le renvoi est facturé **à Jeanne** par son opérateur, comme un appel vers les États-Unis, souvent hors forfait (cf. `docs/terrain.md` §2) |

Avec le repli `VICTIM_PHONE_NUMBER` au lieu de l'app, ajouter **~0,16 USD/min** (jambe
vers un mobile français, tarif relevé sur le compte).

---

# Scénario B — « Faux conseiller bancaire » : la machine au travail

**Durée : 90 s.** C'est la démonstration technique : annonce à l'appelant, transcription,
quatre signaux, score qui monte, alerte chuchotée, mise en attente.

### Prérequis spécifiques

| Élément | Valeur |
|---|---|
| Téléphone appelant | `SCAMMER_PHONE_NUMBER`, **absent du carnet** |
| `.env` | `DEMO_SPOOF_AS=` **vide** |
| Dialogue | `scenarios/scam_02_faux_conseiller_bancaire.json`, dans l'ordre du fichier |
| À l'écran | `/dashboard` au projecteur, visible **pendant tout le passage** |

Le dialogue de ce fichier est étalonné pour atteindre **85** : l'identité revendiquée
(« Banque Crédit Mutuel ») existe au carnet avec un **autre** numéro, donc
`identity_mismatch` s'arme, et « donnez-moi les seize chiffres » + l'identité qui ne colle
pas déclenche le coupe-circuit *paiement + identité*. **Ne pas improviser les répliques** :
c'est précisément la formulation qui fait monter le score.

### Déroulé

| t | Qui / quoi | À l'écran | Ce qu'on entend |
|---|---|---|---|
| 0:00 | **Narrateur** | dashboard vide | « Même numéro, même Jeanne. Cette fois, l'appelant n'est dans aucun carnet. » |
| 0:05 | **L'appelant** compose le numéro public | | tonalité |
| 0:10 | `/twilio/voice/inbound`, branche « inconnu » | dashboard : nouvel appel, bandeau **gris**, « inconnu — appel analysé » | **l'appelant entend l'annonce** : « *Cet appel est analysé par un service de protection contre la fraude.* » (`CALLER_NOTICE_TEXT`) |
| 0:14 | **Narrateur**, pendant l'annonce | | « Il vient d'être **informé**. C'est ce qui rend l'analyse licite : on n'écoute que ce qui suit cette phrase. » |
| 0:17 | l'app sonne | app : « **Numéro inconnu** · *appel analysé* » + le vrai numéro | sonnerie |
| 0:20 | **Jeanne** décroche | app : jauge de risque à **0** | « Allô ? » |
| 0:24 | **Appelant** *(scam_02, t=1)* | transcript qui défile sur le dashboard | « Bonjour, madame Moreau ? Je suis votre conseiller du Crédit Mutuel, service sécurité des comptes. » |
| 0:32 | **Appelant** *(t=11)* | signal **Claude** : fausse autorité ; **réputation** : `identity_mismatch` | « C'est Banque Crédit Mutuel, service anti-fraude. Nous avons détecté des opérations suspectes sur votre compte ce matin. » |
| 0:40 | **Jeanne** | score → **orange, 40–64 (vigilance)** | « Des opérations suspectes ? Mon Dieu, mais je n'ai rien fait moi » |
| 0:44 | **Narrateur** | | « Orange. Le dashboard s'allume, le proche est notifié. **Rien d'audible** : on ne crie pas pour un soupçon. » |
| 0:52 | **Appelant** *(t=30 et 42)* | **mots-clés** : paiement, carte, urgence | « Pour sécuriser vos fonds, je vais vous faire un virement vers votre nouveau compte protégé… Donnez-moi les seize chiffres, et le code à trois chiffres derrière. » |
| 1:02 | **score ≥ 65 — alerte** | dashboard **rouge** + liste des raisons ; app : alerte en grand | **Jeanne seule entend** : « *Attention, cet appel présente des signes d'arnaque. Ne donnez aucune information bancaire.* » — **l'appelant n'entend rien** |
| 1:05 | *en parallèle* | dashboard : `action: sms_contact` | un SMS part au **vrai** proche : « Est-ce toi qui appelles en ce moment ? » |
| 1:10 | **Appelant** *(t=55)* | Claude : **secret** + isolement | « L'enquête est confidentielle. Ne parlez de cet appel à personne, même pas à votre famille. » |
| 1:18 | **score ≥ 85 — intervention** | dashboard : `hold_caller`, puis `announce_victim` | **l'appelant passe en attente** et entend de la **musique** ; puis Jeanne seule : « *Nous avons mis l'appel en pause et prévenu Lucas Martin.* » |
| 1:24 | **Narrateur — le message** | bandeau **rouge**, appelant en attente | « **Jamais de raccrochage automatique.** Un faux positif sur un vrai appel d'urgence serait grave. La mise en attente protège autant, et laisse la décision à Jeanne. » |
| 1:30 | raccrocher | | |

### Ce qui peut mal tourner

| Symptôme | Cause probable | Plan B, en direct |
|---|---|---|
| Le score reste sous 65 | Claude lent (≈2,9 s de latence, cadence `LLM_MIN_INTERVAL_SECONDS`), ou transcription dégradée par la ligne | **laisser courir 10 s de plus** en reprenant la réplique t=42, elle porte le signal paiement. Si rien : cliquer **« le proche dément »** sur le dashboard (`POST /control/calls/{id}/contact-denied`) — c'est un signal, pas une triche, et il arme le coupe-circuit `trusted_contact_denied` |
| Le score explose trop tôt | — | ce n'est pas un problème : commenter l'alerte et sauter directement au palier intervention |
| L'appelant entend l'annonce | fuite de `announce_url` (cf. `docs/terrain.md` §7) | mettre l'appelant en attente **avant** d'annoncer : `curl -s -X POST localhost:8000/control/calls/<CallSid>/hold`, ou le bouton du dashboard |
| Le SMS n'arrive jamais | délivrabilité d'un long code US vers la France | **ne pas attendre sur scène**. Le dire : « en production c'est un SMS ; ici, le bouton du dashboard injecte exactement le même signal » → `contact-denied` |
| Transcription muette | creds AWS expirés | `make check-aws`. Si c'est mort, passer en `TRANSCRIBE_BACKEND=fake` + rejeu (§Rejeu) **plutôt que de parler dans le vide** |
| Le CDN du SDK Voice est bloqué | réseau du lieu | abandonner l'app pour ce passage : décrocher sur `VICTIM_PHONE_NUMBER` et raconter l'histoire **sur le dashboard** |

### Coût du passage

| Poste | Estimation |
|---|---|
| Jambe entrante + jambe app, 90 s | ≈ 0,03 USD — `HYPOTHÈSE` (mêmes tarifs qu'au scénario A) |
| Transcribe streaming, ~2 × 90 s de pistes | ≈ 0,07 USD — `HYPOTHÈSE`, tarif public à relever |
| Bedrock / Claude, ~25 appels à ~2,5 s d'intervalle, fenêtre de 12 tours | ≈ 0,05 USD — `HYPOTHÈSE` |
| 1 SMS vers la France | ≈ 0,08 USD — `HYPOTHÈSE` |
| 1 Lookup | ≈ 0,01 USD — `HYPOTHÈSE` |
| **Total** | **≈ 0,25 USD** |

Si on inverse le sens de l'appel avec `scripts/lancer_appel_demo.py` (Twilio appelle le
téléphone de l'appelant pour que le coût bascule du forfait français vers le compte
Twilio), **ajouter ~0,16 USD/min**, soit **≈ 0,24 USD** pour 90 s.

---

# Scénario C — « Lucas » avec numéro usurpé et voix clonée

**Durée : 90 s. C'est le moment fort du pitch.** Les deux scénarios précédents existent
pour préparer celui-ci : A prouve que la liste blanche laisse passer la famille, B que
l'analyse marche. C montre ce qui se passe quand l'arnaqueur **bat les deux**.

### Prérequis spécifiques

| Élément | Valeur |
|---|---|
| `.env` | `SCAMMER_PHONE_NUMBER=<téléphone de l'appelant>` **et** `DEMO_SPOOF_AS=+336LLLLLLLL` (le numéro de **Lucas Martin** au carnet) |
| | `TRUSTED_CONTACT_PHONE_NUMBER=+336LLLLLLLL` — **la même valeur**, et le même numéro que Lucas Martin dans `data/contacts.json`. Trois endroits, une seule valeur |
| | `VERIFY_CODE_TTL_SECONDS=120` (défaut) |
| Dialogue | `scenarios/scam_01_petit_fils.json` |
| Voix | enregistrement de voix clonée **avec consentement écrit du coéquipier**, joué au haut-parleur près du micro du téléphone appelant. À défaut : le coéquipier imite, et le narrateur dit honnêtement « en production, c'est une voix clonée » |
| Placement | **le vrai Lucas est debout, à l'écart, son téléphone en main, visible du jury** |

Le réglage `DEMO_SPOOF_AS` ne simule **que** ce que notre serveur croit du `From`
(Twilio interdit d'usurper un numéro qu'on ne possède pas — et c'est une bonne chose). Il
est inerte sans `SCAMMER_PHONE_NUMBER` (`demo_spoof_active`) et **chaque usage est logué
en MAJUSCULES**. Il sera **remis à vide après la démo** : un `.env` oublié ferait croire
de confiance n'importe quel inconnu.

Contrôle juste avant de monter sur scène :

```bash
uv run python -c "
from vocalguard.config import get_settings
s = get_settings(); print('demo_spoof_active =', s.demo_spoof_active)"   # doit être True
```

### Déroulé

| t | Qui / quoi | À l'écran | Ce qu'on entend |
|---|---|---|---|
| 0:00 | **Narrateur** | dashboard vide | « Dernier appel. Cette fois, l'arnaqueur a fait ses devoirs : il a **le numéro de Lucas** et **la voix de Lucas**. » |
| 0:06 | **L'appelant** compose le numéro public ; `DEMO_SPOOF_AS` s'applique | journal de `make run` : ligne **en MAJUSCULES** signalant l'usurpation simulée | tonalité |
| 0:12 | branche **confiance** : le numéro est au carnet | dashboard : bandeau **vert**, `action: trusted_passthrough`, « **appel de confiance, non analysé** » | **aucune annonce côté appelant** |
| 0:15 | l'app sonne | app : « **Appel de Lucas Martin** · *Contact de confiance* » | sonnerie |
| 0:18 | **Narrateur** — poser le problème | | « Notre liste blanche vient de se faire avoir. Et c'est **normal** : le numéro est le bon. Aucun filtre par numéro ne peut faire mieux. » |
| 0:22 | **Jeanne** décroche | app : écran en appel, pas de score | « Lucas ? » |
| 0:26 | **« Lucas »** *(voix clonée, scam_01 t=1 et 7)* | | « Mamie ? Mamie c'est Lucas, tu m'entends ? … J'ai eu un accident avec la voiture. » |
| 0:34 | **Jeanne** | | « Mon Dieu, tu n'as rien ? » |
| 0:38 | **« Lucas »** *(t=15 et 21)* | | « Il faut payer le garage tout de suite… Il faut faire un virement de deux mille euros maintenant, c'est urgent. » |
| 0:46 | **Jeanne appuie sur « Vérifier son identité »** dans l'app | `POST /control/calls/{CallSid}/verify` ; dashboard : `action: verify_requested` | *(rien dans l'appel — le code n'est **jamais** prononcé dans la conférence)* |
| 0:50 | le serveur choisit le destinataire : **le numéro du carnet**, pas celui de l'appel | app : le code à 4 chiffres **en très grand**, compte à rebours de **120 s** | Jeanne seule entend : « *Un code a été envoyé à Lucas Martin. Le code est 4… 8… 2… 1. Demandez-lui de vous le lire.* » |
| 0:54 | **le vrai Lucas lève son téléphone** — le geste du passage | | sa sonnerie de SMS, dans la salle |
| 0:56 | **Narrateur** le désigne | | « Le code n'est pas parti dans l'appel. Il est parti **là**. » |
| 1:00 | **« Lucas »** au téléphone, pris au piège *(improvisation contrainte — il n'a pas le code)* | | « Euh… mamie, je n'ai pas reçu de SMS, mon téléphone est cassé, laisse tomber le code… » |
| 1:08 | **Jeanne** | | « Alors dis-moi le code, mon chéri. » |
| 1:12 | le code n'arrive pas (ou le vrai Lucas répond **NON** au SMS) | dashboard : `identity_verified: false` / `contact_denied`, score → **≥ 85** | **l'appelant passe en attente** (musique) ; puis Jeanne seule : « *Nous avons mis l'appel en pause et prévenu Lucas Martin.* » |
| 1:20 | **Narrateur — LA PHRASE DU PITCH.** *(à dire lentement, en regardant le jury, pas l'écran)* | appelant en attente, bandeau rouge, le vrai Lucas téléphone en main | « **Son numéro était parfait. Sa voix était parfaite. Mais le code n'est pas parti vers l'appel : il est parti vers le vrai Lucas. Un escroc peut copier un numéro. Il peut copier une voix. Il ne peut pas tenir le téléphone de votre petit-fils.** » |
| 1:30 | raccrocher, silence | | |

> **Ne pas enchaîner tout de suite.** Laisser deux secondes de silence après la phrase.
> C'est la seule chose que le jury retiendra de la démo.

### Si on veut une variante plus courte (70 s)

Supprimer les répliques t=34 et t=1:08 et déclencher la vérification dès 0:38. On perd
l'escalade, on garde le moment fort.

### Ce qui peut mal tourner

| Symptôme | Cause probable | Plan B, en direct |
|---|---|---|
| **Le SMS n'arrive pas au vrai Lucas** | délivrabilité long code US → France (cf. `docs/terrain.md` §4) | **le plus probable, et le plus préparé** : le code s'affiche **aussi en grand sur l'app** — le montrer. Puis le vrai Lucas dit « je n'ai rien reçu », et on clique **« le proche dément »** : `curl -s -X POST localhost:8000/control/calls/<CallSid>/contact-denied`, ou le bouton du dashboard. Le même signal est injecté, l'escalade est identique. **Le dire au jury plutôt que de le cacher** : « ce bouton existe exactement parce que le SMS n'est pas garanti » |
| L'appel est traité comme **inconnu** (annonce côté appelant, bandeau gris) | `demo_spoof_active` à `False` : `SCAMMER_PHONE_NUMBER` ou `DEMO_SPOOF_AS` vide, ou l'appelant appelle depuis un autre téléphone que `SCAMMER_PHONE_NUMBER` | **ne pas s'arrêter** : « il n'a pas réussi à usurper le numéro, voyons le cas où il y arrive » → rejouer avec le bon téléphone, ou basculer sur la vidéo de rejeu de `scam_01` |
| Le bouton « Vérifier son identité » ne répond pas | `POST /control/calls/{id}/verify` en erreur, ou `call_id` ≠ `CallSid` de la jambe d'origine | déclencher au `curl` depuis le portable : `curl -s -X POST localhost:8000/control/calls/<CallSid>/verify` — récupérer le `CallSid` dans `curl -s localhost:8000/healthz` (`active_calls`) |
| L'app ne sonne pas | arrière-plan / verrouillage | repli `VICTIM_PHONE_NUMBER` : la jambe vocale tient, mais **on perd l'affichage du code**. Dans ce cas, le code n'existe que dans le SMS et dans l'annonce vocale → le vrai Lucas doit le **lire à voix haute** au micro |
| La voix clonée ne convainc pas | enregistrement, haut-parleur | ce n'est pas le cœur : la voix est un **indice plafonné** (jamais > 64 à elle seule). Le dire : « même une imitation parfaite ne passe pas la vérification par code » |
| Deux appels actifs en même temps | un appel précédent mal libéré | `curl -s localhost:8000/healthz` avant de commencer : `active_calls` doit être vide. Avec deux appels actifs, une réponse SMS devient ambiguë et est **volontairement ignorée** |

### Coût du passage

| Poste | Estimation |
|---|---|
| Jambe entrante + jambe app, 90 s | ≈ 0,03 USD — `HYPOTHÈSE` |
| Transcription / Claude | **≈ 0** — appel de **confiance**, donc ni `<Stream>` ni analyse. C'est le scénario le moins cher, et c'est aussi un argument produit |
| 1 SMS de vérification vers la France | ≈ 0,08 USD — `HYPOTHÈSE` |
| 1 SMS de réponse entrant (si le vrai Lucas répond NON) | ≈ 0,01 USD — `HYPOTHÈSE` |
| **Total** | **≈ 0,12 USD** |

### Après la démo — à faire tout de suite

- [ ] `DEMO_SPOOF_AS=` remis **vide** dans le `.env`
- [ ] `##21#` sur le téléphone de Jeanne si on ne rejoue pas (le renvoi est facturé)
- [ ] relever la consommation dans Console → Monitor → Usage et la reporter dans
      `docs/terrain.md` §9

---

# Rejeu sans téléphone — la vidéo de secours

À enregistrer **la veille**, pendant que tout fonctionne. C'est le filet si le réseau du
lieu bloque Bedrock, si ngrok tombe, si les creds AWS expirent, ou si un téléphone ne
sonne pas.

```bash
TRANSCRIBE_BACKEND=fake make replay SCENARIO=scenarios/scam_01_petit_fils.json
```

Le rejeu pousse les répliques du fichier dans le pipeline réel (quatre signaux, score,
seuils, actions) **sans Twilio, sans AWS et sans téléphone**. Le dashboard réagit
exactement comme en vrai : c'est le même code, seule la source d'audio change. Avec
`TRANSCRIBE_BACKEND=aws` et des creds valides, Claude est réellement sollicité et le
score est le vrai score.

### Les sept scénarios disponibles, et ce que chacun prouve

| Fichier | Attendu | Ce que ça démontre |
|---|---|---|
| `scam_01_petit_fils.json` | ≥ 85, *intervene* | le faux proche en urgence — **la base du scénario C** |
| `scam_02_faux_conseiller_bancaire.json` | ≥ 85, *intervene* | l'identité **institutionnelle** — **la base du scénario B** |
| `scam_03_fausse_autorite.json` | ≥ 85, *intervene* | la fausse gendarmerie, un troisième chemin d'autorité |
| `legit_01_famille.json` | ≤ 39, *none* | le vrai Lucas, numéro connu, parle d'argent → **rien ne se déclenche** |
| `legit_02_fille_urgence_reelle.json` | ≤ 39, *none* | **le test le plus dur** : vraie fille, vraie urgence, hôpital et argent. Une mise en attente ici couperait un appel d'urgence réel |
| `legit_03_vrai_conseiller_bancaire.json` | ≤ 39, *none* | le vrai conseiller, depuis le numéro de la banque au carnet |
| `_smoke_technique.json` | — | vérification de plomberie, pas à montrer |

### Ce qu'on filme, dans cet ordre

1. **`legit_02`** d'abord, 30 s. C'est le plus contre-intuitif et il désarme l'objection
   « vous allez bloquer les vrais appels » : vraie urgence, vrai numéro, **score sous 40**.
2. **`scam_01`**, 40 s : le score monte, le coupe-circuit *paiement + identité* s'arme,
   l'intervention part.
3. **`scam_02`**, 30 s : un chemin différent (identité institutionnelle), même issue —
   ça montre que ce n'est pas un cas particulier codé en dur.

Écran à filmer : le `/dashboard` au premier plan, le terminal de rejeu en second. Narration
en voix off, **sans parler par-dessus** l'annonce vocale. Trois clips séparés, pas un
seul long : on veut pouvoir n'en jouer qu'un.

### Si on doit basculer sur la vidéo en direct

Ne pas s'excuser, ne pas déboguer devant le jury. Une phrase, et on enchaîne :

> « Le réseau de la salle ne nous laisse pas passer l'appel. On vous montre le même
> pipeline sur un scénario rejoué — c'est le même code, seule la source audio change. »

Puis on reprend le script du scénario correspondant, **au même minutage** : les
commentaires du narrateur restent valables mot pour mot.
