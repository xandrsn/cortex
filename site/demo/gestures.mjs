// Escritorio de ejemplo que se mueve con la mano. La cámara es opcional y se procesa en el navegador: no sale ningún vídeo.
// La lógica (classify, snapZone) no toca el DOM y se prueba con node.

const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);

export function classify(lm) {
  const out = (tip, pip) => dist(lm[tip], lm[0]) > dist(lm[pip], lm[0]) * 1.15;
  const [i, m, r, p] = [out(8, 6), out(12, 10), out(16, 14), out(20, 18)];
  if (dist(lm[4], lm[8]) / (dist(lm[0], lm[9]) || 1) < 0.35 && (m || r || p)) return "pinch";   // un puño también junta pulgar e índice
  if (i && m && r && p) return "palm";
  if (i && m && !r && !p) return "peace";
  if (!i && !m && !r && !p) return "fist";
  return "none";
}

/** Dónde encaja la ventana al soltarla (x, y de 0 a 1). Solo cerca de los bordes. */
export function snapZone(x, y) {
  if (y < 0.06) return "full";
  if (x < 0.08) return "left";
  if (x > 0.92) return "right";
  return "";
}

export const ZONES = { left: [0, 0, 50, 100], right: [50, 0, 50, 100], full: [0, 0, 100, 100] };

if (typeof document !== "undefined") start();

function start() {
  const desk = document.getElementById("desk"), cursor = document.getElementById("cursor"), hint = document.getElementById("zone");
  const video = document.getElementById("cam"), status = document.getElementById("status"), button = document.getElementById("camBtn");
  const wins = [...desk.querySelectorAll(".win")];
  let top = 10, held = null, dx = 0, dy = 0, at = { x: .5, y: .5 };

  function place(w, x, y, width, height) {
    w.style.left = x + "%"; w.style.top = y + "%"; w.style.width = width + "%"; w.style.height = height + "%";
  }
  const frame = w => [parseFloat(w.style.left), parseFloat(w.style.top), parseFloat(w.style.width), parseFloat(w.style.height)];
  function zone(name) {
    hint.hidden = !name;
    if (name) place(hint, ...ZONES[name]);
  }
  function winAt(x, y) {
    return wins.filter(w => { const [l, t, a, b] = frame(w); return x * 100 >= l && x * 100 <= l + a && y * 100 >= t && y * 100 <= t + b; })
      .sort((a, b) => b.style.zIndex - a.style.zIndex)[0];
  }
  function grab(x, y) {
    held = winAt(x, y);
    if (!held) return;
    const [l, t, w] = frame(held);
    held.style.zIndex = ++top;
    held.classList.add("held");
    if (held.dataset.zone) {   // estaba encajada: vuelve a su tamaño bajo la mano
      held.dataset.zone = "";
      place(held, x * 100 - 17, y * 100 - 3, 34, 44);
      dx = 17; dy = 3;
      return;
    }
    dx = Math.min(x * 100 - l, w); dy = y * 100 - t;
  }
  function move(x, y) {
    at = { x, y };
    if (!held) return;
    const [, , w, h] = frame(held);
    place(held, x * 100 - dx, y * 100 - dy, w, h);
    zone(snapZone(x, y));
  }
  function fit(w, name) { w.dataset.zone = name; place(w, ...ZONES[name]); }
  function drop() {
    if (!held) return;
    const name = snapZone(at.x, at.y);
    if (name) fit(held, name);
    held.classList.remove("held");
    held = null;
    zone("");
  }

  // ---------- ratón, dedo y teclado ----------
  const rel = e => { const b = desk.getBoundingClientRect(); return [Math.min(1, Math.max(0, (e.clientX - b.left) / b.width)), Math.min(1, Math.max(0, (e.clientY - b.top) / b.height))]; };
  desk.addEventListener("pointerdown", e => { grab(...rel(e)); if (held) desk.setPointerCapture(e.pointerId); });
  desk.addEventListener("pointermove", e => move(...rel(e)));
  desk.addEventListener("pointerup", drop);
  desk.addEventListener("pointercancel", drop);
  wins.forEach((w, n) => {
    place(w, 8 + n * 24, 12 + n * 16, 34, 44);
    w.style.zIndex = n + 1;
    w.addEventListener("keydown", e => {
      const name = { ArrowLeft: "left", ArrowRight: "right", ArrowUp: "full" }[e.key];
      if (name) { e.preventDefault(); w.style.zIndex = ++top; fit(w, name); }
      else if (e.key === "ArrowDown") { e.preventDefault(); w.dataset.zone = ""; place(w, 8 + n * 24, 12 + n * 16, 34, 44); }
    });
  });

  // ---------- cámara ----------
  let stream = null, landmarker = null, raf = 0, last = "none", steady = 0, gesture = "none", fistAt = 0, busy = false;
  function stop(text = "Cámara apagada. También puedes arrastrar con el ratón o con las flechas.") {
    cancelAnimationFrame(raf);
    stream?.getTracks().forEach(t => t.stop());
    stream = null;
    video.srcObject = null;
    cursor.hidden = true;
    drop();
    button.textContent = "Activar la cámara";
    status.textContent = text;
  }
  async function begin() {
    status.textContent = "Cargando el modelo de manos…";
    if (!landmarker) {
      const mp = await import("https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@1.2.3/vision_bundle.mjs");
      const files = await mp.FilesetResolver.forVisionTasks("https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@1.2.3/wasm");
      landmarker = await mp.HandLandmarker.createFromOptions(files, { runningMode: "VIDEO", numHands: 1, baseOptions: {
        modelAssetPath: "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task" } });
    }
    stream = await navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480 } });
    video.srcObject = stream;
    await video.play();
    button.textContent = "Apagar la cámara";
    status.textContent = "Enseña la mano.";
    loop();
  }
  function loop() {
    if (!stream) return;
    raf = requestAnimationFrame(loop);
    const hand = landmarker.detectForVideo(video, performance.now()).landmarks[0];
    if (!hand) { cursor.hidden = true; drop(); gesture = last = "none"; return; }
    const now = classify(hand);
    steady = now === last ? steady + 1 : 0;   // 3 fotogramas iguales: un temblor no suelta la ventana
    last = now;
    const x = Math.min(1, Math.max(0, (1 - (hand[4].x + hand[8].x) / 2 - .15) / .7)), y = Math.min(1, Math.max(0, ((hand[4].y + hand[8].y) / 2 - .15) / .7));
    cursor.hidden = false;
    cursor.style.left = x * 100 + "%"; cursor.style.top = y * 100 + "%";
    cursor.className = now === "pinch" ? "on" : "";
    if (steady >= 2 && now !== gesture) {
      if (gesture === "pinch") drop();
      if (now === "pinch") grab(x, y);
      if (now === "fist") fistAt = performance.now();
      if (now === "palm" && performance.now() - fistAt < 1200) { const w = winAt(x, y); if (w) { w.style.zIndex = ++top; fit(w, "full"); } }
      gesture = now;
      status.textContent = { pinch: "Cogida: muévela y suelta cerca de un borde.", palm: "Mano abierta.", fist: "Puño: ábrelo para maximizar.", peace: "Paz: en Cortex activa o para los gestos.", none: "Enseña la mano." }[now];
    }
    move(x, y);
  }
  button.addEventListener("click", async () => {
    if (stream) return stop();
    if (busy) return;
    busy = true;
    try { await begin(); }
    catch (e) { stop(e.name === "NotAllowedError" ? "Sin permiso de cámara. Puedes arrastrar con el ratón." : "No se ha podido usar la cámara aquí. Puedes arrastrar con el ratón."); }
    busy = false;
  });
  addEventListener("pagehide", () => stop());
  document.addEventListener("visibilitychange", () => { if (document.hidden && stream) stop(); });
}
