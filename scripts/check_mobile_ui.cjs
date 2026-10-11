/* QA reproductible : navigateur réel, SDK Twilio et réseau entièrement simulés.
 * NODE_PATH=/tmp/vocalguard-qa/node_modules node scripts/check_mobile_ui.cjs
 * npm install --prefix /tmp/vocalguard-qa playwright@1.56.1
 * Chrome doit être installé (ou PLAYWRIGHT_CHANNEL=chromium avec son navigateur).
 */
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("playwright");

const root = path.resolve(__dirname, "..");
const output = path.join(root, "docs/captures");
const PIN = "432198";
const start = "2026-10-10T17:42:00+00:00";
const example = {
  call_id: "CA_qa_analyzed",
  started_at: start,
  ended_at: "2026-10-10T17:45:12+00:00",
  caller_number: "+33700000000",
  caller_name: "",
  relation: "",
  origin: "inconnu",
  country: "FR",
  line_type: "mobile",
  route: "analyzed",
  duration_s: 192,
  answered: true,
  max_score: 89,
  max_level: "intervene",
  watch_at_s: 30,
  alert_at_s: 68,
  intervene_at_s: 104,
  peak_llm: 0.94,
  peak_keywords: 0.82,
  peak_voice: null,
  peak_reputation: 0.46,
  tactics: ["urgence", "paiement inhabituel", "demande de code SMS"],
  explanation:
    "L'appelant invoque une urgence bancaire et réclame un code personnel.",
  actions: [
    "caller_notice",
    "announce_victim",
    "hold_caller",
    "sms_contact",
    "verify_requested",
  ],
  verification: "expired",
};
let records = [
  example,
  {
    ...example,
    call_id: "CA_qa_contact",
    caller_number: "+33600000000",
    caller_name: "Lucas Martin",
    relation: "Petit-fils",
    origin: "contact",
    route: "trusted_passthrough",
    duration_s: 76,
    max_score: null,
    max_level: null,
    verification: "verified",
    actions: ["identity_verified"],
  },
  {
    ...example,
    call_id: "CA_qa_missed",
    caller_number: "",
    origin: "masqué",
    country: null,
    line_type: "unknown",
    answered: false,
    duration_s: 25,
    max_score: null,
    max_level: null,
  },
  {
    ...example,
    call_id: "CA_qa_foreign",
    caller_number: "+442000000000",
    origin: "étranger",
    country: "GB",
    max_level: "watch",
    max_score: 48,
    duration_s: 118,
  },
];

const contacts = [
  {
    name: "Lucas Martin",
    number: "+33600000000",
    relation: "Petit-fils",
    mode: "trusted",
    favorite: true,
  },
  {
    name: "Sophie Martin",
    number: "+33600000001",
    relation: "Fille",
    mode: "trusted",
    favorite: true,
  },
  {
    name: "Conseiller bancaire",
    number: "+33100000000",
    relation: "Banque",
    mode: "analyze",
  },
];
let messages = [
  {
    id: "VM_qa_lucas",
    call_id: "CA_vm_lucas",
    caller_number: "+33600000000",
    caller_name: "Lucas Martin",
    created_at: start,
    duration_s: 23,
    listened: false,
  },
  {
    id: "VM_qa_unknown",
    call_id: "CA_vm_unknown",
    caller_number: "+33700000000",
    caller_name: "",
    created_at: start,
    duration_s: 12,
    listened: true,
  },
];
const voicemailRequests = [];
function wav(seconds) {
  const rate = 8000,
    frames = rate * seconds,
    output = Buffer.alloc(44 + frames * 2);
  output.write("RIFF");
  output.writeUInt32LE(output.length - 8, 4);
  output.write("WAVE", 8);
  output.write("fmt ", 12);
  output.writeUInt32LE(16, 16);
  output.writeUInt16LE(1, 20);
  output.writeUInt16LE(1, 22);
  output.writeUInt32LE(rate, 24);
  output.writeUInt32LE(rate * 2, 28);
  output.writeUInt16LE(2, 32);
  output.writeUInt16LE(16, 34);
  output.write("data", 36);
  output.writeUInt32LE(frames * 2, 40);
  return output; // WAV PCM décodable, silence de QA uniquement, aucun message personnel.
}

function mocks() {
  class Emitter {
    constructor() {
      this.events = {};
    }
    on(name, fn) {
      (this.events[name] ||= []).push(fn);
    }
    emit(name, data) {
      for (const fn of this.events[name] || []) fn(data);
    }
  }
  class Device extends Emitter {
    constructor() {
      super();
      window.qaDevice = this;
      const outputs = new Map([
        ["default", { deviceId: "default", label: "Téléphone" }],
        ["speaker", { deviceId: "speaker", label: "Haut-parleur" }],
      ]);
      this.audio = {
        incoming() {},
        disconnect() {},
        on() {},
        availableOutputDevices: outputs,
        isOutputSelectionSupported: true,
        speakerDevices: {
          get: () => new Set([outputs.get("default")]),
          set: async (id) => {
            window.qaOutput = id;
          },
        },
      };
    }
    async register() {
      this.emit("registered");
    }
    async connect(options) {
      assertNonce(options);
      window.qaOutgoingParams = options.params;
      const call = qaNewCall();
      call.parameters = { CallSid: "CA_qa_outgoing" };
      call.status = () => (call.accepted ? "open" : "pending");
      setTimeout(() => {
        call.accepted = true;
        call.emit("accept");
      }, 60);
      this.outgoing = call;
      return call;
    }
    disconnectAll() {
      this.outgoing?.disconnect();
    }
    updateToken() {}
    destroy() {}
  }
  function assertNonce(options) {
    if (Object.keys(options.params).join(",") !== "Authorization")
      throw new Error("Le SDK ne doit recevoir que le nonce autorisé.");
  }
  window.Twilio = { Device };
  window.qaCreatedURLs = [];
  window.qaRevokedURLs = [];
  const createURL = URL.createObjectURL.bind(URL),
    revokeURL = URL.revokeObjectURL.bind(URL);
  URL.createObjectURL = (blob) => {
    const url = createURL(blob);
    qaCreatedURLs.push(url);
    return url;
  };
  URL.revokeObjectURL = (url) => {
    qaRevokedURLs.push(url);
    return revokeURL(url);
  };
  window.qaNewCall = () => {
    const call = new Emitter();
    call.parameters = { CallSid: "CA_qa_client" };
    call.customParameters = new Map();
    call.accept = () => call.emit("accept");
    call.reject = () => call.emit("reject");
    call.disconnect = () => call.emit("disconnect");
    call.mute = (value) => {
      call.muted = value;
    };
    call.sendDigits = (value) => {
      (call.digits ||= []).push(value);
    };
    return call;
  };
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] }) },
  });
  Object.defineProperty(navigator, "vibrate", {
    configurable: true,
    value: () => true,
  });
  Object.defineProperty(navigator, "wakeLock", {
    configurable: true,
    value: {
      request: async () => ({
        released: false,
        addEventListener() {},
        release: async () => {},
      }),
    },
  });
  if (!HTMLMediaElement.prototype.setSinkId)
    HTMLMediaElement.prototype.setSinkId = async () => {};
  window.WebSocket = class {
    constructor(url) {
      this.url = url;
      window.qaWs = this;
      setTimeout(() => this.onopen?.(), 0);
    }
    close() {}
  };
}

(async () => {
  await fs.mkdir(output, { recursive: true });
  const browser = await chromium.launch({
    channel: process.env.PLAYWRIGHT_CHANNEL || "chrome",
    headless: true,
  });
  try {
    const context = await browser.newContext({
      viewport: { width: 390, height: 844 },
      deviceScaleFactor: 1,
      isMobile: true,
      hasTouch: true,
      locale: "fr-FR",
      timezoneId: "Europe/Paris",
      reducedMotion: "reduce",
    });
    const page = await context.newPage();
    const failures = [];
    const controls = [];
    page.on("pageerror", (error) => failures.push(error.message));
    await page.addInitScript(mocks);
    let html = await fs.readFile(
      path.join(root, "src/vocalguard/interface/static/app.html"),
      "utf8",
    );
    // Le SDK est remplacé par la doublure ci-dessus ; aucune connexion Twilio.
    html = html.replace(
      /<script\b[^>]*\bsrc="https:[^>]*>[\s\S]*?<\/script>/g,
      "",
    );
    await page.route("**/*", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.hostname !== "vocalguard.test") return route.abort();
      if (url.pathname === "/app")
        return route.fulfill({ contentType: "text/html", body: html });
      if (url.pathname === "/app/icon.svg")
        return route.fulfill({
          contentType: "image/svg+xml",
          body: await fs.readFile(
            path.join(root, "src/vocalguard/interface/static/icon.svg"),
            "utf8",
          ),
        });
      if (url.pathname.endsWith("webmanifest"))
        return route.fulfill({
          json: { name: "VocalGuard", start_url: "/app" },
        });
      const body = request.postDataJSON() || {};
      if (url.pathname.startsWith("/control/")) {
        assert.equal(request.headers()["x-app-pin"], PIN);
        controls.push(url.pathname);
        if (url.pathname.endsWith("/verify"))
          return route.fulfill({
            json: {
              ok: true,
              code: "5729",
              expires_in: 120,
              sent_to: "Lucas Martin",
            },
          });
        return route.fulfill({
          json: { ok: true, caller_held: url.pathname.endsWith("/hold") },
        });
      }
      assert.equal(body.pin, PIN);
      if (url.pathname === "/app/token")
        return route.fulfill({
          json: { token: "jwt-factice", identity: "jeanne", owner: "Jeanne" },
        });
      if (url.pathname === "/app/incoming")
        return route.fulfill({
          json: {
            call_id: example.call_id,
            caller_number: example.caller_number,
            caller_name: "",
            trusted: false,
            ambiguous: false,
            country: "FR",
            origin: "inconnu",
            line_type: "mobile",
          },
        });
      if (url.pathname === "/app/contacts")
        return route.fulfill({ json: { items: contacts } });
      if (url.pathname === "/app/contacts/favorite") {
        const cible = contacts.find(
          (contact) => contact.number === body.number,
        );
        if (cible) cible.favorite = Boolean(body.favorite);
        return route.fulfill({ json: { items: contacts } });
      }
      if (url.pathname === "/app/outgoing")
        return route.fulfill({
          json: {
            authorization: "opaque-qa-nonce",
            number: body.number,
            caller_name:
              contacts.find((contact) => contact.number === body.number)
                ?.name || "",
            direction: "outgoing",
            analyzed: false,
            expires_in: 60,
          },
        });
      if (url.pathname === "/app/voicemail")
        return route.fulfill({ json: { items: messages, enabled: true } });
      if (url.pathname.startsWith("/app/voicemail/")) {
        const [, , , id, operation] = url.pathname.split("/");
        voicemailRequests.push({ id, operation, pin: body.pin });
        const item = messages.find((message) => message.id === id);
        if (!item)
          return route.fulfill({
            status: 404,
            json: { detail: "Message absent" },
          });
        if (operation === "audio")
          return route.fulfill({
            contentType: "audio/wav",
            body: wav(item.duration_s),
            headers: { "cache-control": "no-store" },
          });
        if (operation === "heard") {
          item.listened = true;
          return route.fulfill({ json: { ok: true } });
        }
        if (operation === "delete") {
          messages = messages.filter((message) => message.id !== id);
          return route.fulfill({ json: { ok: true } });
        }
      }
      if (url.pathname === "/app/history")
        return route.fulfill({
          json: {
            items: records,
            total: records.length,
            offset: 0,
            limit: 50,
            has_more: false,
            enabled: true,
          },
        });
      if (url.pathname === "/app/history/clear") {
        records = [];
        return route.fulfill({ json: { ok: true, deleted: 4 } });
      }
      if (url.pathname.startsWith("/app/history/"))
        return route.fulfill({ json: example });
      return route.fulfill({
        status: 404,
        json: { detail: "Route de test inconnue" },
      });
    });
    /* Chaque capture porte son sujet dans son nom de fichier, et sa légende
       part dans index.md : un dossier de PNG numérotés sans texte ne se relit
       pas trois jours plus tard, et encore moins par quelqu'un d'autre. */
    const captures = [];
    const shot = async (name, legende, fullPage = false) => {
      captures.push({ name, legende });
      await page.screenshot({
        path: path.join(output, name + ".png"),
        fullPage,
      });
    };
    await page.goto("http://vocalguard.test/app");
    await shot(
      "01-activation-saisie-du-pin",
      "Entrée de l'app : le PIN ouvre la session et signe ensuite chaque appel au serveur.",
    );
    await page.locator("#pin").fill(PIN);
    await page.locator("#btn-activer").click();
    await page.locator("#ecran-attente").waitFor({ state: "visible" });
    await page.locator(".history-row").first().waitFor();
    await shot(
      "02-recents-appels-recus",
      "Onglet Récents : les derniers appels, leur provenance, et ceux qui ont été manqués.",
    );
    assert.equal(await page.locator("#shell-titre").textContent(), "Recents");
    assert.equal(
      await page.locator('#telephone-onglets [role="tab"]').count(),
      5,
    );
    await page.locator("#filtre-manques").click();
    assert.equal(await page.locator(".history-row").count(), 1);
    await page.locator("#filtre-tous").click();
    assert.equal(await page.locator(".history-row").count(), 4);
    await page.locator("#tab-favoris").click();
    await page.locator("#favoris-liste .contact-main").first().waitFor();
    assert.equal(
      await page.locator("#favoris-liste .contact-main").count(),
      2,
    );
    await shot(
      "03-favoris-proches-epingles",
      "Onglet Favoris : les proches épinglés, appelables sans rien taper — l'écran de l'urgence.",
    );
    await page.locator("#btn-reglages").click();
    await page.locator("#reglages").waitFor({ state: "visible" });
    await shot(
      "04-reglages-etat-de-la-protection",
      "Feuille Réglages : l'état de la protection et les réglages, à un seul endroit.",
    );
    await page.locator("#btn-fermer-reglages").click();
    await page.locator("#reglages").waitFor({ state: "hidden" });
    await page.locator("#tab-contacts").click();
    await page.locator("#contacts-liste .contact-main").first().waitFor();
    assert.equal(
      await page.locator("#contacts-liste .contact-main").count(),
      3,
    );
    await page.locator("#contacts-recherche").fill("lucas");
    assert.equal(
      await page.locator("#contacts-liste .contact-main").count(),
      1,
    );
    await page.locator("#contacts-recherche").fill("");
    await shot(
      "05-contacts-carnet-d-adresses",
      "Onglet Contacts : le carnet, et pour chacun « contact de confiance » ou « appels analysés ».",
    );
    await page
      .locator("#contacts-liste .contact-main")
      .filter({ hasText: "Lucas Martin" })
      .click();
    assert.equal(
      await page.locator("#numerotation-numero").inputValue(),
      "+33600000000",
    );
    await page.locator('#numerotation-touches button[aria-label="5"]').click();
    await page.locator("#btn-effacer-chiffre").click();
    assert.equal(
      await page.locator("#numerotation-numero").inputValue(),
      "+33600000000",
    );
    // Le pavé doit tenir dans l'écran : un scroll ici, et le bouton d'appel
    // passe sous le pli sur les téléphones courts.
    const debordement = await page.evaluate(() => {
      const racine = document.documentElement;
      return racine.scrollHeight - racine.clientHeight;
    });
    assert.ok(
      debordement <= 1,
      `L'écran Clavier déborde de ${debordement}px : il scrolle.`,
    );
    await shot(
      "06-clavier-numero-compose",
      "Onglet Clavier : le pavé tient dans l'écran, sans scroll, et rien ne s'écrit sous le bouton d'appel.",
    );
    await page.locator("#btn-appeler").click();
    await page.waitForFunction(() => etat.sortant && etat.decroche);
    assert.deepEqual(await page.evaluate(() => qaOutgoingParams), {
      Authorization: "opaque-qa-nonce",
    });
    assert.equal(await page.locator("#appel-risque").isVisible(), false);
    assert.equal(await page.locator("#btn-verifier").isVisible(), false);
    assert.equal(await page.locator("#btn-attente").isVisible(), false);
    await shot(
      "07-appel-sortant-non-analyse",
      "Appel sortant en cours : ni analysé ni enregistré — aucun score, pas de bouton Vérifier ni Attente.",
    );
    await page.locator("#btn-raccrocher").click();
    await page.locator("#ecran-attente").waitFor({ state: "visible" });
    await page.locator("#tab-messagerie").click();
    await page.locator("#messagerie-liste .voicemail-card").first().waitFor();
    assert.equal(await page.locator("#messagerie-liste .voicemail-card").count(), 2);
    await shot(
      "08-messagerie-deux-messages",
      "Onglet Messagerie : les messages déposés après le bip, avec leur état lu / non lu.",
    );
    await page.locator(".voicemail-listen").first().click();
    await page.waitForFunction(
      () => etat.voicemail.audio && etat.voicemail.audio.readyState >= 2,
    );
    await page.waitForFunction(() => etat.voicemail.items[0].listened);
    assert.match(
      await page.locator(".voicemail-player audio").first().getAttribute("src"),
      /^blob:/,
    );
    await shot(
      "09-messagerie-ecoute-d-un-message",
      "Écoute d'un message : l'audio arrive par une route protégée par le PIN, sans URL Twilio dans le navigateur.",
    );
    const activeURL = await page.evaluate(() => etat.voicemail.url);
    await page.locator("#tab-contacts").click();
    assert.equal(await page.evaluate(() => etat.voicemail.url), null);
    assert.ok((await page.evaluate(() => qaRevokedURLs)).includes(activeURL));
    await page.locator("#tab-messagerie").click();
    await page.locator("#messagerie-liste .voicemail-card").first().waitFor();
    await page.locator(".voicemail-delete").first().click();
    await shot(
      "10-messagerie-suppression-a-confirmer",
      "Supprimer un message demande une confirmation : l'audio distant part avec la ligne.",
    );
    await page.locator("#btn-message-conserver").click();
    assert.equal(await page.locator("#messagerie-liste .voicemail-card").count(), 2);
    await page.locator(".voicemail-delete").first().click();
    await page.locator("#btn-message-supprimer").click();
    await page.waitForFunction(() => etat.voicemail.items.length === 1);
    await page.locator(".voicemail-delete").first().click();
    await page.locator("#btn-message-supprimer").click();
    await page.locator("#messagerie-vide").waitFor({ state: "visible" });
    await shot(
      "11-messagerie-aucun-message",
      "Messagerie vide : la boîte se lit comme vide, pas comme un chargement qui n'en finit pas.",
    );
    assert.ok(
      voicemailRequests.some((request) => request.operation === "heard"),
    );
    assert.equal(
      await page.evaluate(() => qaCreatedURLs.length === qaRevokedURLs.length),
      true,
    );
    for (const width of [360, 390, 480, 1024]) {
      await page.setViewportSize({ width, height: 844 });
      for (const tab of [
        "btn-historique",
        "tab-contacts",
        "tab-clavier",
        "tab-messagerie",
      ]) {
        await page.locator("#" + tab).click();
        assert.equal(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
          true,
          "Débordement horizontal " + width + " " + tab,
        );
        if (tab === "tab-clavier")
          assert.equal(
            await page.evaluate(
              () =>
                document.getElementById("btn-appeler").getBoundingClientRect()
                  .bottom <=
                document
                  .getElementById("telephone-onglets")
                  .getBoundingClientRect().top,
            ),
            true,
            "Bouton appel couvert par onglets " + width,
          );
      }
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator("#btn-historique").click();
    await page.evaluate(() => {
      window.qaCall = qaNewCall();
      qaDevice.emit("incoming", qaCall);
    });
    await page.waitForFunction(
      () => document.getElementById("appel-nom").textContent === "+33700000000",
    );
    await shot(
      "12-appel-entrant-numero-inconnu",
      "Appel entrant d'un numéro inconnu : le vrai numéro de l'appelant, sa provenance, et le bouton Vérifier.",
    );
    await page.locator("#btn-decrocher").click();
    await page.locator("#appel-boutons-encours").waitFor({ state: "visible" });
    await page.evaluate(() => {
      etat.debut = Date.now() - 42000;
      appliquerEvenement({
        call_id: "CA_qa_analyzed",
        type: "score",
        data: { score: 48, level: "watch" },
      });
      rendreAttente();
    });
    await shot(
      "13-appel-en-cours-vigilance-score-48",
      "Appel analysé en cours, score 48 : vigilance. Le dashboard s'oriente, mais rien n'est encore audible.",
    );
    await page.locator("#btn-muet").click();
    assert.equal(await page.evaluate(() => qaCall.muted), true);
    await page.locator("#btn-muet").click();
    assert.equal(await page.evaluate(() => qaCall.muted), false);
    await page.locator("#btn-clavier").click();
    await shot(
      "14-appel-clavier-dtmf",
      "Clavier pendant l'appel : les touches partent en DTMF dans la conversation (serveurs vocaux).",
    );
    await page.locator('#clavier-touches button[aria-label="5"]').click();
    assert.deepEqual(await page.evaluate(() => qaCall.digits), ["5"]);
    await page.locator("#btn-fermer-clavier").click();
    await page.locator("#btn-attente").click();
    await page.waitForFunction(() => etat.enAttente);
    await shot(
      "15-appel-scammeur-en-attente",
      "Mise en attente de l'appelant : il entend une musique, la personne protégée garde la main. Jamais de raccrochage automatique.",
    );
    await page.locator("#btn-attente").click();
    await page.waitForFunction(() => !etat.enAttente);
    await page.locator("#btn-haut-parleur").click();
    await page
      .getByRole("button", { name: "Haut-parleur", exact: true })
      .click();
    assert.equal(await page.evaluate(() => qaOutput), "speaker");
    await page.evaluate(() =>
      appliquerEvenement({
        call_id: "CA_qa_analyzed",
        type: "score",
        data: { score: 72, level: "alert" },
      }),
    );
    await page.locator("#appel-alerte").waitFor({ state: "visible" });
    await shot(
      "16-appel-alerte-arnaque-score-72",
      "Score 72 : alerte. Annonce à la personne protégée seule, et SMS au vrai proche.",
    );
    await page.locator("#btn-fermer-alerte").click();
    await page.locator("#btn-verifier").click();
    await page.locator("#appel-code").waitFor({ state: "visible" });
    await shot(
      "17-verification-code-a-4-chiffres",
      "Vérification d'identité : le code part par SMS au numéro que le carnet connaît, jamais à celui qui appelle. « Ne dites jamais ce code. »",
    );
    assert.match(
      await page.locator("#appel-code").textContent(),
      /Never say this code/,
    );
    await page.evaluate(() =>
      appliquerEvenement({
        call_id: "CA_qa_analyzed",
        type: "action",
        data: { action: "identity_verified", ok: false },
      }),
    );
    await page.locator("#appel-code").waitFor({ state: "hidden" });
    assert.match(
      await page.locator("#appel-message").textContent(),
      /failed or the code is wrong/,
    );
    await shot(
      "18-verification-echouee",
      "Le code n'a pas été lu : deux essais au maximum, et l'app le dit sans détour.",
    );
    await page.locator("#btn-raccrocher").click();
    await page.locator("#ecran-fin").waitFor({ state: "visible" });
    await shot(
      "19-appel-termine",
      "Fin d'appel : ce qui a été constaté pendant la conversation, une fois raccroché.",
    );
    await page.locator("#ecran-attente").waitFor({ state: "visible" });
    await page.locator("#btn-historique").click();
    await page.locator(".history-row").first().waitFor();
    assert.equal(await page.locator(".history-row").count(), 4);
    await shot(
      "20-historique-liste-des-appels",
      "Historique : les appels conservés 30 jours, sans audio ni transcript en base.",
    );
    await page.locator(".history-row").first().click();
    await page.locator(".timeline-marker").first().waitFor();
    assert.equal(await page.locator(".timeline-marker").count(), 3);
    assert.equal(await page.locator(".signal").count(), 4);
    await shot(
      "21-historique-detail-d-un-appel-analyse",
      "Détail d'un appel analysé, page entière : les 3 seuils franchis, les 4 signaux, les tactiques et la vérification.",
      true,
    );
    await page.locator("#btn-detail-retour").click();
    await page.locator("#btn-effacer-historique").click();
    await shot(
      "22-historique-effacement-a-confirmer",
      "Effacer l'historique demande une confirmation, et couvre aussi l'appel en cours.",
    );
    await page.locator("#btn-annuler-effacement").click();
    assert.equal(await page.locator(".history-row").count(), 4);
    await page.locator("#btn-effacer-historique").click();
    await page.locator("#btn-confirmer-effacement").click();
    await page.locator("#historique-vide").waitFor({ state: "visible" });
    assert.equal(await page.locator(".history-row").count(), 0);
    await shot(
      "23-historique-aucun-appel",
      "Historique vide après effacement : plus aucune métadonnée d'appel en base.",
    );
    assert.ok(controls.some((route) => route.endsWith("/hold")));
    assert.ok(controls.some((route) => route.endsWith("/resume")));
    assert.equal(
      await page.evaluate(() => localStorage.length + sessionStorage.length),
      0,
    );
    assert.deepEqual(failures, []);
    await fs.writeFile(
      path.join(output, "result.json"),
      JSON.stringify(
        {
          viewport: "390×844",
          mode: "SDK Voice, API et appels simulés",
          checks: [
            "activation",
            "appel entrant",
            "acceptation",
            "chronomètre",
            "muet",
            "DTMF",
            "sortie audio",
            "attente/reprise avec PIN",
            "alerte",
            "code et échec",
            "fin",
            "historique",
            "3 seuils et 4 signaux",
            "effacement avec confirmation",
            "absence stockage PIN",
          ],
          screenshots: captures.length,
          passed: true,
          additionalChecks: [
            "5 onglets téléphone",
            "filtres récents/manqués",
            "contacts et recherche",
            "clavier sans scroll",
            "appel sortant nonce SDK",
            "sortants sans analyse",
            "lecture WAV privé avec PIN",
            "message écouté",
            "révocation URL blob",
            "suppression message confirmée",
            "responsive360/390/480/1024",
          ],
        },
        null,
        2,
      ) + "\n",
    );
    await fs.writeFile(
      path.join(output, "index.md"),
      [
        "# Captures de l'app Cut the Line",
        "",
        "Produites par `make test-ui` (`scripts/check_mobile_ui.cjs`) : navigateur réel",
        "en 390×844, SDK Voice Twilio et API entièrement simulés — aucun appel, aucun",
        "numéro et aucun identifiant réel n'y figure. Les captures suivent le parcours,",
        "de l'activation à l'historique effacé.",
        "",
        ...captures.map(
          (capture) =>
            `## ${capture.name}\n\n![${capture.legende}](${capture.name}.png)\n\n${capture.legende}\n`,
        ),
      ].join("\n") + "\n",
    );
    console.log(
      `QA mobile : appels, 5 onglets, messagerie WAV privée, responsive et ${captures.length} captures validés (SDK/API simulés).`,
    );
    console.log("Captures et légendes : docs/captures/ (voir index.md)");
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
