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
const output = path.join(root, "docs/qa/appel-historique");
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
  },
  {
    name: "Sophie Martin",
    number: "+33600000001",
    relation: "Fille",
    mode: "trusted",
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
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: {
      writeText: async (value) => {
        window.qaCopiedNumber = value;
      },
    },
  });
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
    const shot = async (name, fullPage = false) =>
      page.screenshot({ path: path.join(output, name + ".png"), fullPage });
    await page.goto("http://vocalguard.test/app");
    await shot("00-activation");
    await page.locator("#pin").fill(PIN);
    await page.locator("#btn-activer").click();
    await page.locator("#ecran-attente").waitFor({ state: "visible" });
    await page.locator(".history-row").first().waitFor();
    await shot("01-protection");
    assert.match(await page.locator("#titre-attente").textContent(), /Jeanne/);
    assert.equal(
      await page.locator('#telephone-onglets [role="tab"]').count(),
      4,
    );
    await page.locator("#filtre-manques").click();
    assert.equal(await page.locator(".history-row").count(), 1);
    await page.locator("#filtre-tous").click();
    assert.equal(await page.locator(".history-row").count(), 4);
    await page.locator("#tab-contacts").click();
    await page.locator(".contact-main").first().waitFor();
    assert.equal(await page.locator(".contact-main").count(), 3);
    await page.locator("#contacts-recherche").fill("lucas");
    assert.equal(await page.locator(".contact-main").count(), 1);
    await page.locator("#contacts-recherche").fill("");
    await shot("10-contacts");
    await page
      .locator(".contact-main")
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
    await page.locator("#btn-copier-numero").click();
    assert.equal(await page.evaluate(() => qaCopiedNumber), "+33600000000");
    await shot("11-clavier");
    await page.locator("#btn-appeler").click();
    await page.waitForFunction(() => etat.sortant && etat.decroche);
    assert.deepEqual(await page.evaluate(() => qaOutgoingParams), {
      Authorization: "opaque-qa-nonce",
    });
    assert.equal(await page.locator("#appel-risque").isVisible(), false);
    assert.equal(await page.locator("#btn-verifier").isVisible(), false);
    assert.equal(await page.locator("#btn-attente").isVisible(), false);
    await shot("12-sortant");
    await page.locator("#btn-raccrocher").click();
    await page.locator("#ecran-attente").waitFor({ state: "visible" });
    await page.locator("#tab-messagerie").click();
    await page.locator(".voicemail-card").first().waitFor();
    assert.equal(await page.locator(".voicemail-card").count(), 2);
    await shot("13-messagerie");
    await page.locator(".voicemail-listen").first().click();
    await page.waitForFunction(
      () => etat.voicemail.audio && etat.voicemail.audio.readyState >= 2,
    );
    await page.waitForFunction(() => etat.voicemail.items[0].listened);
    assert.match(
      await page.locator(".voicemail-player audio").first().getAttribute("src"),
      /^blob:/,
    );
    await shot("14-message-lecture");
    const activeURL = await page.evaluate(() => etat.voicemail.url);
    await page.locator("#tab-contacts").click();
    assert.equal(await page.evaluate(() => etat.voicemail.url), null);
    assert.ok((await page.evaluate(() => qaRevokedURLs)).includes(activeURL));
    await page.locator("#tab-messagerie").click();
    await page.locator(".voicemail-card").first().waitFor();
    await page.locator(".voicemail-delete").first().click();
    await shot("15-message-suppression");
    await page.locator("#btn-message-conserver").click();
    assert.equal(await page.locator(".voicemail-card").count(), 2);
    await page.locator(".voicemail-delete").first().click();
    await page.locator("#btn-message-supprimer").click();
    await page.waitForFunction(() => etat.voicemail.items.length === 1);
    await page.locator(".voicemail-delete").first().click();
    await page.locator("#btn-message-supprimer").click();
    await page.locator("#messagerie-vide").waitFor({ state: "visible" });
    await shot("16-messagerie-vide");
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
    await shot("02-entrant");
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
    await shot("03-en-cours");
    await page.locator("#btn-muet").click();
    assert.equal(await page.evaluate(() => qaCall.muted), true);
    await page.locator("#btn-muet").click();
    assert.equal(await page.evaluate(() => qaCall.muted), false);
    await page.locator("#btn-clavier").click();
    await page.locator('#clavier-touches button[aria-label="5"]').click();
    assert.deepEqual(await page.evaluate(() => qaCall.digits), ["5"]);
    await page.locator("#btn-fermer-clavier").click();
    await page.locator("#btn-attente").click();
    await page.waitForFunction(() => etat.enAttente);
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
    await shot("04-alerte");
    await page.locator("#btn-fermer-alerte").click();
    await page.locator("#btn-verifier").click();
    await page.locator("#appel-code").waitFor({ state: "visible" });
    await shot("05-code");
    assert.match(
      await page.locator("#appel-code").textContent(),
      /Ne dites jamais ce code/,
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
      /pas été confirmée|échou|pu être confirmée/,
    );
    await page.locator("#btn-raccrocher").click();
    await page.locator("#ecran-fin").waitFor({ state: "visible" });
    await shot("06-fin");
    await page.locator("#ecran-attente").waitFor({ state: "visible" });
    await page.locator("#btn-historique").click();
    await page.locator(".history-row").first().waitFor();
    assert.equal(await page.locator(".history-row").count(), 4);
    await shot("07-historique");
    await page.locator(".history-row").first().click();
    await page.locator(".timeline-marker").first().waitFor();
    assert.equal(await page.locator(".timeline-marker").count(), 3);
    assert.equal(await page.locator(".signal").count(), 4);
    await shot("08-detail", true);
    await page.locator("#btn-detail-retour").click();
    await page.locator("#btn-effacer-historique").click();
    await shot("09-effacement");
    await page.locator("#btn-annuler-effacement").click();
    assert.equal(await page.locator(".history-row").count(), 4);
    await page.locator("#btn-effacer-historique").click();
    await page.locator("#btn-confirmer-effacement").click();
    await page.locator("#historique-vide").waitFor({ state: "visible" });
    assert.equal(await page.locator(".history-row").count(), 0);
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
          screenshots: 17,
          passed: true,
          additionalChecks: [
            "4 onglets téléphone",
            "filtres récents/manqués",
            "contacts et recherche",
            "copie numéro",
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
    console.log(
      "QA mobile : appels, 4 onglets, messagerie WAV privée, responsive et 17 captures validés (SDK/API simulés).",
    );
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
