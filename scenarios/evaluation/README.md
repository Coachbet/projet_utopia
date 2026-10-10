# Jeu de diversité VocalGuard

Ces conversations sont synthétiques et ne représentent pas des appels enregistrés.
Les numéros sont des identifiants de fixtures : aucun appel ni SMS n'est émis.

L'étiquette `evaluation.label` décrit l'intention écrite du scénario, indépendamment
des heuristiques de la doublure. Une arnaque doit atteindre l'alerte, un appel
légitime peut atteindre la vigilance mais doit rester sous l'alerte. Les résultats
hors ligne peuvent donc comporter des erreurs attendues : ils exposent les limites
du moteur de démonstration et ne mesurent pas Claude.

Les appels en anglais et les échanges FR/EN sondent la compréhension du modèle.
Son explication reste en français. La transcription AWS, la détection de voix,
le réseau téléphonique et le routage qui dispense les contacts de confiance de
l'analyse ne sont pas évalués ici. Tous les transcripts synthétiques sont fournis
au pipeline, y compris ceux des contacts, pour tester les faux positifs du score.
Chaque segment est évalué sans délai minimal entre analyses, ce qui diffère de la
cadence en production. Le temps de première alerte utilise l'horloge synthétique
du scénario et ne mesure pas une latence de détection téléphonique.

Exécution reproductible, sans AWS :

```sh
uv run python scripts/evaluate_scenarios.py --mode offline --output-dir artifacts/evaluation/offline
```

Évaluation Bedrock réelle, explicitement autorisée et facturée par AWS :

```sh
# Configurer le profil local ~/.aws/credentials, ou exporter AWS_ACCESS_KEY_ID,
# AWS_SECRET_ACCESS_KEY et, pour un rôle temporaire, AWS_SESSION_TOKEN.
# Un profil nommé se choisit avec AWS_PROFILE. Les replis EC2/ECS sont désactivés.
# Ne jamais coller les identifiants dans le rapport.
uv run python scripts/evaluate_scenarios.py --mode bedrock --output-dir artifacts/evaluation/bedrock
```

Le mode Bedrock lit le modèle, la région, les poids et les seuils de la configuration
de l'application (`.env` et variables exportées). Les options `--model` et `--region`
permettent un remplacement explicite. Le mode hors ligne ignore `.env`.

Les rapports ne contiennent aucune phrase des conversations ni explication brute
du modèle. Ils publient uniquement les étiquettes, scores, métriques agrégées et
catégories d'erreurs. Le sous-ensemble valide et la couverture Bedrock sont indiqués
explicitement : une panne du modèle n'est jamais comptée comme une réussite.
