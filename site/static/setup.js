"use strict";
// Asistente de primera vez (y «Ajustes → Cerebro → Asistente de configuración»): tarjetas y cuentas, paso a paso.
// Todo se puede saltar. En la demo web (CORTEX_DEMO) los campos de cuentas van desactivados: ahí no se inicia sesión.

const SETUP_DEMO = !!window.CORTEX_DEMO;
const SETUP_STEPS = [
  ["welcome", "Hola"], ["widgets", "Tarjetas"], ["claude", "Claude"], ["spotify", "Spotify"],
  ["email", "Correo"], ["whatsapp", "WhatsApp"], ["done", "Listo"],
];
let setupStep = "", setupSteps = [], setupTimer = 0, setupPicked = null;

const setupPost = (url, body = {}) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body) }).then(r => r.json()).catch(() => ({ ok: false, error: "Cortex no responde." }));

function setupIds(accounts) {
  const wanted = Array.isArray(accounts) ? accounts : [];
  return SETUP_STEPS.map(s => s[0]).filter(id => ["welcome", "widgets", "done"].includes(id) || wanted.includes(id));
}

function openSetup(steps, step) {
  setupSteps = steps;
  setupPicked = null;
  $("#drawer").hidden = true;
  if (!$("#setupModal")) {
    document.body.insertAdjacentHTML("beforeend", `<div class="modal" id="setupModal" role="dialog" aria-modal="true" aria-labelledby="setupTitle">
      <div class="modal-card setup-card"><ol class="setup-steps" id="setupNav"></ol><div id="setupBody"></div>
      <div class="setup-foot"><button type="button" class="text-btn" id="setupBack">Atrás</button>
        <button type="button" class="text-btn" id="setupSkip">Terminar más tarde</button>
        <button type="button" class="btn primary" id="setupNext">Siguiente</button></div></div></div>`);
    $("#setupBack").onclick = () => setupGo(-1);
    $("#setupNext").onclick = () => setupGo(1);
    $("#setupSkip").onclick = closeSetup;
    $("#setupModal").addEventListener("keydown", e => {
      e.stopPropagation();   // los atajos del panel (espacio, Esc…) no actúan con el asistente abierto
      if (e.key === "Escape") { closeSetup(); return; }
      if (e.key !== "Tab") return;
      const able = $$("button, input, a[href]", $("#setupModal")).filter(el => !el.disabled && el.offsetParent);
      const end = e.shiftKey ? able[0] : able[able.length - 1];
      if (document.activeElement === end) { e.preventDefault(); (e.shiftKey ? able[able.length - 1] : able[0]).focus(); }
    });
  }
  $("#setupModal").hidden = false;
  showSetup(steps.includes(step) ? step : steps[0]);
}

function closeSetup() {
  clearInterval(setupTimer);
  sessionStorage.removeItem("setupStep");
  $("#setupModal").hidden = true;
  return saveSettings({ setup_done: true });
}

async function setupGo(d) {
  if (d > 0 && setupStep === "welcome") await saveSettings({ user_name: $("#setupName").value.trim().slice(0, 40) });
  if (setupStep === "widgets") setupPicked = $$("#setupWidgets input:checked").map(i => i.value);
  const next = setupSteps[setupSteps.indexOf(setupStep) + d];
  if (next) { showSetup(next); return; }
  const shown = layout.filter(w => !w.hidden).map(w => w.id);
  const changed = setupPicked?.length && [...setupPicked].sort().join() !== shown.sort().join();
  await closeSetup();
  if (changed) {
    await setupPost("/api/layout", layoutFor(setupPicked));
    location.reload();
  }
}

function showSetup(step) {
  clearInterval(setupTimer);
  setupStep = step;
  sessionStorage.setItem("setupStep", step);   // Spotify sale a otra página y vuelve: se sigue donde estaba
  const i = setupSteps.indexOf(step);
  $("#setupNav").innerHTML = setupSteps.map((id, n) =>
    `<li class="${n < i ? "past" : n === i ? "now" : ""}"${n === i ? ' aria-current="step"' : ""}>${SETUP_STEPS.find(s => s[0] === id)[1]}</li>`).join("");
  $("#setupBack").hidden = i === 0;
  $("#setupSkip").hidden = step === "done";
  $("#setupNext").textContent = step === "done" ? "Empezar" : ["welcome", "widgets"].includes(step) ? "Siguiente" : "Saltar";
  $("#setupBody").innerHTML = SETUP_VIEWS[step]();
  SETUP_BIND[step]?.();
  ($("#setupBody input:not(:disabled)") || $("#setupNext")).focus();
}

const setupHead = (title, text) => `<h2 id="setupTitle">${title}</h2><p class="note">${text}</p>`;
const setupDemoNote = SETUP_DEMO ? `<p class="setup-demo">Demo web: los pasos son los del programa, pero aquí no se inicia sesión ni se guarda ninguna cuenta.</p>` : "";
const setupState = text => `<p class="setup-state" id="setupState" role="status">${text}</p>`;

const SETUP_VIEWS = {
  welcome: () => setupHead("Bienvenido a Cortex", "Asistente de voz, gestos y panel. Elige tus tarjetas y conecta lo que quieras usar. Todo se puede saltar y cambiar después en Ajustes.") +
    `<label class="field">¿Cómo te llamo?<input class="input" id="setupName" maxlength="40" autocomplete="off" value="${esc(settings.user_name || "")}" placeholder="Tu nombre (opcional)"></label>
    <p class="setup-demo"><b>Asistente de voz.</b> Mientras espera «Cortex», el micro está siempre encendido y puede activarse por error. Ese audio se procesa en este equipo; lo que le dices o escribes se envía a Claude, y las respuestas se leen con la voz en línea de Microsoft Edge. Se pausa con el botón del oído o en Ajustes → Voz.</p>`,

  widgets: () => {
    const on = setupPicked || layout.filter(w => !w.hidden).map(w => w.id);
    return setupHead("Tus tarjetas", "Marca las que quieres ver. Luego puedes moverlas y estirarlas.") +
      `<div class="setup-widgets" id="setupWidgets">${Object.keys(WIDGETS).map(id => `<label><input type="checkbox" value="${id}" ${on.includes(id) ? "checked" : ""}><span>${esc(WIDGETS[id])}</span></label>`).join("")}</div>`;
  },

  claude: () => setupHead("Claude", "El asistente de voz piensa con Claude Code y tu propia cuenta de Claude. El inicio de sesión lo hace Anthropic en tu navegador; Cortex no ve tu contraseña.") + setupDemoNote +
    `<div id="setupClaude"></div>` + setupState("Comprobando…"),

  spotify: () => setupHead("Spotify", "Cada persona usa su propia app de Spotify (gratis). Para poner música desde aquí hace falta Premium.") + setupDemoNote +
    `<ol class="setup-list"><li>Entra en <a href="https://developer.spotify.com/dashboard" target="_blank" rel="noopener">developer.spotify.com/dashboard</a> y crea una app (Web API).</li>
      <li>En «Redirect URIs» pega <code id="setupUri">http://127.0.0.1:8765/spotify/callback</code></li><li>Copia el <b>Client ID</b> y pégalo aquí.</li></ol>
    <form class="with-btn" id="setupSpotify"><input class="input" name="client_id" placeholder="Client ID (32 caracteres)" aria-label="Client ID de Spotify" autocomplete="off" maxlength="32" ${SETUP_DEMO ? "disabled" : ""}>
      <button class="btn">Conectar</button></form>` + setupState(""),

  email: () => setupHead("Correo (Gmail)", "Con una contraseña de aplicación: se prueba en el momento y se guarda solo en tu equipo (.env junto al programa).") + setupDemoNote +
    `<ol class="setup-list"><li>Con la verificación en dos pasos activada, crea una en <a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noopener">myaccount.google.com/apppasswords</a>.</li>
      <li>Pega aquí tu correo y las 16 letras.</li></ol>
    <form class="setup-form" id="setupMail"><input class="input" name="user" type="email" placeholder="correo@gmail.com" aria-label="Correo" autocomplete="off" required ${SETUP_DEMO ? "disabled" : ""}>
      <input class="input" name="password" type="password" placeholder="Contraseña de aplicación" aria-label="Contraseña de aplicación" autocomplete="new-password" required ${SETUP_DEMO ? "disabled" : ""}>
      <button class="btn" ${SETUP_DEMO ? "disabled" : ""}>Conectar</button></form>` + setupState(""),

  whatsapp: () => setupHead("WhatsApp", "Cortex enseña tus chats y avisa de lo nuevo.") + setupDemoNote +
    `<p class="setup-demo"><b>Aviso.</b> Usa un cliente no oficial (whatsmeow) vinculado como dispositivo: WhatsApp podría limitar o bloquear la cuenta, y más si envías mensajes. El asistente nunca envía; solo tú, desde un chat abierto.</p>
    <fieldset class="setup-mode" id="setupWaMode"><legend>Qué puede hacer</legend>
      <label><input type="radio" name="waMode" value="read" ${settings.whatsapp_mode !== "write" ? "checked" : ""}> Solo leer</label>
      <label><input type="radio" name="waMode" value="write" ${settings.whatsapp_mode === "write" ? "checked" : ""}> Leer y escribir</label></fieldset>` +
    `<button type="button" class="btn" id="setupWa" hidden>Vincular con QR</button><div id="setupQr"></div>` + setupState("Comprobando…"),

  done: () => setupHead("Todo listo", "Di «Cortex» o pulsa el micro. La primera vez, el reconocimiento de voz se descarga en segundo plano (unos 650 MB).") +
    `<ul class="setup-list"><li>Tarjetas: botón de rejilla, arriba a la derecha.</li><li>Cuentas: Ajustes → Cerebro → Asistente de configuración.</li>
      <li>Ventanas con gestos: pellizca para coger, suelta para encajar, signo de paz para activar o parar.</li></ul>`,
};

function setupSay(text, kind = "") {
  const el = $("#setupState");
  if (!el) return;
  el.className = "setup-state " + kind;
  if (el.textContent !== text) el.textContent = text;
  if (kind === "ok") $("#setupNext").textContent = "Siguiente";
}

/** Mira cada pocos segundos cómo va la cuenta mientras ese paso está abierto. */
function setupWatch(step, show) {
  const look = async () => {
    let r = null;
    try { r = await (await fetch("/api/setup")).json(); } catch {}
    if (r && setupStep === step && !$("#setupModal").hidden) show(r[step]);
  };
  look();
  setupTimer = setInterval(look, 3000);
}

const SETUP_BIND = {
  claude() {
    let wasIn = null;
    setupWatch("claude", c => {
      const kind = !c.installed ? "install" : c.logged ? "in" : "login", box = $("#setupClaude");
      if (c.logged && wasIn === false) setupPost("/api/setup/claude/start");
      wasIn = c.logged;
      if (box.dataset.kind === kind) return;
      box.dataset.kind = kind;
      box.innerHTML = kind === "install"
        ? `<ol class="setup-list"><li>Abre PowerShell y pega <code>irm https://claude.ai/install.ps1 | iex</code> (instalador oficial, <a href="https://code.claude.com/docs/en/setup" target="_blank" rel="noopener">más información</a>).</li><li>Vuelve aquí: lo detecto solo.</li></ol>`
        : kind === "login" ? `<button type="button" class="btn" id="setupClaudeBtn">Iniciar sesión en Claude</button>` : "";
      setupSay(kind === "install" ? "Claude Code no está instalado." : kind === "login" ? "Falta iniciar sesión." : "Claude está conectado.", kind === "in" ? "ok" : "");
      $("#setupClaudeBtn")?.addEventListener("click", async () => {
        const r = await setupPost("/api/setup/claude");
        setupSay(r.ok ? "Sigue los pasos en la ventana que se ha abierto…" : r.error, r.ok ? "" : "bad");
      });
    });
  },

  spotify() {
    let saved = false;
    setupWatch("spotify", s => {
      saved = s.client_id;
      $("#setupUri").textContent = s.redirect_uri;
      if (saved && !SETUP_DEMO) $("#setupSpotify").client_id.placeholder = "Client ID guardado · pega otro para cambiarlo";
      if (s.connected) setupSay("Spotify está conectado.", "ok");
    });
    $("#setupSpotify").onsubmit = async e => {
      e.preventDefault();
      const id = e.target.client_id.value.trim();
      if (id || !saved) {
        const r = await setupPost("/api/setup/spotify", { client_id: id });
        if (!r.ok) return setupSay(r.error, "bad");
      }
      if (!SETUP_DEMO) location.href = "/spotify/login";
    };
  },

  email() {
    setupWatch("email", m => { if (m.connected) setupSay("Correo conectado.", "ok"); });
    $("#setupMail").onsubmit = async e => {
      e.preventDefault();
      setupSay("Probando…");
      const f = e.target, r = await setupPost("/api/mail/login", { slot: "p", user: f.user.value, password: f.password.value });
      if (r.ok) f.password.value = "";
      setupSay(r.ok ? "Correo conectado." : r.error, r.ok ? "ok" : "bad");
    };
  },

  whatsapp() {
    setupWatch("whatsapp", w => {
      $("#setupWa").hidden = !w.available || w.state !== "off";
      if (!w.available) return setupSay("Este Cortex se instaló sin WhatsApp. Vuelve a pasar el instalador y márcalo.");
      $("#setupQr").innerHTML = w.qr ? `<img class="setup-qr" src="/api/whatsapp/qr.svg?t=${Date.now()}" alt="Código QR para vincular WhatsApp">` : "";
      if (w.state === "connected") setupSay("WhatsApp está vinculado.", "ok");
      else if (w.qr) setupSay("En el móvil: WhatsApp → Dispositivos vinculados → Vincular, y escanea el código.");
      else if (w.state === "off") setupSay("Sin vincular.");
      else if (w.state === "error") setupSay("El puente se ha cerrado; lo reintento…", "bad");
      else setupSay("Preparando el código…");
    });
    $("#setupWaMode").onchange = e => saveSettings({ whatsapp_mode: e.target.value });
    $("#setupWa").onclick = async () => {
      setupSay("Preparando el código…");
      const r = await setupPost("/api/setup/whatsapp");
      if (!r.ok) setupSay(r.error || "No se ha podido arrancar el puente de WhatsApp.", "bad");
    };
  },
};

$("#openSetup").addEventListener("click", () => openSetup(SETUP_STEPS.map(s => s[0])));
$("#quitApp").addEventListener("click", async () => {
  quitting = true;
  const r = await setupPost("/api/quit");
  if (r.ok) toast("Cortex se ha cerrado. Ya puedes cerrar esta pestaña.");
  else { quitting = false; toast(r.error); }
});

(async function firstRun() {
  try {
    const s = (await (await fetch("/api/settings")).json()).settings;
    if (s.setup_done) return;
    while (!layout.length) await new Promise(r => setTimeout(r, 100));   // el panel carga sus tarjetas en paralelo
    settings = { ...settings, ...s };
    openSetup(setupIds(s.setup_accounts), sessionStorage.getItem("setupStep"));
  } catch {}
})();
