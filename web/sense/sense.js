// Cortex Sense panel: live state over /ws, settings over /api. No framework, no build step.
const $ = (s) => document.querySelector(s);
const post = async (url, body = {}) =>
  (await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();

const NAMES = {
  thumb_up: "Thumb up", thumb_down: "Thumb down", open_palm: "Open palm", closed_fist: "Fist", victory: "Victory ✌",
  pointing_up: "Index up", i_love_you: "I love you 🤟", ok: "OK 👌", shaka: "Shaka 🤙", rock: "Rock 🤘", three: "Three fingers",
  shush: "Shush (finger on lips)", swipe_left: "Swipe left", swipe_right: "Swipe right", swipe_up: "Swipe up",
  swipe_down: "Swipe down", flick_right: "Flick right", flick_left: "Flick left", wag: "Finger wag (no)",
  circle_cw: "Circle clockwise", circle_ccw: "Circle anticlockwise", wave: "Wave", push: "Push towards camera",
  both_palms: "Both palms", both_thumbs_up: "Both thumbs up", both_thumbs_down: "Both thumbs down", both_fists: "Both fists",
  hands_apart: "Hands apart", hands_together: "Hands together",
};
const HUB = {
  next: "Next track", previous: "Previous track", toggle: "Play / pause", volume_up: "Volume up", volume_down: "Volume down",
  listen: "Start listening", stop: "Stop the assistant", quiet: "Mute the assistant", ambient: "Ambient clock",
  wake_toggle: "Wake word on/off", new_chat: "New conversation", theme: "Light / dark theme",
};
const KINDS = { none: "Nothing", hub: "Hub action", media: "Media key", keys: "Shortcut", run: "Open program", window: "Window" };

let settings = {}, catalog = null, vision = {}, lastFired = null;

// ---------- mapping table ----------
function split(action) {
  if (!action || action === "none") return ["none", ""];
  if (!action.includes(":")) return ["hub", action];
  const i = action.indexOf(":");
  return [action.slice(0, i), action.slice(i + 1)];
}

function paramField(kind, value) {
  const opts = kind === "hub" ? HUB : kind === "media" ? Object.fromEntries(catalog.media.map((m) => [m, m.replace("_", " ")]))
    : kind === "window" ? Object.fromEntries(catalog.window.map((w) => [w, w])) : null;
  if (opts) {
    const sel = document.createElement("select");
    for (const [v, t] of Object.entries(opts)) sel.add(new Option(t, v));
    sel.value = value in opts ? value : Object.keys(opts)[0];
    return sel;
  }
  const inp = document.createElement("input");
  inp.value = value;
  inp.placeholder = kind === "keys" ? "ctrl+shift+m" : kind === "run" ? "notepad or https://…" : "";
  inp.disabled = kind === "none";
  return inp;
}

async function save(id, action) {
  const r = await post("/api/settings", { gesture_actions: { [id]: action } });
  $("#mapErr").hidden = !r.error;
  $("#mapErr").textContent = r.error || "";
  if (r.settings) settings = r.settings;
}

function renderTable() {
  if (!catalog) return;
  const actions = settings.gesture_actions || {};
  const custom = (vision.custom || []).map((c) => `custom:${c.name}`);
  const f = $("#filter").value.toLowerCase();
  const rows = [...catalog.gestures, ...custom].filter((id) => !f || (NAMES[id] || id).toLowerCase().includes(f));
  $("#table").replaceChildren(...rows.map((id) => {
    const row = document.createElement("div");
    row.className = "trow";
    row.dataset.g = id;
    const name = document.createElement("div");
    name.className = "g";
    name.textContent = NAMES[id] || id.replace(/^custom:/, "");
    const tag = document.createElement("small");
    tag.textContent = id.startsWith("custom:") ? "your gesture" : catalog.motions.includes(id) ? "motion"
      : catalog.two_hands.includes(id) ? "two hands" : "pose";
    name.append(tag);
    let [kind, value] = split(actions[id]);
    const kindSel = document.createElement("select");
    for (const [v, t] of Object.entries(KINDS)) kindSel.add(new Option(t, v));
    kindSel.value = kind;
    let param = paramField(kind, value);
    const current = () => kindSel.value === "none" ? "none" : kindSel.value === "hub" ? param.value : `${kindSel.value}:${param.value.trim()}`;
    const commit = () => { if (kindSel.value === "none" || param.value.trim()) save(id, current()); };
    kindSel.addEventListener("change", () => {
      const fresh = paramField(kindSel.value, "");
      param.replaceWith(fresh);
      param = fresh;
      param.addEventListener("change", commit);
      commit();
    });
    param.addEventListener("change", commit);
    const btns = document.createElement("div");
    const test = document.createElement("button");
    test.className = "ghost";
    test.textContent = "Try";
    test.title = "Run this command now";
    test.addEventListener("click", async () => {
      const r = await post("/api/action/test", { action: current() });
      $("#mapErr").hidden = !r.error;
      $("#mapErr").textContent = r.error || "";
    });
    btns.append(test);
    if (id.startsWith("custom:")) {
      const del = document.createElement("button");
      del.className = "ghost";
      del.textContent = "✕";
      del.title = "Delete this gesture";
      del.addEventListener("click", () => post("/api/gestures/delete", { name: id.slice(7) }));
      btns.append(del);
    }
    row.append(name, kindSel, param, btns);
    return row;
  }));
}

// ---------- live state ----------
function renderVision(v) {
  const customChanged = JSON.stringify(v.custom) !== JSON.stringify(vision.custom);
  vision = v;
  const cam = $("#pCam");
  cam.textContent = !v.enabled ? "gestures off" : v.camera_ok ? `camera · ${Math.round(v.cam_fps || 0)} fps` : "no camera";
  cam.className = `pill ${v.enabled && v.camera_ok ? "on" : v.enabled ? "off" : ""}`;
  $("#pHub").textContent = v.hub_connected ? "Hub connected" : "Hub not running";
  $("#pHub").className = `pill ${v.hub_connected ? "on" : ""}`;
  $("#rStatus").textContent = v.error || (!v.enabled ? "Gestures are off" : v.engaged ? "Watching for gestures"
    : settings.gestures_require_look ? "Look at the screen to start" : "Waiting for a hand");
  $("#rHand").textContent = v.hand ? `${NAMES[v.label] || v.label || "—"} ${v.score ? Math.round(v.score * 100) + " %" : ""}` : "none";
  const e = v.last_event;
  $("#rLast").textContent = e?.gesture ? `${NAMES[e.gesture] || e.gesture} → ${HUB[e.action] || e.action}` : "—";
  if (v.recording) $("#cProg").style.width = `${v.progress * 100}%`;
  const res = v.record_result;
  $("#cMsg").textContent = v.recording ? `Recording “${v.recording}”… hold the pose` : res
    ? (res.ok ? `Saved “${res.name}” (${res.frames} samples). Give it a command above.` : `Could not record: ${res.error}`) : "";
  if (customChanged) renderTable();
}

function flash(gesture) {
  document.querySelectorAll(".trow.fired").forEach((r) => r.classList.remove("fired"));
  const row = document.querySelector(`.trow[data-g="${CSS.escape(gesture)}"]`);
  if (row) row.classList.add("fired");
  clearTimeout(lastFired);
  lastFired = setTimeout(() => row?.classList.remove("fired"), 1500);
}

function renderSettings() {
  $("#enabled").checked = !!settings.gestures_enabled;
  for (const el of document.querySelectorAll("[data-k]")) {
    const v = settings[el.dataset.k];
    if (el.type === "checkbox") el.checked = !!v; else if (document.activeElement !== el) el.value = v;
  }
  const c = settings.calibration || {};
  $("#calib").textContent = c.time ? `Calibrated ${c.time}` + (c.fps ? ` · ${c.fps} fps` : "") +
    (c.hand_size ? ` · hand ${c.hand_size}` : "") + (c.pinch ? ` · pinch ${c.pinch[0]}–${c.pinch[1]} px` : "") : "Not calibrated yet";
}

function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.type === "vision") renderVision(msg);
    else if (msg.type === "settings") { settings = msg.settings; renderSettings(); renderTable(); }
    else if (msg.type === "gesture") flash(msg.gesture);
  };
  ws.onclose = () => { $("#pCam").textContent = "Sense not responding"; setTimeout(connect, 2000); };
}

// ---------- wizard ----------
const STEPS = [null, ["look", 3], ["hand", 3], ["pinch", 4]];
let step = 0;

function showStep(n) {
  step = n;
  document.querySelectorAll("[data-step]").forEach((d) => (d.hidden = +d.dataset.step !== n));
  document.querySelectorAll("#steps li").forEach((li, i) => li.classList.toggle("now", i === n));
  $("#wNext").textContent = n === 0 ? "Camera works" : n === 4 ? "Done" : "Start";
  $("#wSkip").hidden = n === 4;
  $("#wProg").style.width = "0";
}

function openWizard() {
  $("#wizard").hidden = false;
  $("#wcam").src = `/api/camera.mjpg?t=${Date.now()}`;
  $("#wIndex").value = settings.camera_index ?? 0;
  if (!settings.gestures_enabled) post("/api/settings", { gestures_enabled: true });
  showStep(0);
}

function closeWizard() {
  $("#wizard").hidden = true;
  $("#wcam").removeAttribute("src");
}

async function runStep() {
  const [name, secs] = STEPS[step];
  $("#wNext").disabled = true;
  const t0 = Date.now();
  const tick = setInterval(() => ($("#wProg").style.width = `${Math.min(100, (Date.now() - t0) / secs / 10)}%`), 100);
  const r = await post("/api/calibrate", { step: name, seconds: secs });
  clearInterval(tick);
  $("#wNext").disabled = false;
  if (r.error) return alertIn(r.error);
  if (name !== "look" && r.with_hand < 5) return alertIn("I couldn't see your hand. Try again with your hand in view.");
  if (step < 3) return showStep(step + 1);
  const res = await post("/api/calibrate/apply");
  const rep = res.report || {};
  const lines = [
    rep.fps && `Camera: ${rep.fps} fps, light ${rep.brightness}/255`,
    rep.pitch != null && `“Looking at the screen” centred at ${rep.pitch}° head tilt`,
    rep.hand_size && `Hand size ${rep.hand_size} → swipe distance ${res.changes.swipe_min_distance}`,
    rep.side && `Hand seen: ${rep.side.toLowerCase()}`,
    rep.pinch && `Pinch ${rep.pinch[0]}–${rep.pinch[1]} px → grab at ${res.changes.pinch_grab}, release at ${res.changes.pinch_release}`,
    ...(rep.notes || []).map((n) => `⚠ ${n}`),
  ].filter(Boolean);
  $("#report").replaceChildren(...lines.map((t) => Object.assign(document.createElement("li"), { textContent: t })));
  showStep(4);
}

function alertIn(text) {
  const p = document.querySelector(`[data-step="${step}"] .muted`) || document.querySelector(`[data-step="${step}"]`);
  let m = p.querySelector(".error");
  if (!m) { m = Object.assign(document.createElement("p"), { className: "error" }); p.append(m); }
  m.textContent = text;
}

$("#wNext").addEventListener("click", () => {
  if (step === 0) return showStep(1);
  if (step === 4) return closeWizard();
  runStep();
});
$("#wSkip").addEventListener("click", async () => { await post("/api/settings", { setup_done: true }); closeWizard(); });
$("#wIndex").addEventListener("change", async (e) => {
  await post("/api/settings", { camera_index: +e.target.value });
  $("#wCamMsg").textContent = "Switching camera…";
  setTimeout(() => { $("#wcam").src = `/api/camera.mjpg?t=${Date.now()}`; $("#wCamMsg").textContent = ""; }, 1500);
});
$("#recal").addEventListener("click", openWizard);

// ---------- controls ----------
$("#enabled").addEventListener("change", (e) => post("/api/settings", { gestures_enabled: e.target.checked }));
for (const el of document.querySelectorAll("[data-k]")) {
  el.addEventListener("change", () => post("/api/settings", { [el.dataset.k]: el.type === "checkbox" ? el.checked : +el.value }));
}
$("#filter").addEventListener("input", renderTable);
$("#cRec").addEventListener("click", async () => {
  const name = $("#cName").value.trim();
  if (!name) return $("#cName").focus();
  const r = await post("/api/gestures/record", { name, seconds: 3 });
  if (r.error) $("#cMsg").textContent = r.error;
});

(async () => {
  [settings, catalog] = await Promise.all([fetch("/api/settings").then((r) => r.json()), fetch("/api/catalog").then((r) => r.json())]);
  renderSettings();
  renderTable();
  $("#cam").src = "/api/camera.mjpg";
  connect();
  if (!settings.setup_done) openWizard();
})();
