# Recette du MVP VocalGuard — 10 octobre 2026

Le MVP comprend l'accueil Téléphone (Récents, Contacts, Clavier, Messagerie), les
appels entrants et sortants, la protection en direct, l'historique des quatre
signaux et trois seuils, et une messagerie vocale avec écoute et suppression.

L'état initial a été sauvegardé et poussé sur `main` (`6697f0e`) avant les
modifications. Le développement et les vérifications sont sur
`feat/appel-historique`, avec trois agents : interface, téléphonie et évaluation.

## Validation automatisée

```bash
make lint
make test
```

Les tests couvrent notamment le routage de confiance, le repli après 25 secondes,
les callbacks répétés ou en retard, les signatures Twilio HTTP/WebSocket, le PIN
et son blocage, les codes à quatre chiffres et deux essais, les données SQLite,
la purge et l'effacement, ainsi que la suppression des transcripts en mémoire
à la fermeture. Les appels sortants sont autorisés par un nonce lié à l'identité,
avec retries idempotents du même appel et interdiction de réemploi sur un autre.

Les tests de messagerie vérifient le dépôt attendu, les callbacks authentifiés,
la lecture privée, la suppression distante, la rétention avec retry et l'absence
d'audio dans SQLite. Tous les fournisseurs et numéros utilisés par ces tests sont
simulés : aucun appel ni SMS réel n'a été envoyé.

## Vérification de l'interface

Chrome et Node doivent être installés. Playwright est isolé dans un dossier
temporaire, sans ajouter de build JavaScript à l'application :

```bash
npm install --prefix /tmp/vocalguard-qa playwright@1.56.1
NODE_PATH=/tmp/vocalguard-qa/node_modules make test-ui
```

`PLAYWRIGHT_CHANNEL=chromium` permet d'utiliser le navigateur Playwright installé
à la place de Chrome. Les scripts servent le vrai HTML de l'app dans un navigateur
réel et remplacent uniquement le SDK et les échanges réseau. Les messages vocaux
de QA sont des fichiers WAV générés, décodables, sans voix ni donnée personnelle.

La QA couvre l'activation, les appels, le chronomètre, Muet, DTMF, la sortie audio,
Attente/Reprise, les alertes, la vérification, les récents et leur détail,
l'effacement confirmé, les contacts, la numérotation sortante, et la messagerie.
Elle vérifie aussi les réponses arrivant après un nouvel appel, les événements
SDK obsolètes, les verdicts de vérification, le focus des dialogues et la
révocation des URL audio. Le PIN n'entre pas dans localStorage/sessionStorage.

Les captures sont dans [qa/appel-historique](qa/appel-historique). Les données,
codes, noms, numéros et appels visibles sont fictifs.

## Évaluation des scénarios

```bash
make evaluate
make evaluate EVAL_MODE=keywords
make evaluate EVAL_MODE=bedrock
```

Le jeu contient 22 conversations synthétiques : 11 arnaques et 11 appels
légitimes, en français, anglais et alternance de langues. Il comprend de fausses
banques, une livraison avec demande de code, une usurpation familiale, un faux
support, de fausses autorités, une urgence médicale, une facture normale et un
proche utilisant un téléphone prêté.

Le [rapport hors ligne](../artifacts/evaluation/offline/report.md) utilise
`FakeContentAnalyzer` : 3 arnaques sur 11 atteignent l'alerte, 8 sont manquées et
aucun des 11 appels légitimes ne déclenche l'alerte. Ces résultats montrent les
limites de la doublure ; **ils ne mesurent pas Claude**. Les
[mots-clés seuls](../artifacts/evaluation/keywords/report.md) n'atteignent pas le
seuil d'alerte. Ils servent de filet de vigilance et ne remplacent pas Claude.

Les rapports distinguent l'analyse des transcripts synthétiques de la projection
du routage réel : un numéro de confiance passe sans analyse. Une usurpation d'un
numéro du carnet peut donc échapper au scoring ; la vérification manuelle reste
utile. Les taux excluent les réponses absentes ou invalides et indiquent leur
couverture. Les latences locales ne sont pas des latences AWS ou téléphoniques.

Le prompt reconnaît désormais les négations, les citations et les paiements
légitimes. Le coupe-circuit « paiement inhabituel + identité discordante » exige
un risque de contenu d'au moins 0,65. Ce seuil de MVP est heuristique ; il reste à
calibrer sur des appels réels et un jeu indépendant plus large.

**Claude/Bedrock n'a pas été testé pendant cette recette : aucun identifiant AWS
accessible n'était présent dans les variables exportées, `.env` ou le fichier
de credentials.** Le mode Bedrock permet le test réel avec les réglages du projet,
sans téléphonie, et échoue explicitement si AWS n'est pas disponible.

## Configuration et recette au téléphone

1. Régler le numéro Twilio : Voice URL
   `https://HOST/twilio/voice/inbound`, méthode POST. Conserver
   `TWILIO_VALIDATE_SIGNATURE=true` et `PUBLIC_HOSTNAME=HOST` exact.
2. Régler la **TwiML App** référencée par `TWILIO_TWIML_APP_SID` : Voice URL
   `https://HOST/twilio/voice/outbound`, méthode POST ; Voice Status Callback
   `https://HOST/twilio/voice/status`, méthode POST. Ce callback nettoie notamment
   un appel annulé avant la création de sa jambe téléphonique. Les callbacks de
   la jambe sortante et de messagerie sont déclarés par le TwiML.
3. Configurer un PIN de six chiffres minimum, les clés Twilio et le carnet.
   La ligne de secours doit être distincte du numéro public renvoyé et de Twilio.
   Les appels sortants affichent le numéro Twilio comme numéro d'appelant.
4. Exporter les identifiants AWS du workshop dans le terminal qui lance le serveur,
   ou renseigner le profil partagé. Ne pas copier les clés dans les logs/rapports :

   ```bash
   make check-aws
   make evaluate EVAL_MODE=bedrock
   make run PORT=8001
   ```

5. Dans un autre terminal, `make tunnel PORT=8001`, puis actualiser l'hôte et les
   URLs si ngrok a changé. Ouvrir `/app` sur le téléphone et activer la protection.
6. Depuis un numéro inconnu, faire un appel test : vérifier l'annonce anglaise,
   les événements `transcript`, `llm`, `keyword`, `score`, la progression de risque,
   puis les métadonnées et les seuils de l'appel dans Récents. La console affiche
   les événements sans texte de conversation ; le dashboard par PIN montre le
   transcript en direct pendant l'appel.
7. Tester séparément un appel légitime urgent, un contact de confiance, le clavier
   sortant, puis un dépôt vocal avec l'app fermée et sans secours joignable.
   Vérifier l'écoute du message et sa suppression dans l'app et chez Twilio.

**Ces étapes au vrai téléphone, l'annonce réellement entendue, les appels sortants
réels et le dépôt vocal chez Twilio restent à valider.** Aucun résultat local ne
les remplace.

## Conservation et limites

Les conversations analysées restent sans enregistrement ni transcript persistant.
La messagerie conserve uniquement le message déposé après l'annonce et le bip,
120 secondes maximum. L'audio reste chez Twilio ; SQLite ne conserve que les
métadonnées. La lecture passe par le serveur avec authentification, conformément
à l'[API des fichiers d'enregistrement Twilio](https://www.twilio.com/docs/voice/api/recording#retrieve-a-recordings-media-file).

Par défaut, la rétention est de 30 jours. La purge audio vérifie les messages
expirés au démarrage puis chaque heure ; si Twilio est inaccessible, elle réessaie
et les messages expirés ne sont plus proposés à l'écoute. Un serveur arrêté ne
peut pas supprimer les fichiers distants. L'effacement de l'historique des appels
et la suppression d'un message vocal sont deux actions distinctes.

La PWA ne remplace pas l'app Téléphone système et ne gère pas les appels de la SIM
ou les numéros d'urgence. Une app native est nécessaire pour cette intégration
et la sonnerie fiable lorsque la page est fermée. Ce MVP fonctionne avec un seul
worker ; sessions, nonces et compteurs de blocage du PIN vivent dans son processus.
