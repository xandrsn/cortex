"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const STATE_TEXT = {
  idle: "Say \"Cortex\" or press",
  listening: "Listening…",
  transcribing: "One moment…",
  thinking: "Thinking…",
  speaking: "Speaking",
};

const WIDGETS = {
  avisos: "Reminders", today: "Today", tasks: "Tasks", betis: "Betis",
  gym: "Gym", meals: "Meals", money: "Money", gestures: "Gestures", status: "Status",
  search: "Videos", timers: "Timers", compra: "Shopping", devices: "Devices", mail: "Mail",
  whatsapp: "WhatsApp",
};
// icon and colour of each card (the colour tints the icon bubble and the background)
const LOOK = {
  today: ["📅", "#3b82f6"], tasks: ["✅", "#8b5cf6"], avisos: ["🔔", "#ec4899"],
  betis: ["⚽", "#0b8f55"], gym: ["💪", "#f97316"], meals: ["🍽️", "#ef4444"], money: ["💶", "#14b8a6"],
  gestures: ["👋", "#a855f7"], status: ["🩺", "#64748b"],
  search: ["🎬", "#dc2626"], timers: ["⏱️", "#eab308"], compra: ["🛒", "#84cc16"], devices: ["🔊", "#6366f1"],
  mail: ["✉️", "#0284c7"], whatsapp: ["💬", "#16a34a"],
};

const ROWS = 12;   // the dashboard is a fixed 12 × 12 board that always fits the screen

const DEFAULT_LAYOUT = [
  { id: "today", x: 0, y: 0, w: 3, h: 6 },
  { id: "tasks", x: 3, y: 0, w: 4, h: 6 },
  { id: "betis", x: 7, y: 0, w: 5, h: 3 },
  { id: "money", x: 7, y: 3, w: 3, h: 3 },
  { id: "avisos", x: 10, y: 3, w: 2, h: 3 },
  { id: "gym", x: 0, y: 6, w: 3, h: 3 },
  { id: "meals", x: 3, y: 6, w: 4, h: 3 },
  { id: "search", x: 7, y: 6, w: 5, h: 3 },
  { id: "devices", x: 0, y: 9, w: 2, h: 3 },
  { id: "compra", x: 2, y: 9, w: 2, h: 3 },
  { id: "mail", x: 4, y: 9, w: 4, h: 3 },
  { id: "whatsapp", x: 8, y: 9, w: 4, h: 3 },
  { id: "timers", x: 0, y: 12, w: 2, h: 3, hidden: true },
  { id: "gestures", x: 0, y: 12, w: 4, h: 3, hidden: true },
  { id: "status", x: 0, y: 12, w: 4, h: 3, hidden: true },
];

const GESTURES = {
  swipe_left: "Swipe left", swipe_right: "Swipe right",
  swipe_up: "Swipe up", swipe_down: "Swipe down",
  open_palm: "Open palm", closed_fist: "Fist", thumb_up: "Thumb up", thumb_down: "Thumb down",
  victory: "Victory (two fingers)", pointing_up: "Index finger up", i_love_you: "Horns with thumb",
  ok: "OK (thumb and index in a circle)", shaka: "Shaka (thumb and pinky)", rock: "Horns (index and pinky)",
  three: "Three fingers", shush: "Shush (index on lips)",
  flick_right: "Index flick right", flick_left: "Index flick left",
  circle_cw: "Finger circle (clockwise)", circle_ccw: "Finger circle (counter-clockwise)",
  wag: "Finger saying \"no\"", wave: "Wave", push: "Push toward the camera",
  both_palms: "Two open hands", both_thumbs_up: "Two thumbs up", both_thumbs_down: "Two thumbs down",
  both_fists: "Two fists", hands_apart: "Pull hands apart", hands_together: "Bring hands together",
};
const ACTIONS = {
  none: "Nothing", next: "Next track", previous: "Previous track", toggle: "Pause / resume",
  volume_up: "Volume up", volume_down: "Volume down", listen: "Listen (like \"Cortex\")",
  stop: "Cut off the assistant (stops everything)", quiet: "Silence the assistant (it keeps working)", ambient: "Idle mode", wake_toggle: "Pause/enable \"Cortex\"",
  new_chat: "New conversation", theme: "Toggle light/dark",
};
const ACCENTS = ["#1db954", "#2563eb", "#7c3aed", "#db2777", "#ea580c", "#0891b2", "#111827"];

let ws;
let grid;
let layout = [];
let streamingEl = null;
let settings = {};
let quitting = false;
let lastActivity = Date.now();
let vision = {};

// ---------- dashboard and widgets ----------
/** Spread the chosen cards over the 12 × 12 board; with none valid, the default layout. */
function layoutFor(ids) {
  const items = structuredClone(DEFAULT_LAYOUT);
  const on = items.filter(w => Array.isArray(ids) && ids.includes(w.id));
  if (!on.length) return items;
  const rows = Math.ceil(on.length / 4);   // up to 16 cards: 4 rows of 4
  for (const w of items) w.hidden = !on.includes(w);
  for (let y = 0, i = 0; y < rows; y++) {
    const n = Math.ceil((on.length - i) / (rows - y));
    for (let x = 0; x < n; x++) Object.assign(on[i++], { x: x * 12 / n, y: y * 12 / rows, w: 12 / n, h: 12 / rows });
  }
  return items;
}

async function loadLayout() {
  let saved = [];
  try { saved = await (await fetch("/api/layout")).json(); } catch {}
  if (!Array.isArray(saved) || !saved.length) {
    let chosen = [];   // the cards chosen in the installer
    try { chosen = (await (await fetch("/api/settings")).json()).settings.setup_widgets; } catch {}
    return layoutFor(chosen);
  }
  const known = new Set(saved.map(w => w.id));
  for (const def of DEFAULT_LAYOUT) if (!known.has(def.id)) saved.push({ ...def, hidden: true });
  // missing sizes (old layouts) come from the defaults
  // "hidden" is always the saved value: otherwise cards hidden by default would be hidden again on reload
  return saved.filter(w => WIDGETS[w.id]).map(w => ({ ...DEFAULT_LAYOUT.find(d => d.id === w.id), ...w,
    h: w.h || DEFAULT_LAYOUT.find(d => d.id === w.id)?.h || 2, hidden: !!w.hidden }));
}

function makeItem(item) {
  const tpl = document.getElementById(`tpl-${item.id}`);
  const el = document.createElement("div");
  el.className = "grid-stack-item";
  el.setAttribute("gs-id", item.id);
  for (const k of ["x", "y", "w", "h"]) el.setAttribute(`gs-${k}`, item[k]);
  el.setAttribute("gs-min-w", 2);
  el.setAttribute("gs-min-h", 2);
  const content = document.createElement("div");
  content.className = "grid-stack-item-content";
  content.append(tpl.content.cloneNode(true));
  const [icon, hue] = LOOK[item.id] || ["•", "#64748b"];
  const card = content.querySelector(".card");
  card.dataset.w = item.id;
  card.style.setProperty("--hue", hue);
  const h2 = card.querySelector(".card-head h2");
  if (h2) h2.insertAdjacentHTML("afterbegin", `<span class="bubble">${icon}</span>`);
  el.append(content);
  return el;
}

/** Top music and floating conversation: mounted once, outside the board. */
function mountFixed() {
  if (!$("#topMusic").children.length) $("#topMusic").append(document.getElementById("tpl-spotify").content.cloneNode(true));
  if (!$("#chatPop").children.length) $("#chatPop").append(document.getElementById("tpl-chat").content.cloneNode(true));
}

// the conversation only shows while the mouse is over the mic (or itself) and goes away when it leaves
let chatTimer = null;
function openChat() {
  clearTimeout(chatTimer);
  $("#chatPop").classList.add("open");
  scrollChat();
}
function maybeCloseChat() {
  const pop = $("#chatPop");
  if (pop.matches(":hover") || $("#voice").matches(":hover") || pop.contains(document.activeElement)) return;
  pop.classList.remove("open");
}
function bindChatPop() {
  const pop = $("#chatPop");
  for (const el of [pop, $("#voice")]) {
    el.addEventListener("mouseenter", openChat);
    el.addEventListener("mouseleave", () => { clearTimeout(chatTimer); chatTimer = setTimeout(maybeCloseChat, 450); });
  }
  pop.addEventListener("focusout", () => { chatTimer = setTimeout(maybeCloseChat, 450); });
}

// ---------- card opened full size: the others shrink to the right ----------
const INTERACTIVE = "button, a, input, select, textarea, label, form, [data-dev], [data-video], [data-i], [data-min], [data-v], [data-task], .icon-btn";
let focused = null;   // { item, card }
function setFocus(item) {
  if (focused) {
    focused.item.querySelector(".grid-stack-item-content").append(focused.card);
    focused.item.classList.remove("focused");
    focused = null;
  }
  document.body.classList.toggle("focusing", !!item);
  if (item) {
    const card = item.querySelector(".card");
    item.classList.add("focused");
    $("#focus").append(card);
    focused = { item, card };
  }
  // on closing (or switching card) the mail reader goes back to the list
  if (mailOpen && !(focused && focused.card.querySelector("#mailList"))) { mailOpen = null; renderMail(last.mail || {}); }
  setTimeout(syncScreen, 520);   // the video follows its frame
}
let resizedAt = -1e9;   // when a card last finished resizing

function bindExpand() {
  $("#grid").addEventListener("click", e => {
    if (document.body.classList.contains("editing")) return;
    // releasing the handle after resizing arrives as a click on the card: that must not open it
    if (e.target.closest(".ui-resizable-handle") || performance.now() - resizedAt < 400) { e.stopPropagation(); return; }
    const item = e.target.closest(".grid-stack-item");
    if (!item) return;
    // with one open, tapping a small one opens it; otherwise only if you did not tap a button
    if (document.body.classList.contains("focusing")) { e.preventDefault(); e.stopPropagation(); setFocus(item); return; }
    if (!e.target.closest(INTERACTIVE)) setFocus(item);
  }, true);
  $("#focusClose").addEventListener("click", () => setFocus(null));
  addEventListener("keydown", e => { if (e.key === "Escape" && focused) setFocus(null); });
}

// ---------- battery and Claude usage ----------
async function watchBattery() {
  if (!navigator.getBattery) return;
  try {
    const b = await navigator.getBattery();
    const show = () => {
      const chip = $("#batteryChip");
      chip.hidden = false;
      chip.textContent = `${b.charging ? "⚡" : "🔋"} ${Math.round(b.level * 100)}%`;
      chip.classList.toggle("low", !b.charging && b.level < 0.2);
    };
    show();
    b.addEventListener("levelchange", show);
    b.addEventListener("chargingchange", show);
  } catch {}
}
function renderCredits(u) {
  const chip = $("#creditsChip");
  if (!chip || !u || u.left == null) return;
  chip.hidden = false;
  chip.textContent = `✦ ${Math.round(u.left)}%`;
  chip.title = `Claude: ${Math.round(u.left)}% of the session left` + (u.resets ? ` · resets ${u.resets}` : "");
  chip.classList.toggle("low", u.left < 15);
}

async function buildGrid() {
  mountFixed();
  bindChatPop();
  bindExpand();
  watchBattery();
  layout = await loadLayout();
  const gridEl = $("#grid");
  for (const item of layout) if (!item.hidden) gridEl.append(makeItem(item));
  // resizing always works from the corner (saved automatically); moving cards only while editing
  const rows = Math.max(ROWS, ...layout.filter(i => !i.hidden).map(i => i.y + i.h));
  grid = GridStack.init({ column: 12, cellHeight: 88, margin: 0, float: true, staticGrid: false, disableDrag: true,
                          maxRow: rows, animate: true, resizable: { handles: "e, se, s", autoHide: false } }, gridEl);
  grid.on("resizestart", () => { resizedAt = performance.now() + 1e9; });
  grid.on("resizestop", () => {
    resizedAt = performance.now();
    if (document.body.classList.contains("editing")) return;   // while editing it is saved when "Done" is pressed
    saveLayout();
    requestAnimationFrame(fitGrid);
    lastRect = ""; setTimeout(syncScreen, 400);   // the video follows its card
  });
  fitGrid();
  addEventListener("resize", fitGrid);
}

let webVersion = null;

function checkWebVersion(v) {
  if (!v) return;
  webVersion ??= v;
  const busy = document.body.classList.contains("editing");
  if (v !== webVersion && !busy) location.reload();
}

/** Save what is on the board. Only called on "Done", on add/remove and on reset. */
function saveLayout() {
  const nodes = Object.fromEntries(grid.engine.nodes.map(n => [n.el?.getAttribute("gs-id") || n.id, n]));
  layout = layout.map(item => {
    const n = nodes[item.id];
    return n ? { id: item.id, x: n.x, y: n.y, w: n.w, h: n.h } : { ...item, hidden: true };
  });
  fetch("/api/layout", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(layout) });
}

/** Full screen: rows stretch so the whole dashboard fits without scrolling. */
function fitGrid() {
  if (!grid || document.body.classList.contains("editing")) return;
  document.body.classList.add("fit");
  const top = $("#grid").getBoundingClientRect().top + scrollY;
  document.documentElement.style.setProperty("--grid-top", top + "px");
  const free = innerHeight - top - 14;
  const rows = Math.max(ROWS, ...grid.engine.nodes.map(n => n.y + n.h));
  const cell = Math.max(36, Math.floor(free / rows));
  grid.cellHeight(cell);
  document.documentElement.style.setProperty("--cell-h", cell + "px");
}

function toggleWidget(id) {
  const item = layout.find(w => w.id === id);
  if (!item) return;
  if (item.hidden) {
    // always inside the 12×12 board: in a free gap (shrinking it if needed) or by splitting the biggest card
    const def = DEFAULT_LAYOUT.find(d => d.id === id);
    item.hidden = false;
    const spot = placeFor(item.w || def.w, item.h || def.h);
    Object.assign(item, spot);
    const el = makeItem({ ...def, ...item });
    grid.batchUpdate();
    if (spot.donor) grid.update(spot.donor.el, spot.donor.size);
    $("#grid").append(el);   // makeWidget only prepares a card already on the board; it does not add it
    grid.makeWidget(el, { id, x: spot.x, y: spot.y, w: spot.w, h: spot.h });
    grid.batchUpdate(false);
    delete item.donor;
    renderAll();
    el.classList.add("just-added");
    setTimeout(() => el.classList.remove("just-added"), 1600);
  } else {
    const el = $(`.grid-stack-item[gs-id="${id}"]`);
    item.hidden = true;
    if (el) grid.removeWidget(el);
  }
  saveLayout();
  renderWidgetChips();
}

/** A spot for a new card without leaving the board: {x, y, w, h, donor?}. */
function placeFor(w, h) {
  const rows = ROWS, busy = Array.from({ length: rows }, () => Array(12).fill(false));
  for (const n of grid.engine.nodes)
    for (let y = n.y; y < Math.min(rows, n.y + n.h); y++) for (let x = n.x; x < n.x + n.w; x++) busy[y][x] = true;
  const free = (x, y, w, h) => {
    if (x + w > 12 || y + h > rows) return false;
    for (let j = y; j < y + h; j++) for (let i = x; i < x + w; i++) if (busy[j][i]) return false;
    return true;
  };
  // 1) its own size, and if it does not fit, smaller and smaller (minimum 2×2), biggest gap first
  const sizes = [];
  for (let ww = w; ww >= 2; ww--) for (let hh = h; hh >= 2; hh--) sizes.push([ww, hh]);
  sizes.sort((a, b) => b[0] * b[1] - a[0] * a[1]);
  for (const [ww, hh] of sizes)
    for (let y = 0; y <= rows - hh; y++) for (let x = 0; x <= 12 - ww; x++) if (free(x, y, ww, hh)) return { x, y, w: ww, h: hh };
  // 2) no gap: the biggest card is split in half and the new one takes one half
  const big = [...grid.engine.nodes].filter(n => n.h >= 4 || n.w >= 4).sort((a, b) => b.w * b.h - a.w * a.h)[0];
  if (!big) return { x: 0, y: rows, w, h };
  if (big.h >= big.w / 1.5 && big.h >= 4) {
    const keep = Math.ceil(big.h / 2);
    return { x: big.x, y: big.y + keep, w: big.w, h: big.h - keep, donor: { el: big.el, size: { h: keep } } };
  }
  const keep = Math.ceil(big.w / 2);
  return { x: big.x + keep, y: big.y, w: big.w - keep, h: big.h, donor: { el: big.el, size: { w: keep } } };
}

function renderWidgetChips() {
  $("#widgetChips").replaceChildren(...layout.map(item => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = WIDGETS[item.id];
    b.classList.toggle("on", !item.hidden);
    b.addEventListener("click", () => toggleWidget(item.id));
    return b;
  }));
}

function setEditing(editing) {
  if (editing) { setFocus(null); videoCmd({ action: "hide" }); }
  else { lastRect = ""; setTimeout(syncScreen, 600); }
  document.body.classList.toggle("editing", editing);
  $("#editBtn").classList.toggle("on", editing);
  $("#editbar").hidden = !editing;
  grid.enableMove(editing);   // resizing is always possible; moving only while editing
  if (editing) {
    renderWidgetChips();
    document.body.classList.remove("fit");   // scrolling is allowed while editing
  } else {
    saveLayout();
  }
  requestAnimationFrame(fitGrid);
}

async function resetLayout() {
  grid.removeAll();
  $$("#grid > .grid-stack-item").forEach(el => el.remove());   // in case one was left loose on the page
  grid.batchUpdate();
  layout = structuredClone(DEFAULT_LAYOUT);
  for (const item of layout) if (!item.hidden) { const el = makeItem(item); $("#grid").append(el); grid.makeWidget(el); }
  grid.batchUpdate(false);
  saveLayout();
  renderWidgetChips();
  renderAll();
}

// widgets are created and destroyed: after changing the dashboard everything is repainted from the latest known data
const last = {};
function renderAll() {
  bindWidgetControls();
  if (last.history) { $("#messages")?.replaceChildren(); last.history.forEach(m => addMessage({ ...m, restored: true })); showEmptyHint(); }
  if (last.status) renderStatus(last.status);
  if (last.spotify) renderSpotify(last.spotify);
  if (last.data) renderData(last.data);
  if (last.weather) renderWeather(last.weather);
  if (last.avisos) renderAvisos(last.avisos);
  renderTimers();
  if (last.compra) renderCompra(last.compra);
  loadDevices();
  if (last.mail) renderMail(last.mail); else loadMail();
  if (last.whatsapp) renderWhatsApp(last.whatsapp); else loadWhatsApp();
  renderVision(vision);
}

// ---------- backend connection ----------
function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = e => handle(JSON.parse(e.data));
  ws.onclose = () => {
    setState("idle");
    setSub(quitting ? "Cortex is closed" : "No connection to Cortex, retrying…");
    setTimeout(connect, 1500);
  };
}

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
}

function handle(msg) {
  switch (msg.type) {
    case "hello":
      checkWebVersion(msg.web);
      last.history = msg.history;
      $("#messages")?.replaceChildren();
      msg.history.forEach(m => addMessage({ ...m, restored: true }));
      applySettings(msg.settings);
      setState(msg.state);
      setSub("");
      showEmptyHint();
      break;
    case "state":
      setState(msg.state);
      if (msg.state !== "idle") wakeUp();
      break;
    case "level": $("#bars").style.setProperty("--level", Math.min(1, msg.level * 14).toFixed(3)); break;
    case "user":
      remember(msg);
      addMessage(msg);
      setSub(`"${msg.text}"`);
      break;
    case "assistant": remember(msg); addMessage(msg); break;
    case "assistant_start":
      streamingEl = addMessage({ role: "assistant", text: "" });
      streamingEl?.classList.add("streaming");
      break;
    case "assistant_delta":
      if (streamingEl) {
        streamingEl.textContent += msg.text;
        setSub(streamingEl.textContent.trim().split("\n").pop());
        scrollChat();
      }
      break;
    case "assistant_end":
      if (streamingEl) {
        if (!streamingEl.textContent.trim()) streamingEl.remove();
        else streamingEl.classList.remove("streaming");
      }
      if (msg.text) remember({ role: "assistant", text: msg.text });
      streamingEl = null;
      break;
    case "activity": setSub(msg.text); break;
    case "error":
      addMessage({ role: "error", text: msg.text });
      toast(msg.text);
      break;
    case "cleared":
      last.history = [];
      $("#messages")?.replaceChildren();
      showEmptyHint();
      break;
    case "settings": applySettings(msg.settings); break;
    case "status": last.status = msg; renderStatus(msg); checkWebVersion(msg.web); renderCredits(msg.claude_usage); break;
    case "enroll": renderEnroll(msg); break;
    case "spotify": last.spotify = msg; renderSpotify(msg); break;
    case "data": last.data = msg.data; renderData(msg.data); break;
    case "vision": vision = msg; renderVision(msg); break;
    case "gesture": onGesture(msg); break;
    case "presence": if (msg.engaged) wakeUp(); break;
    case "avisos": last.avisos = msg.items; renderAvisos(msg.items); break;
    case "aviso": wakeUp(); break;
    case "timers": timerList = msg.timers || []; renderTimers(); break;
    case "timer_done": wakeUp(); $(".card.timers")?.classList.add("timer-flash");
      setTimeout(() => $(".card.timers")?.classList.remove("timer-flash"), 6000); break;
    case "compra": last.compra = msg.items; renderCompra(msg.items); break;
    case "video": last.video = msg; renderVideoNow(msg); break;
    case "mail": last.mail = msg; renderMail(msg); break;
    case "whatsapp": last.whatsapp = msg; renderWhatsApp(msg); break;
    case "notice": toast(msg.text); break;
    case "video_results":
      if (layout.find(w => w.id === "search")?.hidden) toggleWidget("search");
      if (msg.query && $("#searchInput")) $("#searchInput").value = msg.query;
      renderVideos(msg.results || []);
      break;
    case "wake_train":
      $("#retrainBtn").disabled = msg.running;
      $("#trainInfo").textContent = msg.running ? "Retraining… (a few minutes, you can keep using it)"
        : msg.ok ? `Detector updated. ${msg.summary || ""}` : "Retraining failed: see logs\\train_wake.log";
      break;
  }
}

function remember(m) {
  last.history = [...(last.history || []), m].slice(-100);
}

function setState(state) {
  document.body.dataset.state = state;
  $("#stateLabel").textContent = STATE_TEXT[state] || STATE_TEXT.idle;
  if (state === "idle" || state === "listening") setSub("");
  if (state !== "listening") $("#bars").style.setProperty("--level", 0);
}

function setSub(text) {
  $("#stateSub").textContent = text || "";
}

// ---------- conversation ----------
function addMessage(m) {
  const box = $("#messages");
  if (!box) return null;
  $(".empty", box)?.remove();
  const el = document.createElement("div");
  el.className = `msg ${m.role}`;
  el.textContent = m.text;
  if (m.local) {
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = "without Claude";
    el.append(meta);
  }
  box.append(el);
  scrollChat();
  return el;
}

function showEmptyHint() {
  const box = $("#messages");
  if (box && !box.children.length) {
    const p = document.createElement("p");
    p.className = "empty";
    p.textContent = "Here you will see what you say to the assistant. While waiting for \"Cortex\", the mic is always on and may trigger by mistake. That audio is processed on this computer; what you say or type is sent to Claude, and replies are read aloud with the Microsoft Edge online voice.";
    box.append(p);
  }
}

function scrollChat() {
  const box = $("#messages");
  if (box) box.scrollTop = box.scrollHeight;
}

// ---------- status ----------
function renderStatus(s) {
  const wake = $("#wakeBtn");
  wake.classList.toggle("on", s.wake);
  wake.classList.toggle("off", !s.wake);
  wake.querySelector("use").setAttribute("href", s.wake ? "#i-ear" : "#i-ear-off");
  wake.title = s.wake ? "Listening for \"Cortex\" (press to pause)" : "Not listening for \"Cortex\" (press to enable)";

  renderWake(s);
  const list = $("#statusList");
  if (!list) return;
  const plan = s.claude.plan || {};
  const used = plan.unifiedWindows?.five_hour?.utilization;
  const rows = [
    ["Mic", s.mic.ok, s.mic.name || "no mic"],
    ["Speaker", s.speaker.ok, s.speaker.name || "no speaker"],
    ["Claude", s.claude.ok, s.claude.ok === false ? "having problems" : modelName(s.claude.model)],
    ["\"Cortex\"", s.wake, s.wake ? (s.wake_method === "detector" ? "listening (detector)" : "listening") : "paused"],
  ];
  if (vision.enabled !== undefined) rows.push(["Camera", vision.enabled ? vision.camera_ok : null,
    !vision.enabled ? "off" : vision.camera_ok ? (vision.engaged ? "watching you" : "standing by") : "no camera"]);
  if (s.mic.muted) rows[0][2] = "muted in Windows";
  list.replaceChildren(...rows.map(([label, ok, value]) => {
    const li = document.createElement("li");
    li.className = ok === null || ok === undefined ? "" : ok ? "ok" : "bad";
    li.innerHTML = `<i></i><span class="label"></span><span class="value"></span>`;
    li.querySelector(".label").textContent = label;
    li.querySelector(".value").textContent = value;
    return li;
  }));
  if (s.mic.muted) {
    const btn = document.createElement("button");
    btn.className = "btn";
    btn.textContent = "Unmute mic";
    btn.addEventListener("click", () => send({ action: "unmute_mic" }));
    const li = document.createElement("li");
    li.style.gridTemplateColumns = "1fr";
    li.append(btn);
    list.append(li);
  }
  if (used !== undefined) {
    const li = document.createElement("li");
    li.className = used > 0.9 ? "bad" : "ok";
    li.innerHTML = `<i></i><span class="label">5 h plan</span><span class="meter"><span class="bar"><span></span></span><small></small></span>`;
    li.querySelector(".bar span").style.width = `${Math.round(used * 100)}%`;
    li.querySelector("small").textContent = `${Math.round(used * 100)}%`;
    list.append(li);
  }
}

let enrolling = false;
function renderEnroll(m) {
  enrolling = !m.done;
  $("#enrollBtn").disabled = enrolling;
  if (m.done) {
    $("#enrollInfo").textContent = m.message || "";
    setSub(m.message || "");
  } else {
    const text = `Say "Cortex" (${m.step} of ${m.total})`;
    $("#enrollInfo").textContent = text;
    setSub(text);
  }
}

function renderWake(s) {
  if (!enrolling) {
    $("#enrollInfo").textContent = s.wake_samples
      ? `I have ${s.wake_samples} recordings of you. You can record more if it still misses.`
      : "Record \"Cortex\" a few times so it recognises you better.";
  }
  const h = s.last_heard || {};
  const box = $("#lastHeard");
  box.hidden = !h.time;
  if (h.time) {
    const ago = Math.round((Date.now() / 1000 - h.time));
    box.innerHTML = "Last thing I heard: <b></b><br><span></span>";
    box.querySelector("b").textContent = `"${h.text || "…"}"`;
    box.querySelector("span").textContent =
      `${h.wake ? "Triggered" : "Did not trigger"} · score ${h.distance} · ${ago < 60 ? ago + " s" : Math.round(ago / 60) + " min"} ago`;
  }
}

// ---------- music ----------
let spotify = {};
let spTimer;
function renderSpotify(s) {
  spotify = s;
  document.body.classList.toggle("music-on", !!s.playing);
  const amb = $("#ambMusic");
  amb.textContent = s.playing && s.title ? `${s.title} · ${s.artist}` : "";
  if (!$("#spOn")) return;
  $("#spOff").hidden = s.connected;
  $("#spOn").hidden = !s.connected;
  $("#spOffText").textContent = s.client_id
    ? "Connect your account to play music by voice."
    : "Spotify Client ID missing: Settings → Brain → Setup assistant.";
  $("#spConnect").hidden = !s.client_id;
  $("#spDevice").textContent = s.device || "";
  $("#spTitle").textContent = s.title || "Nothing playing";
  $("#spArtist").textContent = s.artist || "Say \"Cortex, play the playlist…\"";
  const cover = $("#spCover");
  cover.style.backgroundImage = s.image ? `url("${s.image}")` : "";
  cover.classList.toggle("has-img", !!s.image);
  const bg = $("#spBg");
  bg.style.backgroundImage = s.image ? `url("${s.image}")` : "";
  bg.classList.toggle("on", !!s.image && s.playing);
  $("#spPlayIcon").setAttribute("href", s.playing ? "#i-pause" : "#i-play");
  if (s.volume !== null && s.volume !== undefined && document.activeElement !== $("#spVolume")) $("#spVolume").value = s.volume;
  clearInterval(spTimer);
  let progress = s.progress;
  const draw = () => {
    const bar = $("#spProgress");
    if (bar) bar.style.width = s.duration ? `${Math.min(100, (progress / s.duration) * 100)}%` : "0";
  };
  draw();
  if (s.playing) spTimer = setInterval(() => { progress += 1000; draw(); }, 1000);
  renderSpotifySettings();
}

function renderSpotifySettings() {
  const s = spotify;
  const info = $("#spSettingsInfo");
  info.textContent = !s.client_id ? "Spotify Client ID missing: Settings → Brain → Setup assistant"
    : s.connected ? "Connected to your account." : "Not connected.";
  $("#spSettingsConnect").hidden = s.connected || !s.client_id;
  $("#spLogout").hidden = !s.connected;
}

function spotifyCmd(action, value) {
  return fetch("/api/spotify", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, value }),
  }).then(r => r.json()).then(r => { if (r.error) toast(r.error); return r; });
}

// ---------- data (Drive) ----------
function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}

function lower1(s) { return s ? s[0].toLowerCase() + s.slice(1) : ""; }

async function loadData() {
  try {
    last.data = await (await fetch("/api/data")).json();
    renderData(last.data);
  } catch {}
  loadBank();
}

function setHTML(sel, html) {
  const el = $(sel);
  if (el) el.innerHTML = html;
}

function renderData(d) {
  if (!d || !d.ok) return;
  const now = new Date().toTimeString().slice(0, 5);
  if ($("#todayMode")) $("#todayMode").textContent = d.today.mode || d.today.day;
  setHTML("#todayList", d.today.items.length ? d.today.items.map(i => {
    const active = /^\d\d:\d\d$/.test(i.hora) && i.fin && i.hora <= now && now < i.fin;
    return `<li class="${active ? "now" : ""}"><span class="t">${esc(i.hora)}</span><span>${esc(i.que)}</span></li>`;
  }).join("") : `<li class="empty">Nothing else today</li>`);

  renderTasks(d.tasks);

  const [next, ...rest] = d.betis;
  setHTML("#betisNext", next ? `<b>${esc(next.partido)}</b><span>${esc(next.cuando)}${next.hora ? " · " + esc(next.hora) : ""}</span>`
    : `<span>No matches listed</span>`);
  setHTML("#betisList", rest.map(m => `<li>
      <span class="when">${esc(m.cuando)}</span>
      <div><div>${esc(m.partido)}</div><small>${esc(m.hora || "")}</small></div>
    </li>`).join(""));

  if ($("#gymToday")) {
    $("#gymToday").textContent = d.gym.today || "Rest day today";
    $("#gymNext").textContent = d.gym.tomorrow ? `Tomorrow: ${lower1(d.gym.tomorrow)}` : "";
    setHTML("#gymWeek", (d.gym.week || []).map(w => `<li class="${w.hoy ? "now" : ""}"><span class="when">${esc(w.dia)}</span>
      <div>${esc(w.que)}</div></li>`).join(""));
    $("#gymNotes").textContent = (d.gym.notes || []).join(" · ");
  }

  setHTML("#mealsList", d.menu.map(m => `<li class="${m.hoy ? "now" : ""}">
      <span class="when">${esc(m.dia)}</span>
      <div><div>${esc(m.plato)}</div><small>${esc(m.cubre || "")}</small></div>
    </li>`).join("") || `<li class="empty">No menu</li>`);

  renderMoney();
  setHTML("#datesList", d.dates.map(f => `<li>
      <span class="when">${esc(f.cuando)}</span><div>${esc(f.que)}</div></li>`).join("") || `<li class="empty">Nothing in the coming weeks</li>`);
}

// ---------- tasks (manual: done, edit, remove, new; no confirmations) ----------
const PRIOS = ["alta", "media", "baja"];   // data values from the sheet
const PRIO_LABEL = { alta: "High", media: "Medium", baja: "Low" };
function renderTasks(tasks) {
  const list = $("#tasksList");
  if (!list || !tasks || list.querySelector(".t-form")) return;   // do not overwrite what you are typing
  $("#tasksCount").textContent = tasks.length || "";
  list.innerHTML = tasks.map(t => `<li data-task="${t.i}" title="${esc(t.notas)}">
      <button class="t-check p-${esc(t.prioridad)}" data-task-act="done" title="Done"><svg><use href="#i-check"/></svg></button>
      <div class="t-body"><div>${esc(t.tarea)}</div>${t.cuando || t.estado ? `<small>${esc([t.cuando, t.estado].filter(Boolean).join(" · "))}</small>` : ""}</div>
      <span class="t-acts">
        <button class="icon-btn sm" data-task-act="edit" title="Edit"><svg><use href="#i-pencil"/></svg></button>
        <button class="icon-btn sm" data-task-act="delete" title="Remove"><svg><use href="#i-trash"/></svg></button>
      </span>
    </li>`).join("") || `<li class="empty">Nothing pending 🎉</li>`;
}

function taskForm(t = {}) {
  const li = document.createElement("li");
  li.className = "t-form";
  li.dataset.task = t.i ?? "";
  li.innerHTML = `<form>
      <input class="input" name="tarea" placeholder="What needs doing?" maxlength="200" required>
      <div class="t-form-row">
        <input class="input" name="cuando" placeholder="When (optional)" maxlength="80">
        <select class="input" name="prioridad">${PRIOS.map(p => `<option value="${p}">${PRIO_LABEL[p]}</option>`).join("")}</select>
        <button type="button" class="text-btn" data-task-act="cancel">Cancel</button>
        <button class="btn primary"><svg><use href="#i-check"/></svg>Save</button>
      </div></form>`;
  const f = li.querySelector("form");
  f.tarea.value = t.tarea || "";
  f.cuando.value = t.cuando || "";
  f.prioridad.value = t.prioridad || "media";
  f.addEventListener("submit", e => {
    e.preventDefault();
    const body = { tarea: f.tarea.value, cuando: f.cuando.value, prioridad: f.prioridad.value };
    if (t.i == null) taskCmd({ action: "add", ...body }); else taskCmd({ action: "edit", i: t.i, ...body });
  });
  return li;
}

async function taskCmd(body) {
  try {
    const r = await (await fetch("/api/tasks", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
    if (!r.ok) return toast(r.error);
    $$("#tasksList .t-form").forEach(el => el.remove());
    last.data = r.data;
    renderData(r.data);
  } catch (e) { toast("Could not save the task"); }
}

function onTaskClick(e) {
  const b = e.target.closest("[data-task-act]");
  if (!b) return;
  const li = b.closest("li");
  const i = li.dataset.task === "" ? null : Number(li.dataset.task);
  const act = b.dataset.taskAct;
  if (act === "cancel") { li.remove(); renderTasks(last.data?.tasks); return; }
  if (act === "done") { li.classList.add("leaving"); taskCmd({ action: "done", i }); }
  if (act === "delete") { li.classList.add("leaving"); taskCmd({ action: "delete", i }); }
  if (act === "edit") {
    $$("#tasksList .t-form").forEach(el => el.remove());
    const t = (last.data?.tasks || []).find(x => x.i === i);
    const form = taskForm(t);
    li.replaceWith(form);
    form.querySelector("input").focus();
  }
}

function addTask() {
  const list = $("#tasksList");
  if (list.querySelector(".t-form")) return list.querySelector(".t-form input").focus();
  const form = taskForm();
  list.prepend(form);
  list.scrollTop = 0;
  form.querySelector("input").focus();
}

// ---------- money: balance and latest transactions ----------
function renderMoney() {
  if (!$("#moneyStats")) return;
  const money = last.data?.money || {};
  const a = last.bank?.ok && last.bank.accounts[0];
  const eur2 = v => v.toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " €";
  setHTML("#moneyStats", [["Revolut", bankBalance, "big"], ["Cash stash", money.colchon, ""]]
    .filter(([, v]) => v != null)
    .map(([k, v, cls]) => `<div class="${cls}"><small>${k}</small><b>${esc(typeof v === "number" ? eur2(v) : v)}</b></div>`).join(""));
  const moves = a ? a.recent || [] : [];
  const day = s => s ? new Date(s + "T12:00").toLocaleDateString("en-GB", { day: "numeric", month: "short" }) : "";
  setHTML("#moneyMoves", moves.map(m => `<li class="${m.amount > 0 ? "in" : ""}">
      <span class="when">${esc(day(m.date))}</span><span class="m-desc">${esc(m.description || "—")}${m.pending ? " <small>pending</small>" : ""}</span>
      <b>${m.amount > 0 ? "+" : "−"}${esc(eur2(Math.abs(m.amount)))}</b></li>`).join("")
    || `<li class="empty">${a ? "No recent transactions" : "No bank connected"}</li>`);
  if ($("#moneyUpdated") && last.bank?.fetched_at)
    $("#moneyUpdated").textContent = new Date(last.bank.fetched_at * 1000).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
}

// ---------- bank (Revolut, read-only) ----------
let bankBalance = null;

const eur0 = v => Math.round(v).toLocaleString("en-GB") + " €";

async function loadBank() {
  try {
    const r = await (await fetch("/api/bank")).json();
    last.bank = r;
    const a = r.ok && r.accounts[0];
    bankBalance = a && a.balance && a.balance.amount != null ? a.balance.amount : null;
    if (last.data) renderData(last.data);
    renderMoney();
  } catch {}
}

// ---------- search ----------
function renderVideos(list) {
  setHTML("#searchResults", list.map(v => `<li><a href="#" data-video="${esc(v.id)}" data-title="${esc(v.title)}">
    <div class="r-thumb"><img src="${esc(v.thumb)}" alt="" loading="lazy"><span class="r-len">${esc(v.length)}</span></div>
    <div class="r-title">${esc(v.title)}</div><div class="r-site">${esc(v.channel)}</div></a></li>`).join("")
    || `<li class="r-empty">Nothing on YouTube for that.</li>`);
}

const fmtTime = s => s == null ? "" : `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

/** The widget frame in screen pixels (mpv is placed on top of it). */
function screenRect() {
  const el = $("#videoScreen");
  if (!el || el.hidden) return null;
  const r = el.getBoundingClientRect(), k = devicePixelRatio || 1;
  return { x: Math.round((screenX + r.left) * k), y: Math.round((screenY + r.top) * k),
           w: Math.round(r.width * k), h: Math.round(r.height * k) };
}
let lastRect = "";
function syncScreen() {
  const r = screenRect();
  const key = JSON.stringify(r);
  if (r && key !== lastRect) { lastRect = key; videoCmd({ action: "rect", rect: r }); }
}
addEventListener("resize", () => setTimeout(syncScreen, 300));

function showScreen(on) {
  if (!$("#videoScreen")) return;
  $("#videoScreen").hidden = !on;
  $(".card.search")?.classList.toggle("playing", on);
  if (!on) lastRect = "";
}

function renderVideoNow(st) {
  if (!$("#videoNow")) return;
  $("#videoNow").hidden = !st.playing;
  const inside = !!st.playing && (st.source !== "web" || st.docked !== false) && !st.fullscreen;
  showScreen(inside);
  if (inside) requestAnimationFrame(syncScreen);
  if ($("#videoDock")) {
    $("#videoDock").hidden = st.source !== "web";
    $("#videoDock").dataset.v = st.docked === false ? "dock" : "undock";
    $("#videoDock").textContent = st.docked === false ? "To dashboard" : "Window";
  }
  if (!st.playing) return;
  $("#videoTitle").textContent = st.title || "Video";
  $("#videoTime").textContent = st.duration ? `${fmtTime(st.pos)} / ${fmtTime(st.duration)}${st.paused ? " · paused" : ""}` : "Loading…";
  $("#videoProg").style.width = st.duration ? `${Math.min(100, (st.pos || 0) / st.duration * 100)}%` : "0";
  $("#videoPP").setAttribute("href", st.paused ? "#i-play" : "#i-pause");
}

async function loadVideoServices() {
  if (!$("#videoApps")) return;
  try {
    const r = await (await fetch("/api/video/services")).json();
    setHTML("#videoApps", r.services.filter(s => s.name !== "YouTube")
      .map(s => `<button data-app="${esc(s.url)}">${esc(s.name)}</button>`).join(""));
  } catch {}
}

function videoCmd(body) {
  fetch("/api/video", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

async function doSearch(e) {
  e?.preventDefault();
  const q = $("#searchInput").value.trim();
  if (!q) return;
  setHTML("#searchResults", `<li class="r-empty">Searching…</li>`);
  try {
    const r = await (await fetch("/api/youtube?q=" + encodeURIComponent(q))).json();
    if (!r.ok) throw new Error(r.error);
    renderVideos(r.results);
  } catch (err) {
    setHTML("#searchResults", `<li class="r-empty">${esc(err.message)}</li>`);
  }
}


// ---------- timers ----------
let timerList = [];
let timerOffset = 0;   // server clock − dashboard clock

async function loadTimers() {
  try {
    const r = await (await fetch("/api/timers")).json();
    timerOffset = r.now * 1000 - Date.now();
    timerList = r.timers;
    renderTimers();
  } catch {}
}

function fmtLeft(s) {
  s = Math.max(0, Math.ceil(s));
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), sec = s % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(sec).padStart(2, "0");
}

function renderTimers() {
  const list = $("#timerList");
  if (!list) return;
  const now = (Date.now() + timerOffset) / 1000;
  const live = timerList.filter(t => t.end > now - 1);
  if (!live.length) {
    if (list.dataset.empty !== "1") list.innerHTML = `<li class="r-empty">Say "Cortex, set a 10-minute timer" or tap one.</li>`;
    list.dataset.empty = "1";
    return;
  }
  list.dataset.empty = "";
  list.innerHTML = live.map(t => {
    const left = t.end - now;
    return `<li class="${left < 10 ? "ending" : ""}"><div><div class="t-left">${fmtLeft(left)}</div>
      <div class="t-label">${esc(t.label || "Timer")}</div></div><span></span>
      <button class="icon-btn small" data-cancel-timer="${esc(t.id)}" title="Remove"><svg><use href="#i-x"/></svg></button>
      <span class="t-bar" style="width:${Math.max(0, Math.min(100, left / t.seconds * 100))}%"></span></li>`;
  }).join("");
}

async function timerCmd(body) {
  try {
    const r = await (await fetch("/api/timers", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
    if (r.ok) { timerOffset = r.now * 1000 - Date.now(); timerList = r.timers; renderTimers(); }
  } catch {}
}

// ---------- shopping list ----------
async function loadCompra() {
  try { last.compra = (await (await fetch("/api/compra")).json()).items; renderCompra(last.compra); } catch {}
}

// ---------- mail ----------
async function loadMail() {
  try { last.mail = await (await fetch("/api/mail")).json(); renderMail(last.mail); } catch {}
}

let mailLogin = false;   // accounts form open
const MAIL_DEFAULT = {};   // no default addresses: the user types their own
let mailOpen = null;   // mail open in the reader (full-size card)
function renderMail(m) {
  if (!$("#mailList")) return;
  if (mailOpen && !document.body.classList.contains("focusing")) mailOpen = null;
  if (mailOpen) return;   // reading: the list does not overwrite the reader
  const pending = (m.slots || []).some(s => !s.ok);
  if (mailLogin || !m.configured || (pending && !m.unread.length)) {
    if (!$("#mailList .mail-login")) renderMailLogin(m);
    $("#mailCount").textContent = "";
    return;
  }
  $("#mailCount").textContent = m.unread.length ? `${m.unread.length} unread` : "";
  const when = t => t ? new Date(t * 1000).toLocaleString("en-GB", { weekday: "short", hour: "2-digit", minute: "2-digit" }) : "";
  const two = (m.accounts || []).length > 1;
  setHTML("#mailList", (m.error ? `<li class="setup">${esc(m.error)}</li>` : "") + (m.unread.map(x => `<li data-mail="${esc(x.id)}">
      <div><b>${esc(x.from)}</b><span>${esc(x.subject)}</span></div>
      <small>${two ? `<i class="acc acc-${esc(x.account)}">${x.account === "principal" ? "P" : "S"}</i> ` : ""}${esc(when(x.time))}</small></li>`).join("")
    || `<li class="setup">Nothing unread.</li>`));
}

const KB = n => n < 1024 ? `${n} B` : n < 1048576 ? `${Math.round(n / 1024)} KB` : `${(n / 1048576).toFixed(1)} MB`;

async function openMail(id) {
  mailOpen = id;
  setHTML("#mailList", `<li class="mail-read">
      <div class="mail-read-head"><button class="icon-btn" id="mailBack" title="Back">‹</button>
        <div class="mr-meta"><b id="mrSubject">Opening…</b><span id="mrFrom"></span></div>
        <button class="text-btn" id="mailSum">Summarize</button></div>
      <div class="mr-files" id="mrFiles"></div>
      <iframe id="mrBody" sandbox="allow-popups allow-popups-to-escape-sandbox" referrerpolicy="no-referrer"
        src="/api/mail/view?id=${encodeURIComponent(id)}"></iframe></li>`);
  $("#mailBack").onclick = () => { mailOpen = null; renderMail(last.mail || {}); };
  $("#mailSum").onclick = () => send({ action: "text", text: `Give me a short summary of email ${id}.` });
  let r;
  try {
    const res = await fetch(`/api/mail/open?id=${encodeURIComponent(id)}`);
    r = res.ok ? await res.json() : { error: await res.text() };
  } catch { r = { error: "No connection to Cortex" }; }
  if (mailOpen !== id) return;
  if (r.error) { $("#mrSubject").textContent = r.error; return; }
  $("#mrSubject").textContent = r.subject || "(no subject)";
  const d = r.date ? new Date(r.date) : null;
  $("#mrFrom").textContent = r.from + (d && !isNaN(d) ? " · " + d.toLocaleString("en-GB", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "");
  const part = (f, dl) => `/api/mail/part?id=${encodeURIComponent(id)}&n=${f.n}${dl ? "&dl=1" : ""}`;
  setHTML("#mrFiles", r.files.map(f => f.type.startsWith("image/") && f.type !== "image/svg+xml"
    ? `<a class="mr-file mr-img" href="${part(f, 1)}" download="${esc(f.name)}" title="Download ${esc(f.name)}">
         <img src="${part(f)}" alt="" data-zoom="${part(f)}"><span>${esc(f.name)}</span><small>${KB(f.size)} ⬇</small></a>`
    : `<a class="mr-file" href="${part(f, 1)}" download="${esc(f.name)}" title="Download">📎 <span>${esc(f.name)}</span><small>${KB(f.size)} ⬇</small></a>`).join(""));
  document.querySelectorAll("#mrFiles img[data-zoom]").forEach(i => i.onclick = e => { e.preventDefault(); waZoom(i.dataset.zoom); });
}

// sign-in: once per account; the app password is tested on the spot and saved in .env
function renderMailLogin(m) {
  const slots = m.slots?.length ? m.slots : [{ slot: "p", label: "principal" }, { slot: "s", label: "secundaria" }]   // labels are data values;
  setHTML("#mailList", `<li class="mail-login">
    <p>Once per account: <b>1</b> create an app password in Google (two-step verification is required),
      <b>2</b> paste the 16 letters here and press Connect.</p>
    ${slots.map(s => `<form class="ml-row" data-slot="${s.slot}" autocomplete="off">
      <span class="ml-label">${s.label === "principal" ? "Primary" : "Secondary"} ${s.ok ? "✅" : ""}</span>
      <input class="input" name="user" value="${esc(s.user || MAIL_DEFAULT[s.slot] || "")}" placeholder="you@gmail.com">
      <a class="text-btn" href="https://myaccount.google.com/apppasswords" target="_blank" rel="noopener">1 · Create password</a>
      <input class="input" name="password" type="password" placeholder="${s.ok ? "Connected · paste another to change it" : "2 · Paste the 16 letters"}">
      <button class="text-btn ml-go">Connect</button>
      <small class="ml-msg"></small></form>`).join("")}
    ${m.configured ? `<button type="button" class="text-btn ml-done">Done</button>` : ""}</li>`);
  $("#mailList .ml-done")?.addEventListener("click", () => { mailLogin = false; $("#mailList").replaceChildren(); renderMail(last.mail); });
  document.querySelectorAll("#mailList .ml-row").forEach(f => f.onsubmit = async e => {
    e.preventDefault();
    const msg = f.querySelector(".ml-msg");
    msg.className = "ml-msg"; msg.textContent = "Testing…";
    let r;
    try {
      r = await (await fetch("/api/mail/login", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slot: f.dataset.slot, user: f.user.value, password: f.password.value }) })).json();
    } catch { r = { ok: false, error: "Cortex is not responding." }; }
    msg.className = "ml-msg " + (r.ok ? "ok" : "bad");
    msg.textContent = r.ok ? "Connected ✅" : r.error;
    if (r.ok && !(r.slots || []).some(s => !s.ok)) mailLogin = false;   // both fine: the mail shows on the next refresh
    if (r.ok) { f.password.value = ""; last.mail = r; f.querySelector(".ml-label").textContent = f.querySelector(".ml-label").textContent.replace(" ✅", "") + " ✅"; }
  });
}

// "Refresh" button: spins while checking and repaints with whatever arrives
async function reloadBtn(b, url, done) {
  if (b.classList.contains("spin")) return;
  b.classList.add("spin");
  try { done(await (await fetch(url, { method: "POST" })).json()); }
  catch { toast("Could not refresh"); }
  finally { b.classList.remove("spin"); }
}

// ---------- WhatsApp ----------
async function loadWhatsApp() {
  try { last.whatsapp = await (await fetch("/api/whatsapp")).json(); renderWhatsApp(last.whatsapp); } catch {}
}

let waQr = "", waOpen = null;   // waOpen: chat open in the full-size card
function renderWhatsApp(m) {
  const box = $("#waList");
  if (!box) return;
  if (waOpen && !document.body.classList.contains("focusing")) waOpen = null;
  if (waOpen) { refreshWaChat(); return; }
  const state = $("#waState");
  if (!m.available) {
    setHTML("#waList", `<li class="setup">The WhatsApp bridge is missing. Reinstall Cortex with WhatsApp ticked.</li>`);
    state.textContent = "";
    return;
  }
  if (m.qr) {
    state.textContent = "Link";
    if (waQr !== m.qr || !$("#waQr")) {   // the QR changes every ~20 s
      waQr = m.qr;
      setHTML("#waList", `<li class="wa-qr"><img id="waQr" src="/api/whatsapp/qr.svg?t=${Date.now()}" alt="WhatsApp QR">
        <span>On your phone: WhatsApp → Settings → <b>Linked devices</b> → Link a device, and scan this.</span></li>`);
    }
    return;
  }
  waQr = "";
  if (m.state !== "connected" && !m.chats.length) {
    state.textContent = "";
    setHTML("#waList", `<li class="setup">${m.state === "off" ? "Not linked: Settings → Brain → Setup assistant." : m.state === "error" ? "The bridge went down, retrying…" : "Connecting to WhatsApp…"}</li>`);
    return;
  }
  state.textContent = m.state === "connected" ? "" : "Offline";
  const when = t => {
    if (!t) return "";
    const d = new Date(t * 1000), today = new Date().toDateString() === d.toDateString();
    return today ? d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })
                 : d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric" });
  };
  setHTML("#waList", m.chats.map(c => `<li data-chat="${esc(c.jid)}" class="${c.mine ? "mine" : "theirs"}${c.muted ? " muted" : ""}">
      <div><b>${c.group ? "👥 " : ""}${esc(c.name)}${c.real !== c.name ? ` <em>${esc(c.real)}</em>` : ""}</b>
      <span>${c.mine ? "You: " : c.from ? esc(c.from) + ": " : ""}${esc(c.text)}</span></div>
      <small>${esc(waWhen(c.time))}</small>
      ${waBell(c)}</li>`).join("") || `<li class="setup">Syncing chats…</li>`);
}

// bell: notifies / muted
function waBell(c) {
  return `<button class="wa-bell" data-mute="${esc(c.jid)}" data-name="${esc(c.real || c.name)}" data-muted="${c.muted ? 1 : 0}"
    title="${c.muted ? "Muted: no notifications. Tap to turn them on" : "Notifies. Tap to mute"}">${c.muted ? "🔕" : "🔔"}</button>`;
}
async function waToggleMute(b) {
  const muted = b.dataset.muted !== "1";
  b.dataset.muted = muted ? 1 : 0;
  b.textContent = muted ? "🔕" : "🔔";
  b.closest("li")?.classList.toggle("muted", muted);
  await fetch("/api/whatsapp/mute", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ jid: b.dataset.mute, name: b.dataset.name, muted }) });
  toast(muted ? "Muted: you will not be notified" : "You will be notified about this chat");
}

function waWhen(t, timeOnly) {
  if (!t) return "";
  const d = new Date(t * 1000), today = new Date().toDateString() === d.toDateString();
  return today || timeOnly ? d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })
                           : d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric" });
}

// an open chat: header with the nickname and the messages; consecutive ones from the same person do not repeat the name
async function openWaChat(jid) {
  waOpen = jid;
  $("#waState").textContent = "";
  setHTML("#waList", `<li class="wa-chat">
      <div class="wa-chat-head"><button class="icon-btn" id="waBack" title="Back">‹</button>
        <b id="waName"></b><span id="waBellBox"></span>
        <input class="input" id="waMote" placeholder="Nickname (how Cortex says it)" autocomplete="off">
        <button class="text-btn" id="waSum">Summarize</button></div>
      <div class="wa-msgs" id="waMsgs"></div>
      ${settings.whatsapp_mode === "write" ? `<form class="with-btn" id="waSend"><input class="input" name="text" maxlength="4000" placeholder="Type a message" aria-label="Type a message" autocomplete="off"><button class="btn primary">Send</button></form>` : ""}</li>`);
  if ($("#waSend")) $("#waSend").onsubmit = async e => {
    e.preventDefault();
    const f = e.target, text = f.text.value.trim(), b = f.querySelector("button");
    if (!text || b.disabled) return;
    b.disabled = true;   // a double Enter does not send it twice
    let r = {};
    try { r = await (await fetch("/api/whatsapp/send", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ jid, text }) })).json(); } catch {}
    b.disabled = false;
    if (!r.ok) { toast(r.error || "Not sure it went out: check your phone."); return; }
    f.text.value = "";
    if (waOpen === jid) refreshWaChat(true);
  };
  $("#waBack").onclick = () => { waOpen = null; renderWhatsApp(last.whatsapp); };
  $("#waSum").onclick = () => send({ action: "text", text: `Summarize the latest in the WhatsApp chat "${$("#waName").dataset.real}".` });
  $("#waMote").onkeydown = async e => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    await fetch("/api/whatsapp/mote", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: $("#waName").dataset.real, mote: e.target.value }) });
    e.target.blur();
    toast(e.target.value.trim() ? `Cortex will call them "${e.target.value.trim()}"` : "Nickname removed");
  };
  await refreshWaChat(true);
}

function waBody(m) {
  const src = `/api/whatsapp/media?jid=${encodeURIComponent(waOpen)}&id=${encodeURIComponent(m.id)}`;
  const cap = m.caption ? `<p>${esc(m.caption)}</p>` : "";
  if (m.media === "audio") return `<audio controls preload="none" data-id="${esc(m.id)}" src="${src}"></audio>`;
  if (m.media === "image" || m.media === "sticker")
    return `<img class="wa-${m.media}" loading="lazy" data-id="${esc(m.id)}" src="${src}" alt="">${cap}`;
  return `<p>${esc(m.text)}</p>`;
}

function waGone(el) {
  const what = el.tagName === "AUDIO" ? "🎤 Voice note" : el.classList.contains("wa-sticker") ? "Sticker" : "📷 Photo";
  el.replaceWith(Object.assign(document.createElement("p"), { className: "wa-gone", textContent: `${what} (WhatsApp no longer has it)` }));
}

function waZoom(src) {
  const box = Object.assign(document.createElement("div"), { className: "wa-zoom" });
  box.innerHTML = `<img src="${esc(src)}" alt="">`;
  const close = e => {
    if (e.type === "keydown" && e.key !== "Escape") return;
    e.stopImmediatePropagation();   // Escape closes the photo, not the card
    box.remove(); removeEventListener("keydown", close, true);
  };
  box.onclick = close;
  addEventListener("keydown", close, true);
  document.body.append(box);
}

async function refreshWaChat(first) {
  if (!waOpen || !$("#waMsgs")) return;
  let c;
  try { c = await (await fetch(`/api/whatsapp/chat?jid=${encodeURIComponent(waOpen)}`)).json(); } catch { return; }
  const name = $("#waName");
  name.textContent = (c.group ? "👥 " : "") + c.name;
  name.dataset.real = c.real;
  if (first) setHTML("#waBellBox", waBell(c));
  if (first) $("#waMote").value = c.name !== c.real ? c.name : "";
  const box = $("#waMsgs"), atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  const players = {};   // audio and photos are kept on refresh (otherwise they would cut off or flicker)
  box.querySelectorAll("audio[data-id], img[data-id]").forEach(a => { players[a.dataset.id] = a; });
  let prev = null, day = "";
  box.innerHTML = c.messages.map(m => {
    const d = new Date(m.time * 1000).toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "short" });
    const who = m.mine ? "·yo·" : m.from;
    let out = d !== day ? `<div class="wa-day">${esc(d)}</div>` : "";
    const same = who === prev && d === day;   // consecutive message: do not repeat the name
    day = d; prev = who;
    out += `<div class="wa-msg ${m.mine ? "mine" : ""} ${same ? "cont" : ""}">
      ${!same && !m.mine && c.group ? `<b>${esc(m.from)}</b>` : ""}${waBody(m)}<small>${esc(waWhen(m.time, true))}</small></div>`;
    return out;
  }).join("") || `<div class="wa-day">No saved messages</div>`;
  box.querySelectorAll("audio[data-id], img[data-id]").forEach(a => {
    const old = players[a.dataset.id];
    if (old) { a.replaceWith(old); return; }
    a.addEventListener("error", () => waGone(a), { once: true });
    if (a.tagName === "IMG") {
      a.onclick = () => waZoom(a.src);
      a.addEventListener("load", () => { if (stick) box.scrollTop = box.scrollHeight; });   // they grow when they load
    }
  });
  const stick = first || atBottom;
  if (stick) box.scrollTop = box.scrollHeight;
}

// ---------- quiet mode ----------
function renderQuiet(on) {
  const chip = $("#quietChip");
  chip.textContent = on ? "🔕 Quiet" : "🔔";
  chip.classList.toggle("on", !!on);
}

// ---------- devices ----------
const DEV_TYPES = { Computer: "computer", Speaker: "speaker", Smartphone: "phone", TV: "TV", CastVideo: "Chromecast" };

async function loadDevices(scan = false) {
  if (!$("#devMusic")) return;
  if (scan) setHTML("#devLan", `<li class="r-empty">Scanning the network…</li>`);
  try {
    const r = await (await fetch(scan ? "/api/devices/scan" : "/api/devices", { method: scan ? "POST" : "GET" })).json();
    renderDevices(r);
  } catch {}
}

function renderDevices(r) {
  setHTML("#devMusic", r.music.map(d => `<li class="pick ${d.active ? "on" : ""}" data-dev="${esc(d.id)}"><span class="dot"></span>
    ${esc(d.name)}<small>${d.active ? "playing here" : esc(DEV_TYPES[d.type] || d.type.toLowerCase())}</small></li>`).join("")
    || `<li class="r-empty">Open Spotify on any device (or say "play music") and it will show up here.</li>`);
  setHTML("#devLan", r.lan.map(d => `<li><span class="dot"></span>${esc(d.name)}<small>${esc(d.kinds.join(" · "))}<br>${esc(d.ip)}</small></li>`).join("")
    || `<li class="r-empty">${r.scanned_at ? "Nothing found" : "Scanning…"}</li>`);
}

function renderCompra(items) {
  if (!$("#compraList")) return;
  items = items || [];
  setHTML("#compraList", items.map((it, i) => `<li data-i="${i}" class="${it.done ? "done" : ""}"><i></i><span>${esc(it.text)}</span></li>`).join("")
    || `<li class="r-empty" style="cursor:default">Empty. Say "Cortex, add milk to the list".</li>`);
  $("#compraClear").hidden = !items.some(i => i.done);
}

async function compraCmd(body) {
  try {
    const r = await (await fetch("/api/compra", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
    if (r.ok) { last.compra = r.items; renderCompra(r.items); } else toast(r.error);
  } catch {}
}

// ---------- reminders ----------
const REPEAT_TEXT = { diario: "every day", laborables: "weekdays", findes: "weekends", semanal: "every week", mensual: "every month" };

async function loadAvisos() {
  try { last.avisos = await (await fetch("/api/avisos")).json(); renderAvisos(last.avisos); } catch {}
}

function renderAvisos(items) {
  const list = $("#avisosList");
  if (!list) return;
  list.innerHTML = items.length ? items.map(a => `<li>
      <span class="when">${esc(a.di)}</span>
      <div><div>${esc(a.texto)}</div>${a.repetir ? `<small>${esc(REPEAT_TEXT[a.repetir] || a.repetir)}</small>` : ""}</div>
      <button class="icon-btn small del" data-id="${esc(a.id)}" title="Delete"><svg><use href="#i-trash"/></svg></button>
    </li>`).join("") : `<li class="empty">No reminders. Say "Cortex, remind me…"</li>`;
  for (const b of $$(".del", list)) {
    b.addEventListener("click", async () => {
      // deleting asks for confirmation: first tap arms, second deletes
      if (!b.classList.contains("armed")) {
        b.classList.add("armed");
        b.title = "Tap again to delete";
        setTimeout(() => b.classList.remove("armed"), 3000);
        return;
      }
      await fetch("/api/avisos/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: b.dataset.id }) });
    });
  }
}

function toggleAvisoForm() {
  const f = $("#avisoForm");
  f.hidden = !f.hidden;
  if (!f.hidden) {
    const d = new Date(Date.now() + 3600_000);
    d.setMinutes(0, 0, 0);
    f.cuando.value = new Date(d - d.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
    f.texto.focus();
  }
}

async function submitAviso(e) {
  e.preventDefault();
  const f = e.target;
  const r = await (await fetch("/api/avisos", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ texto: f.texto.value, cuando: f.cuando.value.replace("T", " "), repetir: f.repetir.value }),
  })).json();
  if (r.error) return toast(r.error);
  f.reset();
  f.hidden = true;
}

// ---------- weather ----------
const WDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
function wIcon(name, day = true) {
  const n = !day && name === "sun" ? "moon" : name;
  return n === "moon" ? "#i-moon" : `#w-${name}`;
}

async function loadWeather() {
  try {
    const w = await (await fetch("/api/weather")).json();
    if (w.ok) { last.weather = w; renderWeather(w); }
  } catch {}
}

function renderWeather(w) {
  const chip = $("#weatherChip");
  chip.hidden = false;
  chip.querySelector("use").setAttribute("href", wIcon(w.icon, w.day));
  chip.querySelector("b").textContent = `${w.temp}°`;
  chip.title = `${w.place}: ${w.text}, feels like ${w.feels}°`;
  $("#ambWeather").innerHTML = `<svg><use href="${wIcon(w.icon, w.day)}"/></svg>${w.temp}° · ${esc(w.text)}`;
  if (!$("#wTemp")) return;
  $("#wPlace").textContent = w.place;
  $("#wIcon use").setAttribute("href", wIcon(w.icon, w.day));
  $("#wTemp").textContent = `${w.temp}°`;
  $("#wText").textContent = `${w.text} · feels like ${w.feels}° · wind ${w.wind} km/h`;
  $("#wHours").innerHTML = w.hours.slice(0, 6).map(h => `<div>${h.hour.slice(0, 2)}h<svg><use href="${wIcon(h.icon, h.day)}"/></svg><b>${h.temp}°</b>${h.rain >= 20 ? `<span class="rain">${h.rain}%</span>` : ""}</div>`).join("");
  $("#wDays").innerHTML = w.days.slice(1, 4).map(d => `<div>${WDAYS[new Date(d.date + "T12:00").getDay()]}<svg><use href="${wIcon(d.icon)}"/></svg><b>${d.max}° <span class="note">${d.min}°</span></b>${d.rain >= 20 ? `<span class="rain">${d.rain}%</span>` : ""}</div>`).join("");
}

// ---------- gestures and camera ----------
function gestureName(id) {
  return id?.startsWith("custom:") ? `"${id.slice(7)}" (custom)` : GESTURES[id] || id;
}

function renderVision(v) {
  if (!v || v.enabled === undefined) return;
  // a single camera button on top: green when it sees you (gestures work), crossed out without a camera
  const cam = $("#cameraBtn");
  cam.classList.toggle("engaged", !!(v.enabled && v.camera_ok && v.engaged));
  cam.classList.toggle("off", !v.enabled || !v.camera_ok);
  cam.title = !v.enabled ? "View camera and gestures (gestures off)" : !v.camera_ok ? "View camera (camera not found)"
    : v.engaged ? "I see you: gestures work · view camera" : "View camera and gestures (look at the screen to use them)";

  const st = $("#gState");
  if (st) {
    st.className = `g-state ${!v.enabled ? "" : v.camera_ok ? (v.engaged ? "on" : "") : "off"}`;
    st.querySelector("span").textContent = !v.enabled ? "Gestures off" : !v.camera_ok ? "Camera not found"
      : v.engaged ? "I see you: make a gesture" : settings.gestures_require_look ? "Look at the screen to activate" : "Waiting for a hand";
    const e = v.last_event;
    $("#gLast").innerHTML = e?.gesture ? `Last: <b>${esc(gestureName(e.gesture))}</b> → ${esc(ACTIONS[e.action] || e.action)}` : "";
  }
  const map = $("#gMap");
  if (map) {
    const actions = settings.gesture_actions || {};
    const ids = [...Object.keys(GESTURES), ...(v.custom || []).map(c => `custom:${c.name || c}`)];
    map.innerHTML = ids.filter(id => actions[id] && actions[id] !== "none")
      .map(id => `<li data-g="${esc(id)}"><span>${esc(gestureName(id))}</span><span>${esc(ACTIONS[actions[id]] || actions[id])}</span></li>`).join("")
      || `<li><span>No gestures assigned</span></li>`;
  }
  if (!$("#cameraModal").hidden) renderCameraSide(v);
  if (!$("#drawer").hidden && JSON.stringify(v.custom) !== JSON.stringify(lastCustom)) renderGestureTable();
  lastCustom = v.custom;
  if (last.status) renderStatus(last.status);
}

function renderCameraSide(v) {
  const rows = [
    ["Camera", v.camera_ok, v.camera_ok ? `${v.fps ?? "?"} fps` : (v.error || "no camera")],
    ["Gaze", v.looking, v.looking ? "looking at the screen" : "not looking"],
    ["Angle", null, v.yaw == null ? "—" : `yaw ${Math.round(v.yaw)}° · pitch ${Math.round(v.pitch)}°`],
    ["Gestures", v.engaged, v.engaged ? "active" : "waiting"],
    ["Hand", v.hand, v.hand ? `${gestureName(v.label) || "—"} ${v.score ? Math.round(v.score * 100) + "%" : ""}` : "none in view"],
  ];
  $("#visionList").replaceChildren(...rows.map(([label, ok, value]) => {
    const li = document.createElement("li");
    li.className = ok === null || ok === undefined ? "" : ok ? "ok" : "bad";
    li.innerHTML = `<i></i><span class="label"></span><span class="value"></span>`;
    li.querySelector(".label").textContent = label;
    li.querySelector(".value").textContent = value;
    return li;
  }));
  const rec = v.recording;
  $("#recBar").hidden = !rec;
  if (rec) $("#recBar span").style.width = `${Math.round((v.progress || 0) * 100)}%`;
  $("#recordCustom").disabled = !!rec;
  const res = v.record_result;
  if (res && res !== lastRecResult && JSON.stringify(res) !== JSON.stringify(lastRecResult)) {
    if (lastRecResult !== undefined) {
      if (res.ok) { toastOk(`Gesture "${res.name}" saved (${res.frames} samples). Assign it an action in Settings → Gestures.`); $("#customName").value = ""; }
      else toast(res.error || "Could not see the hand clearly: try again.");
    }
  }
  lastRecResult = res;
  $("#cameraOff").hidden = v.enabled && v.camera_ok;
}

let lastRecResult;
let lastCustom;
const gestureLog = [];
function onGesture(m) {
  wakeUp();
  const name = gestureName(m.gesture);
  const action = ACTIONS[m.action] || m.action;
  gestureLog.unshift(`<div>${new Date().toLocaleTimeString("en-GB")} · <b>${esc(name)}</b> → ${esc(action)}</div>`);
  gestureLog.length = Math.min(gestureLog.length, 30);
  $("#gestureLog").innerHTML = gestureLog.join("");
  if (m.action === "none") return;
  const flash = $("#gestureFlash");
  flash.querySelector("span").textContent = action;
  flash.hidden = true;
  void flash.offsetWidth;
  flash.hidden = false;
  clearTimeout(flash.timer);
  flash.timer = setTimeout(() => (flash.hidden = true), 1700);
  if (m.action === "ambient") setTimeout(() => showAmbient(true), 400);
}

function openCamera() {
  $("#drawer").hidden = true;
  $("#cameraModal").hidden = false;
  $("#cameraImg").src = `/api/camera.mjpg?t=${Date.now()}`;
  renderCameraSide(vision);
}

function closeCamera() {
  $("#cameraModal").hidden = true;
  $("#cameraImg").removeAttribute("src");   // stops the stream: no more CPU use
}

async function recordCustom() {
  const name = $("#customName").value.trim();
  if (!name) { $("#customName").focus(); return; }
  const r = await (await fetch("/api/gestures/record", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }),
  })).json();
  if (r.error) toast(r.error);
}

function renderGestureTable() {
  const actions = settings.gesture_actions || {};
  const custom = (vision.custom || []).map(c => c.name || c);
  const ids = [...Object.keys(GESTURES), ...custom.map(c => `custom:${c}`)];
  $("#gestureTable").replaceChildren(...ids.map(id => {
    const row = document.createElement("div");
    row.className = "g-row";
    const label = document.createElement("span");
    label.textContent = gestureName(id);
    const sel = document.createElement("select");
    for (const [value, text] of Object.entries(ACTIONS)) sel.add(new Option(text, value));
    sel.value = actions[id] || "none";
    sel.addEventListener("change", () => saveSettings({ gesture_actions: { ...settings.gesture_actions, [id]: sel.value } }));
    row.append(label, sel);
    if (id.startsWith("custom:")) {
      const del = document.createElement("button");
      del.type = "button";
      del.className = "icon-btn small";
      del.title = "Delete gesture";
      del.innerHTML = `<svg><use href="#i-trash"/></svg>`;
      del.addEventListener("click", async () => {
        await fetch("/api/gestures/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: id.slice(7) }) });
      });
      row.append(del);
    } else row.append(document.createElement("span"));
    return row;
  }));
}

// ---------- idle ----------
function wakeUp() {
  lastActivity = Date.now();
  if (!$("#ambient").hidden) showAmbient(false);
}

function showAmbient(on) {
  $("#ambient").hidden = !on;
  if (on) tick();
}

// ---------- edit data (tasks, meals…) ----------
const EDIT_GROUPS = {
  tareas: ["tareas"], horario: ["horario"], betis: ["betis"], gym: ["gym"],
  comidas: ["comidas", "compra"], dinero: ["dinero"], fechas: ["fechas"],
};
const EDIT_LABELS = {
  tareas: "Tasks", horario: "Schedule", betis: "Betis", gym: "Gym", comidas: "Meals",
  compra: "Shopping list", dinero: "Money", fechas: "Dates",
};
let editing = { name: null, original: "" };

async function openEditor(group) {
  const names = EDIT_GROUPS[group] || [group];
  $("#editTabs").innerHTML = "";
  if (names.length > 1) {
    for (const n of names) {
      const b = document.createElement("button");
      b.textContent = EDIT_LABELS[n] || n;
      b.dataset.file = n;
      b.addEventListener("click", () => loadEditFile(n));
      $("#editTabs").append(b);
    }
  }
  $("#editModal").hidden = false;
  await loadEditFile(names[0]);
}

async function loadEditFile(name) {
  if (editing.name && $("#editText").value !== editing.original && !confirmDiscard()) return;
  $("#editError").textContent = "";
  $$("#editTabs button").forEach(b => b.classList.toggle("sel", b.dataset.file === name));
  $("#editModal h2").textContent = "Edit · " + (EDIT_LABELS[name] || name);
  try {
    const r = await (await fetch(`/api/file/${name}`)).json();
    if (!r.ok) throw new Error(r.error);
    editing = { name, original: r.text };
    $("#editText").value = r.text;
    $("#editText").focus();
    $("#editText").setSelectionRange(0, 0);
    $("#editText").scrollTop = 0;
  } catch (e) {
    $("#editError").textContent = "Could not open it: " + e.message;
  }
}

let discardArmed = 0;
function confirmDiscard() {
  // no browser dialogs: a second try within 4 s = discard
  if (Date.now() - discardArmed < 4000) return true;
  discardArmed = Date.now();
  $("#editError").textContent = "You have unsaved changes. Press again to discard them.";
  return false;
}

function closeEditor(force = false) {
  if (!force && $("#editText").value !== editing.original && !confirmDiscard()) return;
  $("#editModal").hidden = true;
  editing = { name: null, original: "" };
}

async function saveEditor() {
  const btn = $("#editSave");
  btn.disabled = true;
  try {
    const r = await (await fetch(`/api/file/${editing.name}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: $("#editText").value }),
    })).json();
    if (!r.ok) { $("#editError").textContent = r.error; return; }
    toastOk(`Saved: ${EDIT_LABELS[editing.name] || editing.name}`);
    closeEditor(true);
    loadData();
  } catch (e) {
    $("#editError").textContent = "Could not save: " + e.message;
  } finally {
    btn.disabled = false;
  }
}

function checkAmbient() {
  const minutes = Number(settings.ambient_minutes || 0);
  if (!minutes || !$("#ambient").hidden) return;
  const busy = document.body.dataset.state !== "idle" || !$("#drawer").hidden || !$("#cameraModal").hidden
    || !$("#editModal").hidden
    || document.body.classList.contains("editing") || vision.engaged;
  if (busy) { lastActivity = Date.now(); return; }
  if (Date.now() - lastActivity > minutes * 60_000) showAmbient(true);
}

// ---------- settings ----------
function modelName(m) {
  return { haiku: "Haiku", sonnet: "Sonnet", opus: "Opus" }[m] || m;
}

let toastTimer;
function toastOk(text) {
  toast(text);
  $("#toast").classList.add("ok");
}

function toast(text) {
  const t = $("#toast");
  t.classList.remove("ok");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 6000);
}

function applySettings(s) {
  if (!s) return;
  settings = s;
  if ($("#quietChip")) renderQuiet(s.quiet_mode);
  const theme = s.theme === "auto"
    ? (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
    : s.theme;
  const root = document.documentElement;
  root.dataset.theme = theme;
  if (s.accent) {
    root.style.setProperty("--accent", s.accent);
    const [r, g, b] = [1, 3, 5].map((i) => parseInt(s.accent.slice(i, i + 2), 16));   // black text on light accents, white on dark ones
    root.style.setProperty("--on-accent", 0.299 * r + 0.587 * g + 0.114 * b > 150 ? "#000" : "#fff");
  }
  root.style.setProperty("--scale", s.text_scale || 1);
  renderVision(vision);
  requestAnimationFrame(fitGrid);
}

let spotifyDevices = [];
async function openSettings() {
  if (spotify.connected) {
    const r = await spotifyCmd("devices");
    spotifyDevices = r.devices || [];
  }
  fillSettings(await (await fetch("/api/settings")).json());
  renderSpotifySettings();
  $("#drawer").hidden = false;
}

// ---------- Settings → Video: saved sites and accounts ----------
async function loadWebAccounts() {
  try {
    const [acc, s] = await Promise.all([fetch("/api/web/accounts").then(r => r.json()), fetch("/api/settings").then(r => r.json()).then(r => r.settings)]);
    $("#accList").innerHTML = acc.map(a => `<li><b>${esc(a.site)}</b><span>${esc(a.user)}</span>
      <button type="button" class="text-btn" data-acc-del="${esc(a.site)}">Remove</button></li>`).join("")
      || `<li class="note">None yet.</li>`;
    $("#webList").innerHTML = (s.video_services || []).map(v => `<li><b>${esc(v.name)}</b><span>${esc(v.domains.join(", "))}</span>
      <button type="button" class="text-btn" data-web-del="${esc(v.name)}">Remove</button></li>`).join("");
    for (const b of $$("[data-acc-del]")) b.onclick = async () => {
      await fetch("/api/web/accounts/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ site: b.dataset.accDel }) });
      loadWebAccounts();
    };
    for (const b of $$("[data-web-del]")) b.onclick = async () => {
      const keep = (s.video_services || []).filter(v => v.name !== b.dataset.webDel);
      await fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ video_services: keep }) });
      loadWebAccounts();
    };
  } catch {}
}

function showTab(name) {
  if (name === "video") loadWebAccounts();
  for (const b of $$("#settingsTabs button")) b.classList.toggle("sel", b.dataset.tab === name);
  for (const t of $$("#settingsForm .tab")) t.hidden = t.dataset.tab !== name;
}

function fillSelect(select, values, current, labels = {}) {
  const opts = [...values];
  if (current && !opts.includes(current)) opts.unshift(current);
  select.replaceChildren(...opts.map(v => {
    const o = document.createElement("option");
    o.value = v ?? "";
    o.textContent = labels[v ?? ""] ?? voiceName(v);
    return o;
  }));
  select.value = current ?? "";
}

function voiceName(v) {
  const m = /^([a-z]{2}-[A-Z]{2})-(\w+?)(Multilingual)?Neural$/.exec(v || "");
  return m ? `${m[2]} (${m[1]}${m[3] ? ", multilingual" : ""})` : v;
}

function fillSettings({ settings: s, devices, options }) {
  applySettings(s);
  const f = $("#settingsForm");
  const def = { "": "Windows default" };
  fillSelect(f.mic_device, ["", ...devices.inputs], s.mic_device ?? "", def);
  fillSelect(f.speaker_device, ["", ...devices.outputs], s.speaker_device ?? "", def);
  fillSelect(f.voice_es, options.voices.es, s.voice_es);
  fillSelect(f.stt_model, options.stt, s.stt_model,
    { parakeet: "Parakeet (fast, recommended)", "whisper-small": "Whisper small (slow)", "whisper-base": "Whisper base" });
  fillSelect(f.claude_model, options.claude, s.claude_model,
    { haiku: "Haiku (fastest)", sonnet: "Sonnet (smarter, slower)", opus: "Opus (smartest, slow)" });
  fillSelect(f.spotify_device, ["", ...(spotifyDevices || [])], s.spotify_device ?? "",
    { "": "This laptop (desktop Spotify)" });
  f.tts_engine.value = s.tts_engine;
  f.tts_rate.value = s.tts_rate;
  $("#rateOut").textContent = `${s.tts_rate > 0 ? "+" : ""}${s.tts_rate}%`;
  for (const name of ["session_idle_minutes", "followup_seconds", "ambient_minutes"]) if (f[name]) f[name].value = s[name] ?? 0;
  for (const name of ["wake_enabled", "gestures_enabled", "gestures_require_look", "window_gestures"]) if (f[name]) f[name].checked = !!s[name];
  for (const seg of f.querySelectorAll(".segmented")) {
    for (const b of seg.children) b.classList.toggle("sel", String(s[seg.dataset.name]) === b.dataset.value);
  }
  $("#accentSwatches").replaceChildren(...ACCENTS.map(c => {
    const b = document.createElement("button");
    b.type = "button";
    b.style.setProperty("--c", c);
    b.classList.toggle("sel", (s.accent || ACCENTS[0]) === c);
    b.addEventListener("click", () => saveSettings({ accent: c }));
    return b;
  }));
  $("#weatherCity").placeholder = s.weather_place?.name || "Madrid";
  renderGestureTable();
}

async function saveSettings(changes) {
  const res = await fetch("/api/settings", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(changes),
  });
  fillSettings(await res.json());
}

function onSettingChange(e) {
  const el = e.target;
  if (!el.name) return;
  let value = el.type === "checkbox" ? el.checked : el.value;
  if (el.type === "number" || el.type === "range") value = Number(value);
  if (el.name.endsWith("_device") && value === "") value = null;
  saveSettings({ [el.name]: value });
}

// ---------- startup ----------
const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

function tick() {
  const now = new Date();
  const time = now.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  $("#clock").textContent = time;
  $("#ambClock").textContent = time;
  const day = DAYS[now.getDay()];
  const date = `${day}, ${now.getDate()} ${MONTHS[now.getMonth()]}`;
  $("#date").textContent = date;
  $("#ambDate").textContent = date;
  const h = now.getHours();
  $("#greeting").textContent = h < 6 ? "Good night" : h < 13 ? "Good morning" : h < 21 ? "Good afternoon" : "Good evening";
}

/** Controls inside the widgets (re-bound whenever cards are added). */
function bindWidgetControls() {
  const bind = (el, ev, fn) => { if (el && !el.dataset.bound) { el.addEventListener(ev, fn); el.dataset.bound = "1"; } };
  bind($("#newChatBtn"), "click", () => send({ action: "new_conversation" }));
  bind($("#composer"), "submit", e => {
    e.preventDefault();
    const input = $("#textInput");
    if (input.value.trim()) send({ action: "text", text: input.value });
    input.value = "";
  });
  for (const b of $$("[data-sp]")) bind(b, "click", () => spotifyCmd(b.dataset.sp));
  bind($("#spVolume"), "change", e => spotifyCmd("volume", Number(e.target.value)));
  for (const b of $$("[data-open-camera]")) bind(b, "click", openCamera);
  bind($("#avisoAddBtn"), "click", toggleAvisoForm);
  bind($("#avisoForm"), "submit", submitAviso);
  bind($("#searchForm"), "submit", doSearch);
  bind($("#searchResults"), "click", e => {
    const a = e.target.closest("[data-video]");
    if (!a) return;
    e.preventDefault();
    showScreen(true);
    videoCmd({ action: "play", id: a.dataset.video, title: a.dataset.title, rect: screenRect() });
  });
  bind($("#videoCtrl"), "click", e => {
    const b = e.target.closest("[data-v]");
    if (!b) return;
    if (b.dataset.v === "dock") { showScreen(true); lastRect = ""; }
    videoCmd({ action: b.dataset.v, value: b.dataset.val ? Number(b.dataset.val) : undefined,
               rect: b.dataset.v === "dock" ? screenRect() : undefined });
  });
  bind($("#videoApps"), "click", async e => {
    const b = e.target.closest("[data-app]");
    if (!b) return;
    showScreen(true);
    const r = await (await fetch("/api/video", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "open", url: b.dataset.app, rect: screenRect() }) })).json();
    if (!r.ok) toast(r.error);
  });
  loadVideoServices();
  bind($("#timerQuick"), "click", e => {
    const b = e.target.closest("[data-min]");
    if (b) timerCmd({ seconds: Number(b.dataset.min) * 60 });
  });
  bind($("#timerList"), "click", e => {
    const b = e.target.closest("[data-cancel-timer]");
    if (b) timerCmd({ cancel: b.dataset.cancelTimer });
  });
  bind($("#compraForm"), "submit", e => {
    e.preventDefault();
    const v = $("#compraInput").value.trim();
    if (v) compraCmd({ add: v });
    $("#compraInput").value = "";
  });
  bind($("#compraList"), "click", e => {
    const li = e.target.closest("li[data-i]");
    if (li) compraCmd({ toggle: Number(li.dataset.i) });
  });
  bind($("#compraClear"), "click", () => compraCmd({ clear_done: true }));
  bind($("#devScan"), "click", () => loadDevices(true));
  bind($("#tasksList"), "click", onTaskClick);
  bind($("#taskAddBtn"), "click", addTask);
  bind($("#mailAccounts"), "click", () => { mailLogin = true; $("#mailList").replaceChildren(); renderMail(last.mail || {}); });
  bind($("#mailCheck"), "click", e => reloadBtn(e.currentTarget, "/api/mail/check", m => { last.mail = m; renderMail(m); }));
  bind($("#waCheck"), "click", e => reloadBtn(e.currentTarget, "/api/whatsapp/check", m => { last.whatsapp = m; renderWhatsApp(m); }));
  bind($("#waList"), "click", e => {
    const bell = e.target.closest("[data-mute]");
    if (bell) { e.stopPropagation(); waToggleMute(bell); return; }
    const li = e.target.closest("[data-chat]");
    if (li) openWaChat(li.dataset.chat);   // the card opens full size and shows the chat
  });
  bind($("#mailList"), "click", e => {
    const li = e.target.closest("[data-mail]");
    if (li) openMail(li.dataset.mail);   // the card opens full size with the whole email
  });
  bind($("#devMusic"), "click", async e => {
    const li = e.target.closest("[data-dev]");
    if (!li) return;
    const r = await (await fetch("/api/devices/music", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: li.dataset.dev }) })).json();
    if (!r.ok) toast(r.error);
    setTimeout(loadDevices, 1200);
  });
}

async function toggleFullscreen() {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen();
  } catch {}
}

async function init() {
  await buildGrid();
  bindWidgetControls();
  tick();
  setInterval(tick, 5_000);
  setInterval(checkAmbient, 10_000);

  $("#talkBtn").addEventListener("click", () => {
    const busy = ["thinking", "speaking", "transcribing"].includes(document.body.dataset.state);
    send({ action: busy ? "stop" : "press" });
  });
  $("#wakeBtn").addEventListener("click", () => send({ action: "wake_toggle" }));
  $("#editBtn").addEventListener("click", () => setEditing(!document.body.classList.contains("editing")));
  $("#doneEdit").addEventListener("click", () => setEditing(false));
  $("#resetLayout").addEventListener("click", resetLayout);
  $("#openEditor").addEventListener("click", () => { $("#drawer").hidden = true; setEditing(true); });
  $("#settingsBtn").addEventListener("click", openSettings);
  $("#cameraBtn").addEventListener("click", openCamera);
  $("#weatherChip").addEventListener("click", e => {
    e.stopPropagation();
    const pop = $("#weatherPop"), r = e.currentTarget.getBoundingClientRect();
    pop.style.top = `${r.bottom + 10}px`;
    pop.style.left = `${Math.max(12, Math.min(r.left + r.width / 2 - 210, innerWidth - 432))}px`;
    pop.hidden = !pop.hidden;
  });
  $("#weatherPop").addEventListener("click", e => e.stopPropagation());
  document.addEventListener("click", () => { $("#weatherPop").hidden = true; });
  $("#accSave").addEventListener("click", async () => {
    const body = { site: $("#accSite").value.trim(), user: $("#accUser").value.trim(), password: $("#accPass").value };
    const r = await (await fetch("/api/web/accounts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
    $("#accMsg").textContent = r.ok ? `Account for ${r.site} saved.` : r.error;
    if (r.ok) { $("#accPass").value = ""; $("#accUser").value = ""; $("#accSite").value = ""; loadWebAccounts(); }
  });
  $("#morningChip").addEventListener("click", () => send({ action: "text", text: "Give me my morning summary." }));
  $("#fullBtn").addEventListener("click", toggleFullscreen);
  $("#closeDrawer").addEventListener("click", () => ($("#drawer").hidden = true));
  $("#cameraModal").addEventListener("click", e => { if (e.target === e.currentTarget || e.target.closest("[data-close]")) closeCamera(); });
  $("#recordCustom").addEventListener("click", recordCustom);
  $("#ambient").addEventListener("click", () => { wakeUp(); });
  for (const ev of ["pointerdown", "keydown"]) addEventListener(ev, () => (lastActivity = Date.now()), true);
  addEventListener("fullscreenchange", () => requestAnimationFrame(fitGrid));
  $("#settingsTabs").addEventListener("click", e => { const b = e.target.closest("button"); if (b) showTab(b.dataset.tab); });

  const form = $("#settingsForm");
  form.addEventListener("change", onSettingChange);
  form.tts_rate.addEventListener("input", e => {
    const v = Number(e.target.value);
    $("#rateOut").textContent = `${v > 0 ? "+" : ""}${v}%`;
  });
  for (const seg of form.querySelectorAll(".segmented")) {
    seg.addEventListener("click", e => {
      const b = e.target.closest("button");
      if (!b) return;
      const raw = b.dataset.value;
      saveSettings({ [seg.dataset.name]: /^[\d.]+$/.test(raw) ? Number(raw) : raw });
    });
  }
  for (const b of form.querySelectorAll("[data-test]")) {
    b.addEventListener("click", e => {
      e.preventDefault();
      fetch("/api/voice/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ lang: b.dataset.test }) });
    });
  }
  $("#weatherSave").addEventListener("click", async () => {
    const name = $("#weatherCity").value.trim();
    if (!name) return;
    const r = await (await fetch("/api/weather/place", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) })).json();
    if (r.error) toast(r.error);
    else { $("#weatherCity").value = ""; last.weather = r; renderWeather(r); fillSettings(await (await fetch("/api/settings")).json()); }
  });
  $("#enrollBtn").addEventListener("click", () => send({ action: "enroll" }));
  $("#retrainBtn").addEventListener("click", () => fetch("/api/wake/retrain", { method: "POST" }));
  $("#spLogout").addEventListener("click", () => spotifyCmd("logout"));
  $("#enrollClear").addEventListener("click", () => send({ action: "enroll_clear" }));
  $("#refreshDevices").addEventListener("click", async () => {
    fillSettings(await (await fetch("/api/devices/refresh", { method: "POST" })).json());
  });
  document.addEventListener("click", e => {
    const b = e.target.closest("[data-edit]");
    if (b) openEditor(b.dataset.edit);
  });
  $("#editModal").addEventListener("click", e => {
    if (e.target === e.currentTarget || e.target.closest("[data-close]")) closeEditor();
  });
  $("#editSave").addEventListener("click", saveEditor);
  $("#editText").addEventListener("keydown", e => {
    if (e.key === "Tab") {   // Tab = two spaces (YAML does not allow tabs)
      e.preventDefault();
      e.target.setRangeText("  ", e.target.selectionStart, e.target.selectionEnd, "end");
    }
    if (e.key === "s" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); saveEditor(); }
  });

  document.addEventListener("keydown", e => {
    if (e.code === "Space" && !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) {
      e.preventDefault();
      $("#talkBtn").click();
    }
    if (e.key === "Escape") {
      if (!$("#ambient").hidden) showAmbient(false);
      else if (!$("#editModal").hidden) closeEditor();
      else if (!$("#cameraModal").hidden) closeCamera();
      else if (!$("#drawer").hidden) $("#drawer").hidden = true;
      else if (document.body.classList.contains("editing")) setEditing(false);
      else if (focused) return;   // setFocus already closes it
      else send({ action: "stop" });
    }
    if (e.key === "F11") { e.preventDefault(); toggleFullscreen(); }
  });
  connect();
  loadData();
  setInterval(loadBank, 30 * 60_000);   // served from the server cache: no bank queries used
  loadTimers();
  loadCompra();
  loadDevices();
  setInterval(loadDevices, 60_000);
  loadMail();
  $("#quietChip").addEventListener("click", async () => {
    const r = await (await fetch("/api/quiet", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ on: !settings.quiet_mode }) })).json();
    settings.quiet_mode = r.quiet;
    renderQuiet(r.quiet);
  });
  setInterval(renderTimers, 1000);
  setInterval(loadCompra, 5 * 60_000);    // in case Cortex (Claude) changes it by editing the file
  const editHash = location.hash.match(/^#edit=(\w+)/);   // e.g. /#edit=tareas opens the editor
  if (editHash) openEditor(editHash[1]);
  loadWeather();
  loadAvisos();
  setInterval(loadData, 5 * 60_000);
  setInterval(loadWeather, 15 * 60_000);
}

init();
