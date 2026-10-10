# Projet Utopia — Détecteur d'arnaques vocales

Garde-fou temps réel : on écoute l'appel via Twilio, on calcule un score de risque à
partir de quatre signaux, et on protège la victime **pendant** l'appel — alerte
chuchotée, mise en attente du scammeur, SMS au vrai proche.

> **Pas de raccrochage automatique.** Un faux positif sur un vrai appel d'urgence
> serait grave. La mise en attente protège autant et laisse la décision à la victime.

## Démarrage

```bash
cp .env.example .env      # puis remplir (cf. section Secrets)
make install              # uv sync, sans torch
make check-aws            # H0 : Bedrock et Transcribe sont-ils autorisés ?
make run                  # API sur :8000
make tunnel               # ngrok, dans un autre terminal
```

La détection de voix synthétique est optionnelle et lourde (~2 Go de torch) :
`make install-voice` seulement sur la machine de Dev 2.

Pour travailler **sans téléphone**, rejouer un scénario écrit dans le pipeline :

```bash
make replay SCENARIO=scenarios/scam_01_petit_fils.json
```

## Les quatre signaux

| Signal | Mesure | Poids |
|---|---|---|
| Claude (contenu) | Urgence, secret, paiement inhabituel, fausse autorité, identité revendiquée | 45 |
| Mots-clés | Termes à risque pondérés, FR + EN | 20 |
| Voix synthétique | Probabilité « fake » lissée sur plusieurs fenêtres | 20 |
| Réputation | Ligne VoIP, numéro inconnu, identité qui ne colle pas au numéro | 15 |

**Principe directeur :** le contenu et la vérification auprès du vrai contact portent
la décision. La voix synthétique est un indice, **jamais un verdict à elle seule** —
un plafond l'empêche de dépasser 64 à elle seule, ce qui limite les faux positifs.

### Seuils

| Score | Niveau | Effet |
|---|---|---|
| < 40 | — | rien |
| 40–64 | vigilance | dashboard orange, notification au proche. Rien d'audible. |
| 65–84 | alerte | annonce à la victime seule + SMS au vrai contact |
| ≥ 85 | intervention | scammeur en attente, puis annonce à la victime |

### Coupe-circuits

Trois règles empêchent un signal très fort d'être dilué par la moyenne :

1. Paiement inhabituel **et** identité revendiquée qui ne correspond pas au numéro → score ≥ 85.
2. Risque Claude ≥ 0,9 → score ≥ 65, quels que soient les autres signaux.
3. La voix synthétique seule ne fait jamais dépasser 64.

## Architecture (DDD / hexagonale)

Le découpage sert un but précis pour ce hackathon : **chacun peut avancer seul**.
Le domaine ne connaît ni Twilio ni AWS, donc Dev 2 et Biz 1 travaillent sur de faux
événements tant que la téléphonie n'est pas prête.

```
src/vocalguard/
├── domain/              # cœur métier, zéro dépendance externe
│   ├── events.py        # le contrat Event — FIGÉ À H0
│   ├── models.py        # Call, Contact, les quatre signaux
│   ├── alerts.py        # seuils et politique d'alerte
│   ├── ports.py         # interfaces : Transcriber, ContentAnalyzer, CallController…
│   └── scoring/         # somme pondérée + coupe-circuits
├── application/         # orchestration : session d'appel, pipeline, intervention
├── infrastructure/      # adapters, un par dépendance externe
│   ├── telephony/       # Twilio : TwiML, μ-law, hold/announce, Lookup, SMS
│   ├── aws/             # Transcribe streaming, Bedrock/Claude
│   ├── keywords/        # moteur regex + lexique FR
│   ├── voice/           # wav2vec2
│   ├── contacts/        # carnet d'adresses
│   └── fakes/           # doublures pour bosser sans téléphone ni AWS
└── interface/           # FastAPI : webhooks TwiML, WS media, WS dashboard, static/
```

Le dossier `telephony/` ne s'appelle pas `twilio/` pour ne pas masquer le paquet pip
`twilio` à l'import.

## Contrat d'événement — figé à H0

Toute brique n'émet et ne consomme que ça. Schéma complet dans
[`contracts/event.schema.json`](contracts/event.schema.json).

```json
{"call_id": "CA123", "t": 12.4, "type": "transcript | keyword | llm | voice | reputation | score | action",
 "speaker": "caller | victim", "data": {"text": "…", "risk": 0.82, "reasons": ["urgence", "paiement inhabituel"]}}
```

`caller` = l'appelant (scammeur présumé, piste Twilio `inbound`).
`victim` = la personne appelée (piste `outbound`). Les pistes séparées sont ce qui
nous permet de savoir qui parle.

## Endpoints (à régler dans la console Twilio)

Le serveur est en place. Une fois `make tunnel` lancé et `PUBLIC_HOSTNAME` renseigné,
il n'y a qu'une seule URL à coller dans la console : le webhook de réception du numéro.
Tout le reste, Twilio le rappelle tout seul à partir du TwiML qu'on lui renvoie.

| Route | Rôle |
|---|---|
| `POST /twilio/voice/inbound` | **À coller dans la console** (A call comes in). Branche `<Stream>` puis la conférence, et appelle la victime. |
| `wss://…/ws/media` | Audio μ-law des deux pistes → transcription par locuteur |
| `/twilio/voice/join/{call_id}` | TwiML de la jambe victime |
| `/twilio/voice/announce/{call_id}` | Phrase dite à la victime seule (`announce_url`) |
| `/twilio/voice/hold-music` | Ce qu'entend le scammeur en attente |
| `/twilio/voice/status` | Fin d'appel : libère la session |
| `GET /healthz` | Backend choisi, Twilio configuré ou non, appels en cours |

Sans credentials Twilio, le serveur démarre quand même et **trace** les actions au lieu
de les exécuter : le pipeline complet reste testable sans téléphone
(`TRANSCRIBE_BACKEND=fake` + `make replay`).

Pour Dev 2, deux points de branchement, sans rien modifier de l'existant :

```python
create_app(sink=mon_bus, audio_tap_factory=lambda session: mon_detecteur_de_voix)
```

- `sink` remplace la sortie console par le bus d'événements / le WebSocket du dashboard ;
- `audio_tap_factory` reçoit l'audio PCM16 décodé de l'appelant (voix synthétique) ;
- `CallSession.window()` donne la fenêtre glissante de transcript à envoyer à Claude,
  `CallSession.signals` accueille les quatre signaux ;
- `InterventionRunner.handle_event(event)` consomme un événement `score` et déclenche
  les actions Twilio — c'est la seule chose à appeler pour fermer la boucle.

## Rôles

| Rôle | Responsabilités | Premier livrable |
|---|---|---|
| **Dev 1** — Téléphonie & temps réel | Compte et numéro Twilio, TwiML (stream + conférence), WebSocket + ngrok, décodage μ-law, Transcribe streaming, actions Twilio | Un appel réel produit un transcript live dans la console |
| **Dev 2** — Intelligence & score | Prompt Claude + sortie JSON, moteur de mots-clés, modèle de voix, Lookup + contacts, moteur de score, événements vers le dashboard | Le score réagit à un transcript texte, sans téléphone |
| **Biz 1** — Produit & démo | 3 scénarios d'arnaque + 2 appels légitimes, lexique FR, voix clonée (avec consentement), dashboard HTML avec Dev 2, script de démo minuté | Les 5 scénarios écrits et enregistrés |
| **Biz 2** — Pitch & business | Chiffres sourcés, persona, modèle économique, RGPD et consentement, deck, répétitions, vidéo de secours | Plan du deck et message clé en une phrase |

## À tester en premier (H0 → H1)

Le plus gros risque est la téléphonie. Ces vérifications passent avant tout le reste :

- [ ] **L'annonce n'est entendue que par la victime.** Sinon : coach via Agent
      Conference, ou mise en attente du scammeur avant chaque annonce.
- [x] Le rôle AWS autorise `transcribe:StartStreamTranscription` **et** Bedrock répond
      — vérifié le 10/10 avec `make check-aws` (rôle `WSParticipantRole`, `us-west-2`,
      `fr-FR` en 8 kHz). Le repli Whisper n'est donc pas nécessaire.
- [ ] Tous les téléphones de la démo sont vérifiés dans la console Twilio.
      Un compte d'essai n'appelle que des numéros vérifiés, depuis le pays
      d'inscription. Créditer ~20 $ lève ces limites.
- [ ] Le réseau du lieu ne bloque pas Bedrock (déjà vu). Hotspot dédié en secours.

## Contraintes connues

- **Numéro US obligatoire.** Un numéro français exige un regulatory bundle
  (pièce d'identité + adresse en France).
- **Python 3.12.** `audioop` disparaît en 3.13 ; on décode le μ-law avec numpy
  pour ne pas en dépendre, mais rester en 3.12 évite d'autres surprises.
- **Whisper/coach Twilio** exige l'Agent Conference activée dans la console,
  sinon erreur 16000. On tente d'abord l'annonce par participant.
- **Twilio Lookup v2** est facturé à la requête → `TWILIO_LOOKUP_ENABLED=false`
  pendant les tests.
- **Transcribe** : fr-FR en streaming, 8 kHz accepté.
- **wav2vec2** attend du 16 kHz propre → suréchantillonner 8 → 16 kHz, fenêtres de
  4 s. Plusieurs fiches de modèles reconnaissent une mauvaise généralisation ; à
  tester sur de l'audio réellement passé par le téléphone.

## Secrets

`.env` est dans `.gitignore` et n'est **jamais** commité. Partir de `.env.example`.
Les identifiants AWS du workshop expirent : les rafraîchir dans `~/.aws/credentials`
juste avant de passer devant le jury.

Aucun audio ni transcript n'est persisté par défaut (`PERSIST_AUDIO=false`,
`PERSIST_TRANSCRIPTS=false`) — c'est aussi la réponse à la question vie privée du jury.
