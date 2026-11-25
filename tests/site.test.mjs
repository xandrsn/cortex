// node tests/site.test.mjs — the web demo and the card layouts, without a browser. Launched by tests/test_site.py.
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { createRequire } from "node:module";
import { classify, swipe, ACTIONS, DEFAULTS, GESTURES } from "../site/sense/sense.mjs";
const { createDemo, freshState, parseAmount, wmo } = createRequire(import.meta.url)("../site/demo/mock.js");

const box = { window: {} };
vm.runInNewContext(fs.readFileSync(new URL("../site/demo/defaults.js", import.meta.url), "utf8"), box);
const defaults = box.window.CORTEX_DEFAULTS;
assert.equal(defaults.data_dir, "");

// ---------- the demo ----------
let now = Date.UTC(2026, 0, 5, 12), sent = [];
const demo = createDemo({ state: freshState(now), defaults, emit: m => sent.push(m), clock: () => now,
  weather: async p => ({ ok: true, place: p.name, text: "Despejado", temp: 20 }) });
assert.equal(parseAmount("temporizador de 5 minutos"), 300);
assert.equal(parseAmount("media hora"), 1800);
assert.equal(parseAmount("un rato"), 0);
assert.equal(wmo(61).icon, "rain");
assert.equal(wmo(1234).icon, "cloud");

assert.match(await demo.ask("Pon un temporizador de 5 minutos"), /5 minutes/);
assert.match(await demo.ask("set a timer for 2 minutes"), /2 minutes/);
demo.S.timers.pop();
assert.equal(demo.S.timers.length, 1);
assert.deepEqual(demo.due(), []);
now += 301e3;
assert.equal(demo.due().length, 1);
assert.equal(demo.S.timers.length, 0);

await demo.ask("añade pan a la lista");
assert.ok(demo.S.compra.some(i => i.text === "pan"));
await demo.ask("apunta la tarea llamar al dentista");
assert.equal(demo.S.tasks[0].tarea, "llamar al dentista");
assert.match(await demo.ask("recuérdame en 2 minutos beber agua"), /beber agua/);
assert.match(await demo.ask("recuérdame dentro de media hora que saque la ropa"), /: saque la ropa/);
now += 121e3;
assert.deepEqual(demo.due(), ["Reminder: beber agua."]);
assert.match(await demo.ask("¿qué tiempo hace?"), /20 degrees/);
await demo.ask("modo oscuro");
assert.equal(demo.settings().theme, "dark");
assert.match(await demo.ask("escribe un poema"), /does not use Claude/);

let r = await demo.api("POST", "/api/tasks", "", { action: "add", tarea: "x", prioridad: "rara" });
assert.equal(r.data.tasks[0].prioridad, "media");
r = await demo.api("POST", "/api/tasks", "", { action: "done", i: r.data.tasks[0].i });
assert.ok(!r.data.tasks.some(t => t.tarea === "x"));
assert.equal((await demo.api("POST", "/api/avisos", "", { texto: "tarde", cuando: "2020-01-01 10:00" })).error, "Pick a time in the future.");
await demo.api("POST", "/api/settings", "", { accent: "#2563eb", inventada: 1 });
assert.equal(demo.settings().accent, "#2563eb");
assert.ok(!("inventada" in demo.settings()));
// nothing that looks like an account is accepted
for (const path of ["/api/mail/login", "/api/mail/send", "/api/setup/claude", "/api/setup/spotify", "/api/setup/whatsapp", "/api/web/accounts"]) {
  r = await demo.api("POST", path, "", { user: "a@example.com", password: "secreta" });
  assert.equal(r.ok, false, path);
}
assert.ok(!JSON.stringify(demo.S).includes("secreta"));
// WhatsApp: only writes if chosen
const wa = demo.S.chats[0].jid;
assert.equal((await demo.api("POST", "/api/whatsapp/send", "", { jid: wa, text: "hola" })).ok, false);
await demo.api("POST", "/api/settings", "", { whatsapp_mode: "write" });
assert.equal((await demo.api("POST", "/api/whatsapp/send", "", { jid: wa, text: "hola" })).ok, true);
assert.equal((await demo.api("GET", "/api/whatsapp")).chats[0].mine, true);
// every panel GET has a response
for (const path of fs.readFileSync(new URL("../web/hub/app.js", import.meta.url), "utf8").matchAll(/fetch\(["`](\/api\/[a-z/]+)/g)) {
  const p = path[1].replace(/\/$/, "/x");
  const got = await Promise.all(["GET", "POST"].map(m => demo.api(m, p)));
  assert.ok(got.every(g => g !== undefined), p);
}

// ---------- layout of the chosen cards (web/hub/app.js) ----------
const app = fs.readFileSync(new URL("../web/hub/app.js", import.meta.url), "utf8");
const cut = (from, to) => app.slice(app.indexOf(from), app.indexOf(to, app.indexOf(from)));
const ctx = { structuredClone };
vm.runInNewContext(cut("const DEFAULT_LAYOUT", "const GESTURES") + cut("function layoutFor", "async function loadLayout") + "this.out = { layoutFor, DEFAULT_LAYOUT };", ctx);
const { layoutFor, DEFAULT_LAYOUT } = ctx.out;
const ids = Array.from(DEFAULT_LAYOUT, w => w.id);
for (let n = 1; n <= ids.length; n++) {
  const on = layoutFor(ids.slice(0, n)).filter(w => !w.hidden);
  assert.equal(on.length, n);
  const cells = new Set();
  for (const w of on) for (let x = w.x; x < w.x + w.w; x++) for (let y = w.y; y < w.y + w.h; y++) cells.add(`${x},${y}`);
  assert.equal(cells.size, 144, `${n} cards fill the board without overlapping`);
  assert.ok(on.every(w => Number.isInteger(w.x) && Number.isInteger(w.w) && w.x + w.w <= 12 && w.y + w.h <= 12));
}
assert.deepEqual(layoutFor(["nada"]), layoutFor(undefined));

// ---------- Sense demo gestures ----------
// fingers point up (y down in the image); knuckles 5/9/13/17 at y .6, wrist at .9
const hand = (up, thumb = { x: 0.45, y: 0.62 }) => {
  const lm = Array.from({ length: 21 }, () => ({ x: 0.5, y: 0.9 }));
  [[8, 6, 5, .4], [12, 10, 9, .5], [16, 14, 13, .6], [20, 18, 17, .7]].forEach(([tip, pip, mcp, x], i) => {
    lm[mcp] = { x, y: 0.6 }; lm[pip] = { x, y: 0.5 }; lm[tip] = { x, y: up[i] ? 0.2 : 0.75 };
  });
  lm[3] = { x: 0.35, y: 0.65 };
  lm[4] = thumb === "index" ? { ...lm[8] } : thumb;
  return lm;
};
assert.equal(classify(hand([1, 1, 1, 1])), "palm");
assert.equal(classify(hand([0, 0, 0, 0])), "fist");
assert.equal(classify(hand([0, 0, 0, 0], "index")), "fist");
assert.equal(classify(hand([1, 1, 0, 0])), "peace");
assert.equal(classify(hand([1, 1, 1, 0])), "three");
assert.equal(classify(hand([1, 0, 0, 0])), "point");
assert.equal(classify(hand([1, 0, 0, 1])), "rock");
assert.equal(classify(hand([1, 0, 0, 1], { x: 0.1, y: 0.6 })), "love");
assert.equal(classify(hand([0, 0, 0, 1], { x: 0.1, y: 0.6 })), "shaka");
assert.equal(classify(hand([1, 1, 1, 1], "index")), "ok");
assert.equal(classify(hand([0, 0, 0, 0], { x: 0.35, y: 0.3 })), "thumb_up");
assert.equal(classify(hand([0, 0, 0, 0], { x: 0.35, y: 1.0 })), "thumb_down");
const path = (dx, dy, ms = 300) => [0, 1, 2, 3].map((k) => ({ t: k * ms / 3, x: 0.5 + dx * k / 3, y: 0.5 + dy * k / 3 }));
assert.equal(swipe(path(-0.3, 0)), "swipe_right", "the image is not mirrored: moving left in it is the user's right");
assert.equal(swipe(path(0.3, 0.05)), "swipe_left");
assert.equal(swipe(path(0, -0.3)), "swipe_up");
assert.equal(swipe(path(0.1, 0.1)), "");
assert.equal(swipe(path(0.3, 0, 2000)), "", "too slow is not a swipe");
assert.equal(swipe([]), "");
assert.ok(Object.entries(DEFAULTS).every(([g, a]) => g in GESTURES && a in ACTIONS), "every default is a known gesture and action");
console.log("ok");
