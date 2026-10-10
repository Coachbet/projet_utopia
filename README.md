# Projet Utopia — Détecteur d'arnaques vocales

Garde-fou temps réel : on écoute l'appel via Twilio, on calcule un score de risque à
partir de quatre signaux, et on protège la victime **pendant** l'appel — alerte
chuchotée, mise en attente du scammeur, SMS au vrai proche.

> **Pas de raccrochage automatique.** Un faux positif sur un vrai appel d'urgence
> serait grave. La mise en attente protège autant et laisse la décision à la victime.

## Le montage : un seul numéro

La personne protégée **garde son vrai numéro**. Elle active **une fois** un renvoi
d'appel vers notre numéro Twilio (`**21*<numéro Twilio>#`). Tout appel entrant passe
alors par notre serveur, qui décide :

| L'appelant est… | Ce qui se passe |
|---|---|
| **au carnet d'adresses** (famille, médecin) | l'appel est **transmis tel quel**. Aucune annonce, aucun flux audio, aucune transcription. La famille n'est pas écoutée — c'est la promesse produit, pas un réglage |
| **inconnu, masqué, ou au carnet en `mode: "analyze"`** | annonce **à lui seul** (« cet appel est analysé par un service de protection contre la fraude »), **puis** l'analyse démarre : transcription, quatre signaux, score, alertes, mise en attente, SMS au proche |
| **un de nos propres numéros** | refusé. C'est la boucle du renvoi, et chaque tour coûterait deux jambes facturées |

L'ordre `<Say>` → `<Start><Stream>` → `<Conference>` n'est pas cosmétique : **on
n'analyse que ce qui suit l'information de l'appelant**. C'est la pièce centrale du
montage juridique (ePrivacy art. 5, secret des correspondances).

À tout moment, la personne protégée peut demander une **vérification d'identité par
code à 4 chiffres**. Le code part par SMS au numéro que **le carnet** connaît pour le
proche revendiqué — jamais au numéro affiché par l'appel. Usurper un numéro ne donne
pas la réception de ses SMS : un arnaqueur ne peut pas lire ce code.

### Les numéros, et l'app (à ne jamais confondre)

| Nom | Rôle | Réglage |
|---|---|---|
| Numéro Twilio | relais technique, que personne ne compose | `TWILIO_PHONE_NUMBER` |
| Numéro public | le vrai mobile de la personne protégée, **renvoyé** vers Twilio | `PROTECTED_PHONE_NUMBER` |
| **L'app VocalGuard** | page web installable (PWA) avec le SDK Voice JS : c'est **elle** qui sonne, affiche l'appelant et porte les boutons | `VOICE_CLIENT_IDENTITY` |
| Ligne de secours | repli si l'app n'est pas ouverte | `VICTIM_PHONE_NUMBER` |
| Vrai proche | reçoit le SMS et le code | `TRUSTED_CONTACT_PHONE_NUMBER` + `data/contacts.json` |

**Pourquoi une app et pas un rappel sur le numéro** : si Twilio rappelait le numéro
public, l'opérateur le renverrait… vers Twilio. **Boucle.** En faisant sonner l'app
(jambe `client:<identité>` au lieu d'un numéro), il n'y a plus de boucle, plus besoin de
double SIM, la minute coûte beaucoup moins qu'un appel vers un mobile français, et l'app
peut afficher **le vrai numéro de l'appelant** et un bouton « Vérifier son identité ».

```
Ami / arnaqueur ──compose──▶ n° public (vrai mobile de la personne protégée)
                          │ renvoi opérateur (**21*)
                          ▼
                   Twilio ──webhook──▶ /twilio/voice/inbound
                          │                    │ décide : confiance / analyse / boucle
                          │◀── TwiML ──────────┘
                          ├─ [inconnu]   <Say> annonce ▶ <Start><Stream> ▶ <Conference>
                          └─ [confiance] <Conference>, sans Stream ni annonce
                          │
                          └──calls.create(to="client:…")──▶ L'APP sur le téléphone
                                 « Appel de Lucas · Contact de confiance »
                                 Décrocher · Refuser · Vérifier son identité · Attente
```

> **Limite à connaître.** Une page web ne sonne **que si elle est ouverte** : iOS suspend
> les pages en arrière-plan et il n'existe pas de push VoIP sans application native. App
> fermée → repli sur `VICTIM_PHONE_NUMBER`. L'app native (CallKit iOS /
> ConnectionService Android, compte Apple Developer payant) est la roadmap.

> **Coût à connaître.** Le renvoi d'appel est facturé par l'opérateur du numéro renvoyé,
> donc **à la personne protégée**, comme un appel vers les États-Unis — souvent hors
> forfait. Vérifier le forfait avant la démo et faire des appels courts. La réponse de
> fond est un numéro français, qui exige un dossier réglementaire.

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

Les documents opérationnels :

| Document | À quoi il sert |
|---|---|
| [`docs/terrain.md`](docs/terrain.md) | les sept vérifications à faire **avec un vrai téléphone**, avant de se fier au montage |
| [`docs/script_demo.md`](docs/script_demo.md) | les trois scénarios de démo, minutés, avec leurs plans B |

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
├── application/         # orchestration, dans le sens du flux :
│   ├── sessions.py      #   Dev 1 — audio → transcription → événements `transcript`
│   ├── scoring.py       #   Dev 2 — `transcript` → quatre signaux → `score`
│   ├── intervention.py  #   Dev 1 — `score` → attente, annonce, SMS
│   └── verification.py  #   code à 4 chiffres → SMS au vrai proche → écoute
├── infrastructure/      # adapters, un par dépendance externe
│   ├── telephony/       # Twilio : TwiML, μ-law, hold/announce, Lookup, SMS
│   ├── aws/             # Transcribe streaming, Bedrock/Claude
│   ├── keywords/        # moteur regex + lexique FR
│   ├── voice/           # wav2vec2
│   ├── contacts/        # carnet d'adresses
│   └── fakes/           # doublures pour bosser sans téléphone ni AWS
└── interface/           # FastAPI
    ├── http/webhooks.py #   TwiML + routage liste blanche / analyse
    ├── http/control.py  #   boutons : attente, reprise, démenti, vérification
    ├── http/phone_app.py#   l'app sur le téléphone : page, jeton Voice, appelant
    └── static/          #   dashboard.html et app.html, sans build
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

**La seule extension autorisée depuis H0** : aucun nouveau `type`, mais six valeurs de
plus dans `data.action` pour les faits de routage et de vérification —
`trusted_passthrough`, `caller_notice`, `verify_requested`, `identity_verified`,
`verify_expired`, `spoof_simulated`. Elles sont nommées une seule fois, dans
`domain.alerts.ReportedAction`, et déclarées dans le schéma. Elles ne sont **pas** des
`ActionKind` : celles-là sont des effets que la politique d'alerte prescrit et que
Twilio applique, celles-ci rapportent ce que le routage a constaté. Les confondre fait
échouer tout test qui affirme « rien d'audible n'a été déclenché » — d'où
`InMemoryEventSink.actions()`.

## Endpoints (à régler dans la console Twilio)

Le serveur est en place. Une fois `make tunnel` lancé et `PUBLIC_HOSTNAME` renseigné,
il n'y a qu'une seule URL à coller dans la console : le webhook de réception du numéro.
Tout le reste, Twilio le rappelle tout seul à partir du TwiML qu'on lui renvoie.

| Route | Rôle |
|---|---|
| `POST /twilio/voice/inbound` | **À coller dans la console** (A call comes in). Décide du routage, branche `<Stream>` puis la conférence, et fait sonner la personne protégée. |
| `POST /twilio/sms/inbound` | **À coller dans la console** (A message comes in). Recueille le « NON » du proche. Sans elle, le signal le plus décisif du système n'arrive nulle part. |
| `wss://…/ws/media` | Audio μ-law des pistes diffusées → transcription par locuteur |
| `/twilio/voice/join/{call_id}` | TwiML de la jambe « personne protégée » |
| `/twilio/voice/announce/{call_id}` | Phrase dite à la victime seule (`announce_url`) |
| `/twilio/voice/hold-music` | Ce qu'entend le scammeur en attente |
| `/twilio/voice/status` | Fin d'appel : libère la session |
| `GET /dashboard` · `wss://…/ws/dashboard` | L'écran de démo et son flux d'événements |
| `GET /app` | **L'app sur le téléphone** (PWA). `GET /app/manifest.webmanifest` pour l'installer. |
| `POST /app/token` | Access Token Voice, derrière `APP_PIN`. L'URL ngrok est publique. |
| `POST /app/incoming` | Qui appelle, pour l'app : nom, numéro, confiance. Même PIN. |
| `POST /control/calls/{id}/verify` | **Vérification d'identité par code.** Marche aussi sur un appel de confiance. |
| `POST /control/calls/{id}/hold` · `/resume` | Surcharges humaines depuis l'app ou le dashboard |
| `POST /control/calls/{id}/contact-denied` | Le proche dément, saisi à la main : le chemin qui ne dépend pas du SMS |
| `GET /healthz` | Backend choisi, Twilio configuré ou non, appels en cours |

Deux paramètres de query sur `/twilio/voice/inbound`, réservés à la démo sortante de
`scripts/lancer_appel_demo.py` (où Twilio appelle l'arnaqueur, donc où `From` vaut notre
propre numéro) : `?demo_outbound=1` désarme la garde anti-boucle **pour cet appel seul**,
et `&presented_from=<E.164>` fournit l'identité à présenter. Sans le drapeau,
`presented_from` est ignoré — l'URL est publique.

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

## Réglages du montage « un seul numéro »

Tous dans `.env.example`, avec pour chacun la panne qu'il évite. Aucun n'a de valeur
par défaut qui expose quoi que ce soit.

| Réglage | Ce qu'il fait | Vide / par défaut |
|---|---|---|
| `PROTECTED_PHONE_NUMBER` | le numéro public renvoyé, qu'on ne rappelle jamais | la garde anti-boucle ne couvre que Twilio et le repli |
| `CALLER_NOTICE_TEXT` | la phrase dite à l'appelant inconnu | une phrase par défaut qui nomme l'analyse |
| `VOICE_CLIENT_IDENTITY` | l'identité du client Voice JS : l'app sonne | repli téléphonique sur `VICTIM_PHONE_NUMBER` |
| `TWILIO_API_KEY_SID` · `_SECRET` · `TWILIO_TWIML_APP_SID` | signent l'Access Token Voice | `/app/token` refuse, en nommant le réglage manquant |
| `APP_PIN` | protège `/app/token` et `/app/incoming` | **la route refuse tout** plutôt que de distribuer des jetons |
| `VERIFY_CODE_TTL_SECONDS` | fenêtre du code à 4 chiffres | 120 s (mettre ~45 s pour une démo de 90 s) |
| `TRUSTED_VICTIM_ONLY_ANALYSIS` | sur un appel de confiance, n'écouter que la voix de l'abonnée | `false` — **à valider juridiquement** avant toute activation |
| `SCAMMER_PHONE_NUMBER` · `DEMO_SPOOF_AS` | usurpation **simulée** pour la démo | vide, et inerte si l'un des deux manque. À revider après la démo |

## À tester en premier (H0 → H1)

Le plus gros risque est la téléphonie. Le protocole complet, à dérouler avec un vrai
téléphone, est dans **[`docs/terrain.md`](docs/terrain.md)** — sept vérifications, avec
pour chacune la manipulation exacte et la décision si le résultat est mauvais. La plus
décisive est la §3 : **le `From` d'un appel renvoyé est-il bien le numéro de
l'appelant ?** Toute la liste blanche en dépend.

Ces vérifications passent avant tout le reste :

- [ ] **L'annonce n'est entendue que par la victime.** Sinon : coach via Agent
      Conference, ou mise en attente du scammeur avant chaque annonce.
- [ ] **Le `From` d'un appel renvoyé est le numéro de l'appelant** (`docs/terrain.md` §3).
      Si c'est le numéro de la personne protégée, ou vide, le routage est à repenser.
- [ ] **L'app sonne sur le vrai téléphone** de l'utilisateur, il décroche, il entend son
      ami (`docs/terrain.md` §6). C'est le test qui valide le montage de bout en bout.
- [ ] **Le DTMF remonte-t-il** à travers un `<Start><Stream>` unidirectionnel
      (`docs/terrain.md` §5) ? Non bloquant : le déclencheur de vérification sur lequel
      on compte est le bouton de l'app.
- [x] Le rôle AWS autorise `transcribe:StartStreamTranscription` **et** Bedrock répond
      — vérifié le 10/10 avec `make check-aws` (rôle `WSParticipantRole`, `us-west-2`,
      `fr-FR` en 8 kHz). Le repli Whisper n'est donc pas nécessaire.
- [ ] Tous les téléphones de la démo sont vérifiés dans la console Twilio.
      Un compte d'essai n'appelle que des numéros vérifiés, depuis le pays
      d'inscription. Créditer ~20 $ lève ces limites.
- [ ] Le réseau du lieu ne bloque pas Bedrock (déjà vu). Hotspot dédié en secours.

## Contraintes connues

- **Numéro US obligatoire.** Un numéro français exige un regulatory bundle
  (pièce d'identité + adresse en France). C'est aussi ce qui rend le renvoi
  d'appel coûteux pour la personne protégée : elle paie un appel vers les
  États-Unis, souvent hors forfait.
- **L'app ne sonne que si la page est ouverte.** iOS suspend les pages en
  arrière-plan, et il n'y a pas de push VoIP sans application native. Le repli
  est `VICTIM_PHONE_NUMBER`.
- **Paramètres personnalisés vers un client Voice JS :** la doc Twilio n'en
  documente que pour la noun `<Dial><Client><Parameter>`, que notre jambe
  n'emprunte pas (`calls.create(to="client:…")`), et l'API REST n'en documente
  aucun pour cette forme. L'app lit donc l'appelant sur `POST /app/incoming` :
  le serveur est seul à détenir le carnet et le registre.
- **`<Start><Stream>` est unidirectionnel.** Rien ne garantit que Twilio y
  pousse les événements DTMF : le déclencheur « appuyez sur 1 » de la
  vérification est câblé mais **non éprouvé** (`docs/terrain.md` §5).
- **Twilio interdit d'usurper un numéro qu'on ne possède pas**, et c'est une
  bonne chose. Le scénario de démo « Lucas appelle depuis un numéro usurpé »
  simule donc la seule chose qui compte pour la démonstration — ce que notre
  serveur croit du `From` — via `DEMO_SPOOF_AS`, inerte sans
  `SCAMMER_PHONE_NUMBER` et logué en MAJUSCULES à chaque usage.
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
