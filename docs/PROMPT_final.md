Tu es un développeur senior. Tu reprends un projet de hackathon déjà fonctionnel et tu dois l'amener à sa version finale. Lis tout le dépôt avant d'écrire du code (README.md, src/vocalguard/, tests/, docs/). Ne casse rien : tous les tests existants doivent rester verts (`make test`, `make lint`).

## Le produit, en une phrase
VocalGuard protège une personne (souvent âgée) contre les arnaques téléphoniques : elle garde son vrai numéro, ses appels passent par notre serveur, ses proches passent sans être écoutés, les inconnus sont prévenus puis analysés en temps réel, et une application sur son téléphone affiche le danger pendant l'appel.

## Ce qui existe déjà (ne pas refaire)
- Backend Python FastAPI, architecture hexagonale (`domain/`, `application/`, `infrastructure/`, `interface/`).
- Téléphonie Twilio : la personne renvoie sa ligne vers un numéro Twilio ; le webhook `/twilio/voice/inbound` reçoit l'appel ; une conférence Twilio réunit l'appelant et la personne protégée ; l'audio part en direct vers `/ws/media`.
- Tri par carnet de contacts (`data/contacts.json`) : contact de confiance = appel transmis sans annonce ni analyse ; inconnu = annonce à l'appelant puis analyse.
- Analyse : transcription Amazon Transcribe + 4 signaux (Claude via Bedrock sur le contenu, mots-clés, voix synthétique, réputation du numéro) → score 0–100 → 3 seuils : vigilance (40), alerte (65), intervention (85) → actions : message à l'oreille de la personne seule, SMS au vrai proche, mise en attente de l'appelant. Jamais de raccrochage automatique.
- Vérification d'identité : bouton « Vérifier » → code à 4 chiffres envoyé par SMS au vrai numéro du proche (celui du carnet) ; l'appelant doit le lire à voix haute.
- Une app web installable (`/app`, SDK Twilio Voice JS) qui sonne sur le téléphone, protégée par un code `APP_PIN`.

## Ce que je veux en version finale

### 1. Un écran d'appel qui ressemble à un vrai appel de smartphone
- Appel entrant plein écran : avatar rond avec initiales (ou silhouette pour un inconnu), nom en très grand, numéro, provenance en petit (« Mobile · France », « Numéro masqué », « Étranger · +44 »), badge « Contact de confiance » (vert) ou « Inconnu — appel analysé » (orange).
- Deux gros boutons ronds en bas : Refuser (rouge) et Accepter (vert, qui pulse). Sonnerie type téléphone (Web Audio) et vibration sur Android. L'écran ne se met pas en veille quand la protection est active (Wake Lock).
- Appel en cours : chronomètre, grille de boutons ronds Muet / Haut-parleur / Clavier / Vérifier / Attente, gros bouton Raccrocher rouge.
- Pendant un appel analysé : un bandeau de risque discret (couleur + « Normal / Vigilance / Alerte / Danger » + score) qui devient une alerte rouge plein écran au niveau alerte ou intervention : « Ne donnez aucun code ni numéro de carte ».
- Le code de vérification s'affiche en grand avec un compte à rebours et la consigne : « Ne dites jamais ce code. C'est à lui de vous le lire. »
- Fin d'appel : « Appel terminé · 03:12 » puis retour à l'écran « Protection active ».
- Mobile d'abord (iPhone Safari et Android Chrome), très lisible pour une personne âgée, en français.

### 2. Un historique des appels avec les métriques
- Stocker chaque appel dans une base SQLite locale (`data/calls.sqlite`, hors git), **sans jamais stocker d'audio ni de transcription**.
- Pour chaque appel : date, durée, décroché ou manqué, numéro, nom du contact s'il est connu, **provenance** (contact / inconnu / masqué / étranger + pays déduit de l'indicatif + type de ligne si connu), chemin suivi (confiance ou analysé), **score maximum et niveau atteint**, **le moment (en secondes) où chacun des 3 seuils — vigilance, alerte, intervention — a été franchi**, le pic de chacun des 4 signaux, les tactiques d'arnaque détectées, la phrase d'explication de Claude, les actions déclenchées, et le résultat de la vérification par code (aucune / vérifiée / échouée / expirée).
- Rétention 30 jours par défaut (réglable), et un bouton pour tout effacer.
- Dans l'app : un écran « Historique » comme les « Récents » d'un téléphone (avatar, nom ou numéro, provenance, heure, durée, pastille de couleur du niveau atteint, appels manqués en rouge), et un écran de détail par appel avec une frise horizontale montrant quand chaque seuil a été franchi, les barres des 4 signaux, les tactiques, l'explication et les actions.
- Ces routes sont protégées par `APP_PIN`.

### 3. L'annonce à l'appelant en anglais
- L'appelant inconnu entend, avant toute analyse : « This call is being analyzed by a fraud protection service. »
- Langue réglable (`CALLER_NOTICE_LANGUAGE=en-US`), option pour ajouter la phrase française juste après.
- Ce que la personne protégée entend reste en français.
- Dire « analyzed », jamais « recorded » : rien n'est enregistré.

### 4. Fiabilité et sécurité (indispensable avant toute démo)
- Si l'app n'est pas ouverte ou ne répond pas en 25 secondes : rappeler le numéro de secours (`VICTIM_PHONE_NUMBER`) s'il existe, sinon dire à l'appelant « The person is not available » et raccrocher ; l'appel apparaît « manqué » dans l'historique. L'appelant doit entendre une tonalité de sonnerie pendant l'attente, pas un silence.
- Protéger par `APP_PIN` toutes les routes de contrôle (`/control/*`) et le flux en direct (`/ws/dashboard`), et effacer les événements d'un appel quand il se termine.
- Code de vérification : n'accepter qu'un nombre de 4 chiffres exactement, 2 essais maximum, sinon échec.
- PIN : 6 chiffres minimum, blocage 10 minutes après 5 erreurs.

## Contraintes
- Python 3.12, FastAPI ; pas de nouveau framework front (HTML/CSS/JS dans `interface/static/`).
- Le format d'événement (`contracts/event.schema.json`) ne change pas.
- Aucun secret dans le code, les logs ou les commits.
- Travaille par petites étapes avec des commits clairs, et ajoute des tests pour chaque fonctionnalité.

## Ordre de travail
1. Fiabilité et sécurité (section 4)
2. Écran d'appel (section 1)
3. Annonce en anglais (section 3)
4. Stockage de l'historique puis écran Historique (section 2)

## C'est fini quand
- Tous les tests et le lint passent.
- Tu fournis des captures d'écran mobiles (390×844) de : appel entrant, appel en cours avec risque, code de vérification, fin d'appel, historique, détail d'un appel.
- Un vrai appel de test produit une ligne d'historique correcte (provenance, durée, seuils franchis).
- L'appelant inconnu entend l'annonce en anglais.
- README et `.env.example` sont à jour avec les nouveaux réglages.
- Tu me donnes un récapitulatif : ce qui marche, ce qui a été testé au vrai téléphone, ce qui reste à faire.
