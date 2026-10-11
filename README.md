# Cut the Line

**They phish. We cut the line.** Real-time protection against phone scams and
AI-cloned voices, for the people scammers target most.

Cut the Line sits on the call path of a protected person (typically an elderly
parent). It listens to calls from unknown numbers, scores the risk live from four
signals, and steps in **during** the call: a warning only the callee hears, the
scammer put on hold, a text to the real relative, and a 4-digit identity check that
a voice clone cannot pass.

> **No automatic hang-up.** A false positive on a real family emergency would be
> serious. Putting the caller on hold protects just as well and leaves the decision
> to the callee.

The detailed French version of this document, with every setting and design
decision, is in [`docs/README_fr.md`](docs/README_fr.md).

---

## 1. The problem

- Phone scams target older people with the same script: a relative in trouble
  ("Grandma, it's Lucas, I had an accident"), a fake bank advisor, or a fake police
  officer. Each one pushes for an urgent payment and asks the victim to keep it secret.
- A few seconds of public audio are now enough to clone a voice. "I recognized his
  voice" is no longer proof.
- Caller ID can be spoofed, so a call can show a real relative's number.
- The victim has seconds to decide, alone, under emotional pressure. Spam filters
  act before the call, and bank fraud checks act after the payment. Nothing protects
  the conversation itself.

## 2. The solution

The protected person **keeps their own number**. They activate call forwarding
once (`**21*<Twilio number>#`). Every incoming call then goes through our server,
which routes it:

| The caller is… | What happens |
|---|---|
| **in the contact book** (family, doctor) | Passed through untouched. No notice, no audio stream, no transcription. Family calls are never listened to. |
| **unknown, hidden, or flagged `mode: "analyze"`** | The caller alone hears *"This call is being analyzed by a fraud protection service."* Only then does analysis start: transcription, four signals, live score, graduated response. |
| **one of our own numbers** | Refused, to prevent a forwarding loop. |

The protected person answers in **the Cut the Line app**, a PWA (installable web
app) built on the Twilio Voice JS SDK. The app rings, shows who is calling and the
live risk level, and carries the buttons: Answer, Decline, Mute, Hold,
**Verify their identity**. It also has Recents (with per-call risk detail),
Contacts, a Keypad for outgoing calls, and Voicemail.

**Identity check.** At any time the callee can tap *Verify their identity*. A
4-digit code is texted to the number **the contact book** holds for the claimed
relative, never to the number shown on screen. The caller must read it back.
A scammer who spoofs a number or clones a voice still doesn't have the real phone,
so the code never reaches him. The screen says: **"Never say this code. They must
read it to you."**

```
Caller ──dials──▶ protected person's real number
                     │ carrier call forwarding (**21*)
                     ▼
              Twilio ──webhook──▶ /twilio/voice/inbound
                     │                 │ decides: trusted / analyze / loop
                     │◀──── TwiML ─────┘
                     ├─ [unknown]  <Say> notice ▶ <Start><Stream> ▶ <Conference>
                     └─ [trusted]  <Conference>, no stream, no notice
                     │
                     └──calls.create(to="client:…")──▶ Cut the Line app on the phone
```

## 3. How the risk score works

| Signal | What it measures | Weight |
|---|---|---|
| Claude (content) on Amazon Bedrock | Urgency, secrecy, unusual payment, false authority, claimed identity, isolation, emotional pressure | 45 |
| Keywords | Weighted risk terms, FR and EN | 20 |
| Synthetic voice | wav2vec2 "fake" probability, smoothed over 4 s windows | 20 |
| Reputation | VoIP or hidden line, unknown number, claimed identity that doesn't match the number | 15 |

| Score | Level | Response |
|---|---|---|
| < 40 | none | nothing |
| 40–64 | caution | dashboard warning, nothing audible |
| 65–84 | alert | spoken warning to the callee only, plus a text to the trusted contact |
| ≥ 85 | intervention | caller put on hold, then a warning to the callee |

**Circuit breakers** keep a strong signal from being averaged away:

1. Unusual payment **and** a claimed identity that doesn't match the number, with
   content risk ≥ 0.65 → score ≥ 85.
2. Claude risk ≥ 0.9 → score ≥ 65.
3. Synthetic voice alone can never push the score above 64. It is a hint, never a
   verdict.

The real contact's answer also feeds the score: a **NO** to the "is that you
calling?" text raises it, and a correct code read back marks the identity as verified.

## 4. Architecture

Hexagonal (ports and adapters): the domain knows nothing about Twilio or AWS, so
every part can be tested without a phone.

```
src/vocalguard/
├── domain/              # pure business logic, no external dependency
│   ├── events.py        # the Event contract (frozen schema: contracts/event.schema.json)
│   ├── models.py        # Call, Contact, the four signals
│   ├── alerts.py        # thresholds and alert policy
│   ├── ports.py         # interfaces: Transcriber, ContentAnalyzer, CallController…
│   └── scoring/         # weighted sum + circuit breakers
├── application/         # orchestration, in the order of the flow
│   ├── sessions.py      #   audio → transcription → `transcript` events
│   ├── scoring.py       #   `transcript` → four signals → `score`
│   ├── intervention.py  #   `score` → hold, warning, text
│   └── verification.py  #   4-digit code → text to the real relative → listen
├── infrastructure/      # one adapter per external dependency
│   ├── telephony/       # Twilio: TwiML, μ-law, hold/announce, Lookup, SMS
│   ├── aws/             # Transcribe streaming, Bedrock (Claude)
│   ├── keywords/        # regex engine + lexicon
│   ├── voice/           # wav2vec2 synthetic-voice detector
│   ├── contacts/        # contact book
│   └── fakes/           # test doubles to work without a phone or AWS
└── interface/           # FastAPI
    ├── http/webhooks.py #   TwiML + trusted / analyze routing
    ├── http/control.py  #   hold, resume, contact denied, verify
    ├── http/phone_app.py#   the phone app: page, Voice token, caller info
    └── static/          #   app.html (PWA) and dashboard.html, no build step
```

**Stack:** Python 3.12, FastAPI, Twilio Programmable Voice (Media Streams,
Conference, Voice JS SDK 2.18), Amazon Transcribe streaming (8 kHz μ-law),
Claude on Amazon Bedrock, wav2vec2
([`MelodyMachine/Deepfake-audio-detection-V2`](https://huggingface.co/MelodyMachine/Deepfake-audio-detection-V2)),
SQLite for call metadata.

Every component emits and consumes a single event shape:

```json
{"call_id": "CA123", "t": 12.4, "type": "transcript | keyword | llm | voice | reputation | score | action",
 "speaker": "caller | victim", "data": {"risk": 0.82, "reasons": ["urgency", "unusual payment"]}}
```

## 5. Installation

### Prerequisites

- Python **3.12** (not 3.13) and [`uv`](https://docs.astral.sh/uv/)
- A Twilio account with a voice-capable number, an API key and a TwiML App
- AWS credentials allowed to call `transcribe:StartStreamTranscription` and Bedrock
  (Claude)
- [ngrok](https://ngrok.com/) or another HTTPS tunnel

### Run it

```bash
cp .env.example .env      # fill in Twilio, AWS and APP_PIN (6+ digits)
cp data/contacts.example.json data/contacts.json   # your trusted contacts
make install              # uv sync, without torch
make check-aws            # checks that Bedrock and Transcribe answer
make run                  # API on :8000
make tunnel               # ngrok, in another terminal
```

Then in the Twilio console, on your number:

| Event | URL |
|---|---|
| A call comes in | `POST https://<PUBLIC_HOSTNAME>/twilio/voice/inbound` |
| A message comes in | `POST https://<PUBLIC_HOSTNAME>/twilio/sms/inbound` |
| TwiML App Voice URL (outgoing calls) | `POST https://<PUBLIC_HOSTNAME>/twilio/voice/outbound` |

Open `https://<PUBLIC_HOSTNAME>/app` on the protected phone, enter the PIN, allow
the microphone and keep the page open. The live dashboard is at `/dashboard`
(`/dashboard?demo=1` plays a scripted scam without any backend).

### Optional: synthetic-voice detector

```bash
make install-voice        # adds torch + transformers (~2 GB)
# .env: VOICE_DETECTOR_ENABLED=true
make calibrate-voice HUMAN="human1.wav human2.wav" FAKE="clone1.wav clone2.wav"
# copy the two suggested values into VOICE_DETECTOR_CALIBRATION_LOW / _HIGH
```

Phone audio is 8 kHz and the model expects 16 kHz. The detector upsamples with a
band-limited filter so no fake high frequencies reach the model. It loads the model
once at startup, and calibration maps its raw output to what you measured on real
phone audio.

### Without a phone

```bash
make replay SCENARIO=scenarios/scam_01_petit_fils.json   # replay a call through the pipeline
make evaluate                     # 22 scenarios, offline stand-in model
make evaluate EVAL_MODE=bedrock   # same scenarios with the real Claude
make test                         # 1,024 tests
```

## 6. Results

On 22 scripted calls (11 scams, 11 legitimate, FR and EN), no legitimate call
raised an alert in any mode. With an offline stand-in for Claude, 3 of 11 scams
reach the alert level, including the two reference demo scams. The grandparent
scam reaches intervention 15 s into the call. Reports are in
[`artifacts/evaluation/`](artifacts/evaluation/). Run `make evaluate EVAL_MODE=bedrock`
for the numbers with the real model. The conversations are synthetic: they validate
the pipeline, not real-world accuracy.

## 7. Privacy and compliance

- The unknown caller is told the call is analyzed **before** any audio is processed
  (TwiML order `<Say>` → `<Start><Stream>` → `<Conference>`, ePrivacy art. 5).
- Trusted contacts are never streamed, transcribed or analyzed.
- No audio, transcript or verification code is stored. Call history keeps metadata
  only (risk peaks, tactics, actions), purged after 30 days. `HISTORY_ENABLED=false`
  turns it off.
- Voicemail is the one recording, made after a notice and a beep, and is deleted
  after 30 days.
- The app, history, controls and dashboard stream are PIN-protected (5 failures →
  10 min lockout). Twilio webhooks and the media stream are signature-checked.
- `.env` is git-ignored and never committed.

## 8. Known limitations

- **One US relay number.** The person forwards their line to one Twilio number. A
  local number needs a regulatory bundle, and forwarding to the US is billed to the
  protected person, often outside their plan. A carrier could run the same logic
  natively on its network.
- **Single user, single call.** The contact book is a JSON file and live state is
  in memory, in one server process. Several users or simultaneous calls need a
  database and per-user tenancy.
- **The web app rings only while it is open.** iOS suspends background pages and
  there is no VoIP push without a native app (CallKit / ConnectionService). When
  the app is closed, the call falls back to `VICTIM_PHONE_NUMBER`, then voicemail.
- **Spoofed trusted numbers pass through.** A call showing a contact's number is not
  analyzed, by design. The 4-digit code check is the defense, but the callee must
  ask for it.
- **Accuracy is not proven in the field.** Scenarios are synthetic. Voice-deepfake
  models generalize poorly to phone audio; the voice signal is capped and needs
  calibration on real recordings.
- **One language per deployment.** Transcription runs in `TRANSCRIBE_LANGUAGE_CODE`
  (en-US in this build).
- **AWS data use.** Bedrock model providers have no access to prompts and
  completions. Amazon Transcribe stores and uses processed audio to improve the
  service unless an AWS Organizations AI opt-out policy is applied. That policy
  must be in place before any real use.
- **Unproven paths:** DTMF through a one-way `<Start><Stream>` is wired but not
  verified. Caller-ID spoofing in the demo is simulated (`DEMO_SPOOF_AS`), because
  Twilio forbids presenting a number you don't own.


