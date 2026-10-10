/* Tests de concurrence UI, données et SDK simulés. Voir RECETTE_MVP.md. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { chromium } = require("playwright");
const html = fs.readFileSync(
  "src/vocalguard/interface/static/app.html",
  "utf8",
);
const requests = [];
let pendingContext = null,
  pendingVerify = null,
  pendingHold = null,
  liveBrowser = null;
let pendingAudio = null,
  pendingContacts = null,
  pendingOutgoing = null;
let voiced = [
  {
    id: "VM_races",
    call_id: "CA_vm_races",
    caller_number: "+33600000000",
    caller_name: "Lucas",
    created_at: "2026-10-10T17:42:00Z",
    duration_s: 30,
    listened: false,
  },
];
const heard = [];
function wav(seconds) {
  const rate = 8000,
    frames = rate * seconds,
    b = Buffer.alloc(44 + frames * 2);
  b.write("RIFF");
  b.writeUInt32LE(b.length - 8, 4);
  b.write("WAVE", 8);
  b.write("fmt ", 12);
  b.writeUInt32LE(16, 16);
  b.writeUInt16LE(1, 20);
  b.writeUInt16LE(1, 22);
  b.writeUInt32LE(rate, 24);
  b.writeUInt32LE(rate * 2, 28);
  b.writeUInt16LE(2, 32);
  b.writeUInt16LE(16, 34);
  b.write("data", 36);
  b.writeUInt32LE(frames * 2, 40);
  return b;
}
function deferred() {
  let resolve;
  const promise = new Promise((r) => (resolve = r));
  return { promise, resolve };
}
(async () => {
  const browser = await chromium.launch({
    channel: process.env.PLAYWRIGHT_CHANNEL || "chrome",
    headless: true,
  });
  liveBrowser = browser;
  const page = await browser.newPage({
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 1,
  });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.addInitScript(() => {
    class Emitter {
      constructor() {
        this.handlers = {};
      }
      on(name, fn) {
        (this.handlers[name] ??= []).push(fn);
      }
      emit(name, value) {
        for (const fn of this.handlers[name] || []) fn(value);
      }
    }
    class Device extends Emitter {
      constructor() {
        super();
        window.mockDevice = this;
        this.audio = { incoming() {}, disconnect() {} };
      }
      async connect(params) {
        window.connectCount = (window.connectCount || 0) + 1;
        window.connectParams = params;
        const c = newCall("out", "Appel sortant");
        setTimeout(() => c.emit("accept"), 60);
        return c;
      }
      disconnectAll() {}
      register() {
        this.emit("registered");
        return Promise.resolve();
      }
      destroy() {}
      updateToken() {}
    }
    window.Twilio = { Device };
    window.createdURLs = [];
    window.revokedURLs = [];
    const create = URL.createObjectURL.bind(URL),
      revoke = URL.revokeObjectURL.bind(URL);
    URL.createObjectURL = (blob) => {
      const url = create(blob);
      createdURLs.push(url);
      return url;
    };
    URL.revokeObjectURL = (url) => {
      revokedURLs.push(url);
      return revoke(url);
    };
    const play = HTMLMediaElement.prototype.play;
    HTMLMediaElement.prototype.play = function () {
      if (window.blockAutoPlay)
        return Promise.reject(
          new DOMException("Lecture requiert interaction", "NotAllowedError"),
        );
      return play.call(this);
    };
    window.WebSocket = class {
      constructor(url) {
        this.url = url;
        window.lastSocket = this;
        setTimeout(() => this.onopen?.({}), 0);
      }
      close() {}
    };
    Object.defineProperty(navigator, "mediaDevices", {
      value: {
        getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] }),
      },
    });
    window.newCall = (id, name = "", number = "+33755555555", exact = true) => {
      const c = new Emitter();
      c.parameters = { CallSid: "leg_" + id };
      c.customParameters = new Map(
        exact
          ? [
              ["call_id", id],
              ["caller_name", name],
              ["caller_number", number],
              ["trusted", "false"],
            ]
          : [],
      );
      c.accept = () => c.emit("accept");
      c.reject = () => c.emit("reject");
      c.disconnect = () => c.emit("disconnect");
      c.mute = (v) => (c.muted = v);
      c.sendDigits = (v) => (c.digits ??= []).push(v);
      return c;
    };
  });
  await page.route("**/*", async (route) => {
    const req = route.request(),
      url = new URL(req.url());
    if (url.hostname === "cdn.jsdelivr.net") return route.abort();
    if (url.pathname === "/app")
      return route.fulfill({ contentType: "text/html", body: html });
    if (req.method() === "POST") {
      const data = req.postDataJSON();
      requests.push({ url: url.pathname, headers: req.headers(), data });
      let body = {};
      if (url.pathname === "/app/token")
        body = { token: "mock", owner: "Jeanne" };
      else if (url.pathname === "/app/incoming") {
        if (pendingContext) {
          const hold = pendingContext;
          pendingContext = null;
          body = await hold.promise;
        } else body = { call_id: null };
      } else if (url.pathname.endsWith("/verify")) {
        if (pendingVerify) {
          const hold = pendingVerify;
          pendingVerify = null;
          body = await hold.promise;
        } else
          body = { ok: true, code: "5729", expires_in: 120, sent_to: "Lucas" };
      } else if (url.pathname.endsWith("/hold")) {
        if (pendingHold) {
          const hold = pendingHold;
          pendingHold = null;
          body = await hold.promise;
        } else body = { ok: true, caller_held: true };
      } else if (url.pathname === "/app/contacts") {
        if (pendingContacts) {
          const wait = pendingContacts;
          pendingContacts = null;
          body = await wait.promise;
        } else body = { items: [] };
      } else if (url.pathname === "/app/outgoing") {
        if (pendingOutgoing) {
          const wait = pendingOutgoing;
          pendingOutgoing = null;
          body = await wait.promise;
        } else
          body = {
            authorization: "race-nonce",
            number: data.number,
            caller_name: "Lucas",
            expires_in: 60,
          };
      } else if (url.pathname === "/app/voicemail")
        body = { items: voiced, enabled: true };
      else if (url.pathname.endsWith("/audio")) {
        let clip = wav(30);
        if (pendingAudio) {
          const wait = pendingAudio;
          pendingAudio = null;
          clip = await wait.promise;
        }
        return route.fulfill({ contentType: "audio/wav", body: clip });
      } else if (url.pathname.endsWith("/heard")) {
        heard.push(url.pathname);
        voiced[0].listened = true;
        body = { ok: true };
      } else if (url.pathname === "/app/history")
        body = { items: [], total: 0, has_more: false, enabled: true };
      return route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(body),
      });
    }
    return route.fulfill({ status: 204, body: "" });
  });
  await page.goto("http://mobile.test/app");
  await page.fill("#pin", "123456");
  await page.click("#btn-activer");
  await page.waitForSelector("#ecran-attente:not([hidden])");
  assert.match(await page.evaluate(() => window.lastSocket.url), /pin=123456/);
  // XSS and SDK actual accepting, muted/DTMF controls.
  await page.evaluate(() => {
    window.callA = newCall("A", '<img src=x onerror="window.pwned=true">');
    surAppelEntrant(callA);
  });
  assert.equal(
    await page.locator("#appel-nom").textContent(),
    '<img src=x onerror="window.pwned=true">',
  );
  assert.equal(await page.evaluate(() => window.pwned), undefined);
  await page.click("#btn-decrocher");
  assert.equal(await page.evaluate(() => etat.decroche), true);
  await page.click("#btn-muet");
  assert.equal(await page.evaluate(() => callA.muted), true);
  await page.click("#btn-clavier");
  await page.getByRole("button", { name: "5", exact: true }).click();
  assert.deepEqual(await page.evaluate(() => callA.digits), ["5"]);
  await page.keyboard.press("Escape");
  await page.click("#btn-attente");
  await page.waitForFunction(() => etat.enAttente);
  assert.equal(await page.evaluate(() => etat.enAttente), true);
  assert.equal(
    requests.find((r) => r.url === "/control/calls/A/hold").headers[
      "x-app-pin"
    ],
    "123456",
  );
  await page.click("#btn-verifier");
  await page.waitForSelector("#appel-code:not([hidden])");
  assert.equal(
    await page.evaluate(() => document.querySelector("main").inert),
    true,
  );
  await page.evaluate(() =>
    appliquerEvenement({
      call_id: "A",
      type: "action",
      data: { action: "identity_verified", ok: false },
    }),
  );
  assert.equal(await page.locator("#appel-code").isVisible(), false);
  assert.match(await page.locator("#appel-message").textContent(), /échoué/);
  await page.evaluate(() => afficherCode("5729", 120, "Lucas"));
  await page.evaluate(() =>
    appliquerEvenement({
      call_id: "A",
      type: "action",
      data: { action: "verify_expired", ok: false },
    }),
  );
  assert.equal(await page.locator("#appel-code").isVisible(), false);
  // Old context response never identifies the next call.
  await page.evaluate(() => terminerAppel());
  const oldContext = deferred();
  pendingContext = oldContext;
  await page.evaluate(() => {
    window.old = newCall("old", "", "+33100000000", false);
    surAppelEntrant(old);
  });
  await page.waitForTimeout(30);
  await page.evaluate(() => {
    terminerAppel();
    window.current = newCall("new", "Nouvel appel");
    surAppelEntrant(current);
  });
  oldContext.resolve({
    call_id: "old",
    caller_name: "Ancien appel",
    trusted: true,
  });
  await page.waitForTimeout(100);
  assert.equal(await page.locator("#appel-nom").textContent(), "Nouvel appel");
  assert.equal(await page.evaluate(() => etat.callId), "new");
  await page.evaluate(() => old.emit("disconnect"));
  assert.equal(await page.evaluate(() => etat.appel === current), true);
  // Old verify response cannot expose its code in a replacement call.
  await page.click("#btn-decrocher");
  const oldVerify = deferred();
  pendingVerify = oldVerify;
  await page.click("#btn-verifier");
  await page.evaluate(() => {
    terminerAppel();
    window.next = newCall("next", "Dernier appel");
    surAppelEntrant(next);
  });
  oldVerify.resolve({
    ok: true,
    code: "8123",
    expires_in: 120,
    sent_to: "Ancien proche",
  });
  await page.waitForTimeout(100);
  assert.equal(await page.locator("#appel-code").isVisible(), false);
  assert.equal(await page.evaluate(() => etat.callId), "next");
  // Old hold response cannot put the replacement call on hold.
  await page.click("#btn-decrocher");
  const oldHold = deferred();
  pendingHold = oldHold;
  await page.click("#btn-attente");
  await page.evaluate(() => {
    terminerAppel();
    window.last = newCall("last", "Dernière ligne");
    surAppelEntrant(last);
  });
  oldHold.resolve({ ok: true, caller_held: true });
  await page.waitForTimeout(100);
  assert.equal(await page.evaluate(() => etat.enAttente), false);
  // Risk alert can be dismissed and stays dismissed for the same level.
  await page.click("#btn-decrocher");
  await page.evaluate(() =>
    appliquerEvenement({
      call_id: "last",
      type: "score",
      data: { score: 70, level: "alert" },
    }),
  );
  await page.keyboard.press("Tab");
  assert.equal(
    await page.evaluate(() => document.activeElement.id),
    "btn-fermer-alerte",
  );
  await page.keyboard.press("Tab");
  assert.equal(
    await page.evaluate(() => document.activeElement.id),
    "btn-fermer-alerte",
  );
  await page.click("#btn-fermer-alerte");
  await page.evaluate(() =>
    appliquerEvenement({
      call_id: "last",
      type: "score",
      data: { score: 72, level: "alert" },
    }),
  );
  assert.equal(await page.locator("#appel-alerte").isVisible(), false);
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
    true,
  );
  const dimensions = await page.evaluate(() => ({
    height: document.documentElement.scrollHeight,
    viewport: innerHeight,
  }));
  assert.ok(
    dimensions.height <= dimensions.viewport + 4,
    `active-call overflow ${JSON.stringify(dimensions)}`,
  );
  // Contact trust has no invented risk badge, even if unrelated scores arrive.
  await page.evaluate(() => {
    terminerAppel();
    window.trusted = newCall("trust", "Lucas");
    trusted.customParameters.set("trusted", "true");
    surAppelEntrant(trusted);
    appliquerEvenement({
      call_id: "trust",
      type: "score",
      data: { score: 99, level: "intervene" },
    });
  });
  assert.equal(await page.locator("#appel-risque").isVisible(), false);

  // A terminal verdict beats a late response in the same call.
  await page.evaluate(() => {
    terminerAppel();
    window.same = newCall("same", "Même appel");
    surAppelEntrant(same);
  });
  await page.click("#btn-decrocher");
  const terminalVerify = deferred();
  pendingVerify = terminalVerify;
  await page.click("#btn-verifier");
  await page.evaluate(() =>
    appliquerEvenement({
      call_id: "same",
      type: "action",
      data: { action: "identity_verified", ok: true },
    }),
  );
  terminalVerify.resolve({
    ok: true,
    code: "9471",
    expires_in: 120,
    sent_to: "Lucas",
  });
  await page.waitForTimeout(100);
  assert.equal(await page.locator("#appel-code").isVisible(), false);
  assert.equal(await page.locator("#btn-verifier").isDisabled(), true);
  // A newer bus state beats a late HTTP hold result in the same call.
  const sameHold = deferred();
  pendingHold = sameHold;
  await page.click("#btn-attente");
  await page.evaluate(() => {
    appliquerEvenement({
      call_id: "same",
      type: "action",
      data: { action: "hold_caller", ok: true },
    });
  });
  sameHold.resolve({ ok: false, caller_held: false });
  await page.waitForTimeout(100);
  assert.equal(await page.evaluate(() => etat.enAttente), true);

  // A late contacts response cannot replace another tab or introduce stale names.
  await page.evaluate(async () => {
    terminerAppel();
    await ouvrirOnglet("recents");
  });
  const contactsDeferred = deferred();
  pendingContacts = contactsDeferred;
  await page.click("#tab-contacts");
  await page.click("#tab-clavier");
  contactsDeferred.resolve({
    items: [{ name: "Ancien carnet", number: "+33100000000", mode: "trusted" }],
  });
  await page.waitForTimeout(80);
  assert.equal(await page.evaluate(() => etat.onglet), "clavier");
  assert.equal(await page.evaluate(() => etat.contacts.items.length), 0);
  // No audio URL is created from a response delivered after leaving voicemail.
  await page.click("#tab-messagerie");
  await page.locator(".voicemail-listen").waitFor();
  const audioDeferred = deferred();
  pendingAudio = audioDeferred;
  await page.click(".voicemail-listen");
  await page.click("#tab-clavier");
  const createdBefore = await page.evaluate(() => createdURLs.length);
  audioDeferred.resolve(wav(30));
  await page.waitForTimeout(80);
  assert.equal(await page.evaluate(() => etat.voicemail.url), null);
  assert.equal(await page.evaluate(() => createdURLs.length), createdBefore);
  // Download and blocked autoplay do not mark a message heard. Actual play does.
  await page.evaluate(() => (window.blockAutoPlay = true));
  await page.click("#tab-messagerie");
  await page.locator(".voicemail-listen").waitFor();
  await page.click(".voicemail-listen");
  await page.waitForFunction(
    () => etat.voicemail.audio && etat.voicemail.audio.readyState >= 2,
  );
  assert.equal(heard.length, 0);
  assert.equal(
    await page.evaluate(() => etat.voicemail.items[0].listened),
    false,
  );
  await page.evaluate(async () => {
    window.blockAutoPlay = false;
    await etat.voicemail.audio.play();
  });
  await page.waitForFunction(() => etat.voicemail.items[0].listened);
  assert.equal(heard.length, 1);
  const playingURL = await page.evaluate(() => etat.voicemail.url);
  await page.evaluate(() => {
    window.audioCaller = newCall("audioCaller", "Nouvel appel");
    surAppelEntrant(audioCaller);
  });
  assert.equal(await page.evaluate(() => etat.voicemail.url), null);
  assert.ok((await page.evaluate(() => revokedURLs)).includes(playingURL));
  // Authorization delivered after an incoming call must never start an outgoing call.
  await page.evaluate(async () => {
    terminerAppel();
    await preparerNumero("+33600000000", "Lucas");
  });
  const outgoingDeferred = deferred();
  pendingOutgoing = outgoingDeferred;
  const connectsBefore = await page.evaluate(() => window.connectCount || 0);
  await page.click("#btn-appeler");
  await page.evaluate(() => {
    window.interruptCaller = newCall("interrupt", "Appel prioritaire");
    surAppelEntrant(interruptCaller);
  });
  outgoingDeferred.resolve({
    authorization: "old-race-nonce",
    number: "+33600000000",
    expires_in: 60,
  });
  await page.waitForTimeout(80);
  assert.equal(
    await page.evaluate(() => window.connectCount || 0),
    connectsBefore,
  );
  assert.equal(await page.evaluate(() => etat.callId), "interrupt");
  // Cancel an SDK connect in flight: its late Call is disconnected, never installed.
  await page.evaluate(async () => {
    terminerAppel();
    await preparerNumero("+33600000000", "Lucas");
    mockDevice.connect = (params) => {
      window.connectParams = params;
      return new Promise((resolve) => (window.resolvePendingConnect = resolve));
    };
  });
  await page.click("#btn-appeler");
  await page.waitForFunction(() => etat.sortantPending);
  assert.deepEqual(await page.evaluate(() => connectParams.params), {
    Authorization: "race-nonce",
  });
  await page.click("#btn-raccrocher");
  assert.equal(await page.locator("#ecran-fin").isVisible(), true);
  await page.evaluate(() => {
    const call = newCall("cancelled", "Appel annulé");
    call.disconnect = () => {
      window.cancelledDisconnects = (window.cancelledDisconnects || 0) + 1;
    };
    resolvePendingConnect(call);
  });
  await page.waitForFunction(() => window.cancelledDisconnects === 1);
  assert.equal(await page.evaluate(() => etat.appel), null);
  assert.equal(await page.locator("#ecran-appel").isVisible(), false);
  assert.equal(
    await page.evaluate(() => createdURLs.length === revokedURLs.length),
    true,
  );
  assert.equal(errors.length, 0, errors.join("\n"));
  await browser.close();
  console.log(
    "PASS: XSS/PIN, race context/SDK/verify/hold/contacts/voicemail/outgoing, WAV play-only heard, blob cleanup, keyboard focus and trusted/outgoing privacy",
  );
})().catch(async (e) => {
  console.error(e);
  if (liveBrowser) await liveBrowser.close();
  process.exitCode = 1;
});
