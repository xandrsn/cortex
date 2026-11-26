// Cortex Sense web demo: your camera, the 21 hand points MediaPipe finds, the gesture they make and the action it is mapped to.
// Everything runs in this tab; no frame leaves it. classify() and swipe() do not touch the DOM and are tested with node.

const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);

/** One hand's 21 landmarks (image coordinates, y down) → pose name, or "none". */
export function classify(lm) {
  const palm = dist(lm[0], lm[9]) || 1;
  const out = (tip, pip) => dist(lm[tip], lm[0]) > dist(lm[pip], lm[0]) * 1.15;
  const [i, m, r, p] = [out(8, 6), out(12, 10), out(16, 14), out(20, 18)];
  const thumb = dist(lm[4], lm[5]) > palm * 0.6;   // thumb tip well away from the index knuckle
  if (dist(lm[4], lm[8]) / palm < 0.35 && (m || r || p)) return "ok";   // a fist also brings thumb and index together
  if (i && m && r && p) return "palm";
  if (i && m && r && !p) return "three";
  if (i && m && !r && !p) return "peace";
  if (i && !m && !r && p) return thumb ? "love" : "rock";
  if (!i && !m && !r && p && thumb) return "shaka";
  if (i && !m && !r && !p) return "point";
  if (!i && !m && !r && !p) {
    const knuckles = [5, 9, 13, 17].map((k) => lm[k].y);
    if (thumb && lm[4].y < Math.min(...knuckles) - palm * 0.4) return "thumb_up";
    if (thumb && lm[4].y > Math.max(...knuckles) + palm * 0.4) return "thumb_down";
    return "fist";
  }
  return "none";
}

/** Wrist track [{t, x, y}] (camera image, 0..1, not mirrored) → swipe from the user's side, or "". */
export function swipe(track, ms = 450, far = 0.22) {
  const end = track.at(-1);
  const start = end && track.find((s) => end.t - s.t <= ms);
  if (!start) return "";
  const dx = end.x - start.x, dy = end.y - start.y;
  if (Math.abs(dx) > far && Math.abs(dx) > 2 * Math.abs(dy)) return dx < 0 ? "swipe_right" : "swipe_left";   // the image is not mirrored
  if (Math.abs(dy) > far && Math.abs(dy) > 2 * Math.abs(dx)) return dy < 0 ? "swipe_up" : "swipe_down";
  return "";
}

export const GESTURES = {
  palm: ["✋", "Open palm"], fist: ["✊", "Fist"], peace: ["✌️", "Victory"], three: ["3️⃣", "Three fingers"], point: ["☝️", "Index up"],
  thumb_up: ["👍", "Thumb up"], thumb_down: ["👎", "Thumb down"], ok: ["👌", "OK"], rock: ["🤘", "Rock"], love: ["🤟", "I love you"],
  shaka: ["🤙", "Shaka"], swipe_left: ["👈", "Swipe left"], swipe_right: ["👉", "Swipe right"], swipe_up: ["👆", "Swipe up"],
  swipe_down: ["👇", "Swipe down"], both_palms: ["🙌", "Both palms"],
};

// From simple to developer: the same kinds of action the app offers
export const ACTIONS = {
  none: ["—", "Nothing"],
  "window:minimize_all": ["Windows", "Minimise everything"], "window:desktop": ["Windows", "Show the desktop"],
  "window:maximize": ["Windows", "Maximise this window"], "window:minimize": ["Windows", "Minimise this window"],
  "window:switch": ["Windows", "Switch window"], "window:lock": ["Windows", "Lock the PC"],
  "media:play_pause": ["Music", "Play / pause"], "media:next": ["Music", "Next track"], "media:previous": ["Music", "Previous track"],
  "media:volume_up": ["Music", "Volume up"], "media:volume_down": ["Music", "Volume down"], "media:mute": ["Music", "Mute"],
  "run:spotify": ["App", "Open Spotify"], "run:https://calendar.google.com": ["Website", "Open my calendar"],
  "keys:win+shift+s": ["Shortcut", "Win+Shift+S (screenshot)"], "keys:ctrl+shift+m": ["Shortcut", "Ctrl+Shift+M (mute in a call)"],
  "script:focus.ps1": ["Script", "focus.ps1 (close chats, start a timer)"], "script:deploy.py --staging": ["Script", "deploy.py --staging"],
};

export const DEFAULTS = {
  swipe_left: "media:next", swipe_right: "media:previous", swipe_up: "window:maximize", swipe_down: "window:minimize",
  palm: "media:play_pause", thumb_up: "media:volume_up", thumb_down: "media:volume_down", both_palms: "window:minimize_all",
  peace: "window:desktop", three: "window:switch", point: "run:https://calendar.google.com", ok: "keys:ctrl+shift+m",
  rock: "script:focus.ps1", love: "keys:win+shift+s", shaka: "run:spotify", fist: "none",
};

if (typeof document !== "undefined") start();

function start() {
  const $ = (s) => document.querySelector(s);
  const video = $("#cam"), canvas = $("#overlay"), ctx = canvas.getContext("2d"), button = $("#camBtn"), status = $("#status");
  const map = { ...DEFAULTS };
  try { Object.assign(map, JSON.parse(localStorage.getItem("sense.map") || "{}")); } catch { /* private window: defaults */ }

  // ---------- mapping table ----------
  const rows = {};
  for (const [id, [emoji, name]] of Object.entries(GESTURES)) {
    const row = document.createElement("li");
    const sel = document.createElement("select");
    sel.setAttribute("aria-label", `Action for ${name}`);
    for (const [a, [kind, label]] of Object.entries(ACTIONS)) sel.add(new Option(a === "none" ? label : `${kind} · ${label}`, a));
    sel.value = map[id] in ACTIONS ? map[id] : "none";
    sel.addEventListener("change", () => { map[id] = sel.value; try { localStorage.setItem("sense.map", JSON.stringify(map)); } catch { /* ok */ } });
    const g = document.createElement("span");
    g.className = "g";
    g.innerHTML = `<i aria-hidden="true">${emoji}</i>`;
    g.append(name);
    row.append(g, sel);
    $("#map").append(row);
    rows[id] = row;
  }

  // ---------- firing ----------
  const log = $("#log");
  function fire(id) {
    const action = map[id] || "none";
    const [kind, label] = ACTIONS[action];
    rows[id].classList.remove("fired"); void rows[id].offsetWidth; rows[id].classList.add("fired");
    $("#bigG").textContent = `${GESTURES[id][0]} ${GESTURES[id][1]}`;
    $("#bigA").textContent = action === "none" ? "not mapped" : `→ ${label}`;
    $("#big").classList.remove("pop"); void $("#big").offsetWidth; $("#big").classList.add("pop");
    if (action === "none") return;
    const li = document.createElement("li");
    li.innerHTML = `<time>${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</time> `;
    li.append(`${GESTURES[id][1]} → ${kind}: ${label}`);
    const code = document.createElement("code");
    code.textContent = action;
    li.append(" ", code);
    log.prepend(li);
    while (log.children.length > 8) log.lastChild.remove();
  }

  // ---------- drawing ----------
  const BONES = [[0, 1], [1, 2], [2, 3], [3, 4], [0, 5], [5, 6], [6, 7], [7, 8], [5, 9], [9, 10], [10, 11], [11, 12], [9, 13], [13, 14], [14, 15], [15, 16], [13, 17], [0, 17], [17, 18], [18, 19], [19, 20]];
  function draw(hands, pose) {
    const w = video.videoWidth, h = video.videoHeight;
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }   // resizing reallocates the canvas: only when it changes
    ctx.clearRect(0, 0, w, h);
    for (const lm of hands) {
      ctx.lineWidth = Math.max(2, w / 220); ctx.strokeStyle = "#6ea8fe"; ctx.beginPath();
      for (const [a, b] of BONES) { ctx.moveTo(lm[a].x * w, lm[a].y * h); ctx.lineTo(lm[b].x * w, lm[b].y * h); }
      ctx.stroke();
      lm.forEach((p, k) => {
        ctx.fillStyle = [4, 8, 12, 16, 20].includes(k) ? "#f2d45c" : "#fff";
        ctx.beginPath(); ctx.arc(p.x * w, p.y * h, Math.max(3, w / 160), 0, 7); ctx.fill();
      });
    }
    $("#pose").textContent = pose === "none" ? (hands.length ? "hand found, no gesture" : "no hand") : `${GESTURES[pose][0]} ${GESTURES[pose][1]}`;
  }

  // ---------- camera loop ----------
  const HOLD = 450, COOLDOWN = 900;   // ms a pose must stay; ms before the next gesture (the app has the same two settings)
  let stream = null, landmarker = null, raf = 0, busy = false, lastT = -1, track = [], seen = "none", since = 0, done = false, quietUntil = 0, frames = 0, fpsAt = 0;
  function stop(text = "Camera off.") {
    cancelAnimationFrame(raf);
    stream?.getTracks().forEach((t) => t.stop());
    stream = null; video.srcObject = null;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    button.textContent = "Turn on the camera"; status.textContent = text;
    document.body.classList.remove("live");
  }
  async function begin() {
    status.textContent = "Loading the hand model…";
    if (!landmarker) {
      const mp = await import("https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@1.2.3/vision_bundle.mjs");
      const files = await mp.FilesetResolver.forVisionTasks("https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@1.2.3/wasm");
      const opts = (delegate) => ({ runningMode: "VIDEO", numHands: 2, baseOptions: { delegate,
        modelAssetPath: "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task" } });
      // the GPU (WebGL) is several times faster; fall back to the CPU where WebGL is missing
      landmarker = await mp.HandLandmarker.createFromOptions(files, opts("GPU")).catch(() => mp.HandLandmarker.createFromOptions(files, opts("CPU")));
    }
    stream = await navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480, frameRate: { ideal: 60 } } });
    video.srcObject = stream;
    await video.play();
    button.textContent = "Turn off the camera"; status.textContent = "Show your hand to the camera.";
    document.body.classList.add("live");
    loop();
  }
  function loop() {
    if (!stream) return;
    raf = requestAnimationFrame(loop);
    if (video.currentTime === lastT) return;   // same camera frame as last time: nothing new to detect
    lastT = video.currentTime;
    const now = performance.now();
    const hands = landmarker.detectForVideo(video, now).landmarks;
    frames++;
    if (now - fpsAt > 1000) { $("#fps").textContent = `${frames} fps`; frames = 0; fpsAt = now; }
    const poses = hands.map(classify);
    const pose = hands.length === 2 && poses.every((p) => p === "palm") ? "both_palms" : poses[0] || "none";
    draw(hands, pose);
    if (now < quietUntil) { track = []; return; }
    if (hands.length === 1) {
      track.push({ t: now, x: hands[0][0].x, y: hands[0][0].y });
      track = track.filter((s) => now - s.t < 600);
      const s = swipe(track);
      if (s) { fire(s); track = []; quietUntil = now + COOLDOWN; seen = "none"; return; }
    } else track = [];
    if (pose !== seen) { seen = pose; since = now; done = false; }
    $("#hold").style.width = pose === "none" || done ? "0%" : `${Math.min(100, (now - since) / HOLD * 100)}%`;
    if (!done && pose !== "none" && now - since >= HOLD) { done = true; fire(pose); quietUntil = now + COOLDOWN; }
  }
  button.addEventListener("click", async () => {
    if (stream) return stop();
    if (busy) return;
    busy = true;
    try { await begin(); } catch (e) { stop(e.name === "NotAllowedError" ? "No camera permission." : "The camera could not be used in this browser."); }
    busy = false;
  });
  $("#camStart").addEventListener("click", () => button.click());
  $("#reset").addEventListener("click", () => {
    Object.assign(map, DEFAULTS);
    for (const [id, row] of Object.entries(rows)) row.querySelector("select").value = map[id] || "none";
    try { localStorage.removeItem("sense.map"); } catch { /* ok */ }
  });
  addEventListener("pagehide", () => stop());
  document.addEventListener("visibilitychange", () => { if (document.hidden && stream) stop(); });
}
