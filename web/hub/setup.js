"use strict";
// First-run wizard (and "Settings → Brain → Setup assistant"): cards and accounts, step by step.
// Every step can be skipped. In the web demo (CORTEX_DEMO) the account fields are disabled: nobody signs in there.

const SETUP_DEMO = !!window.CORTEX_DEMO;
const SETUP_STEPS = [
  ["welcome", "Hello"], ["widgets", "Cards"], ["claude", "Claude"], ["spotify", "Spotify"],
  ["email", "Mail"], ["whatsapp", "WhatsApp"], ["done", "Done"],
];
let setupStep = "", setupSteps = [], setupTimer = 0, setupPicked = null;

const setupPost = (url, body = {}) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body) }).then(r => r.json()).catch(() => ({ ok: false, error: "Cortex is not responding." }));

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
      <div class="setup-foot"><button type="button" class="text-btn" id="setupBack">Back</button>
        <button type="button" class="text-btn" id="setupSkip">Finish later</button>
        <button type="button" class="btn primary" id="setupNext">Next</button></div></div></div>`);
    $("#setupBack").onclick = () => setupGo(-1);
    $("#setupNext").onclick = () => setupGo(1);
    $("#setupSkip").onclick = closeSetup;
    $("#setupModal").addEventListener("keydown", e => {
      e.stopPropagation();   // dashboard shortcuts (space, Esc…) do not act while the wizard is open
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
  sessionStorage.setItem("setupStep", step);   // Spotify leaves for another page and comes back: resume where we were
  const i = setupSteps.indexOf(step);
  $("#setupNav").innerHTML = setupSteps.map((id, n) =>
    `<li class="${n < i ? "past" : n === i ? "now" : ""}"${n === i ? ' aria-current="step"' : ""}>${SETUP_STEPS.find(s => s[0] === id)[1]}</li>`).join("");
  $("#setupBack").hidden = i === 0;
  $("#setupSkip").hidden = step === "done";
  $("#setupNext").textContent = step === "done" ? "Get started" : ["welcome", "widgets"].includes(step) ? "Next" : "Skip";
  $("#setupBody").innerHTML = SETUP_VIEWS[step]();
  SETUP_BIND[step]?.();
  ($("#setupBody input:not(:disabled)") || $("#setupNext")).focus();
}

const setupHead = (title, text) => `<h2 id="setupTitle">${title}</h2><p class="note">${text}</p>`;
const setupDemoNote = SETUP_DEMO ? `<p class="setup-demo">Web demo: the steps are the same as in the app, but here nobody signs in and no account is saved.</p>` : "";
const setupState = text => `<p class="setup-state" id="setupState" role="status">${text}</p>`;

const SETUP_VIEWS = {
  welcome: () => setupHead("Welcome to Cortex", "Voice assistant, gestures and dashboard. Pick your cards and connect whatever you want to use. Everything can be skipped and changed later in Settings.") +
    `<label class="field">What should I call you?<input class="input" id="setupName" maxlength="40" autocomplete="off" value="${esc(settings.user_name || "")}" placeholder="Your name (optional)"></label>
    <p class="setup-demo"><b>Voice assistant.</b> While waiting for "Cortex", the mic is always on and may trigger by mistake. That audio is processed on this computer; what you say or type is sent to Claude, and replies are read aloud with the Microsoft Edge online voice. Pause it with the ear button or in Settings → Voice.</p>`,

  widgets: () => {
    const on = setupPicked || layout.filter(w => !w.hidden).map(w => w.id);
    return setupHead("Your cards", "Tick the ones you want to see. You can move and resize them afterwards.") +
      `<div class="setup-widgets" id="setupWidgets">${Object.keys(WIDGETS).map(id => `<label><input type="checkbox" value="${id}" ${on.includes(id) ? "checked" : ""}><span>${esc(WIDGETS[id])}</span></label>`).join("")}</div>`;
  },

  claude: () => setupHead("Claude", "The voice assistant thinks with Claude Code and your own Claude account. Anthropic handles the sign-in in your browser; Cortex never sees your password.") + setupDemoNote +
    `<div id="setupClaude"></div>` + setupState("Checking…"),

  spotify: () => setupHead("Spotify", "Everyone uses their own Spotify app (free). Playing music from here requires Premium.") + setupDemoNote +
    `<ol class="setup-list"><li>Go to <a href="https://developer.spotify.com/dashboard" target="_blank" rel="noopener">developer.spotify.com/dashboard</a> and create an app (Web API).</li>
      <li>Paste <code id="setupUri">http://127.0.0.1:8765/spotify/callback</code> into "Redirect URIs".</li><li>Copy the <b>Client ID</b> and paste it here.</li></ol>
    <form class="with-btn" id="setupSpotify"><input class="input" name="client_id" placeholder="Client ID (32 characters)" aria-label="Spotify Client ID" autocomplete="off" maxlength="32" ${SETUP_DEMO ? "disabled" : ""}>
      <button class="btn">Connect</button></form>` + setupState(""),

  email: () => setupHead("Mail (Gmail)", "Uses an app password: it is tested right away and stored only on your computer (.env next to the program).") + setupDemoNote +
    `<ol class="setup-list"><li>With two-step verification on, create one at <a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noopener">myaccount.google.com/apppasswords</a>.</li>
      <li>Paste your email address and the 16 letters here.</li></ol>
    <form class="setup-form" id="setupMail"><input class="input" name="user" type="email" placeholder="you@gmail.com" aria-label="Email" autocomplete="off" required ${SETUP_DEMO ? "disabled" : ""}>
      <input class="input" name="password" type="password" placeholder="App password" aria-label="App password" autocomplete="new-password" required ${SETUP_DEMO ? "disabled" : ""}>
      <button class="btn" ${SETUP_DEMO ? "disabled" : ""}>Connect</button></form>` + setupState(""),

  whatsapp: () => setupHead("WhatsApp", "Cortex shows your chats and notifies you of new messages.") + setupDemoNote +
    `<p class="setup-demo"><b>Warning.</b> Uses an unofficial client (whatsmeow) linked as a device: WhatsApp may limit or ban the account, more so if you send messages. The assistant never sends; only you do, from an open chat.</p>
    <fieldset class="setup-mode" id="setupWaMode"><legend>What it can do</legend>
      <label><input type="radio" name="waMode" value="read" ${settings.whatsapp_mode !== "write" ? "checked" : ""}> Read only</label>
      <label><input type="radio" name="waMode" value="write" ${settings.whatsapp_mode === "write" ? "checked" : ""}> Read and write</label></fieldset>` +
    `<button type="button" class="btn" id="setupWa" hidden>Link with QR</button><div id="setupQr"></div>` + setupState("Checking…"),

  done: () => setupHead("All set", "Say \"Cortex\" or press the mic. The first time, speech recognition downloads in the background (about 650 MB).") +
    `<ul class="setup-list"><li>Cards: grid button, top right.</li><li>Accounts: Settings → Brain → Setup assistant.</li>
      <li>Window gestures: pinch to grab, release to snap, peace sign to start or stop.</li></ul>`,
};

function setupSay(text, kind = "") {
  const el = $("#setupState");
  if (!el) return;
  el.className = "setup-state " + kind;
  if (el.textContent !== text) el.textContent = text;
  if (kind === "ok") $("#setupNext").textContent = "Next";
}

/** Poll the account status every few seconds while that step is open. */
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
        ? `<ol class="setup-list"><li>Open PowerShell and paste <code>irm https://claude.ai/install.ps1 | iex</code> (official installer, <a href="https://code.claude.com/docs/en/setup" target="_blank" rel="noopener">more info</a>).</li><li>Come back here: it is detected automatically.</li></ol>`
        : kind === "login" ? `<button type="button" class="btn" id="setupClaudeBtn">Sign in to Claude</button>` : "";
      setupSay(kind === "install" ? "Claude Code is not installed." : kind === "login" ? "Sign-in needed." : "Claude is connected.", kind === "in" ? "ok" : "");
      $("#setupClaudeBtn")?.addEventListener("click", async () => {
        const r = await setupPost("/api/setup/claude");
        setupSay(r.ok ? "Follow the steps in the window that just opened…" : r.error, r.ok ? "" : "bad");
      });
    });
  },

  spotify() {
    let saved = false;
    setupWatch("spotify", s => {
      saved = s.client_id;
      $("#setupUri").textContent = s.redirect_uri;
      if (saved && !SETUP_DEMO) $("#setupSpotify").client_id.placeholder = "Client ID saved · paste another to change it";
      if (s.connected) setupSay("Spotify is connected.", "ok");
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
    setupWatch("email", m => { if (m.connected) setupSay("Mail connected.", "ok"); });
    $("#setupMail").onsubmit = async e => {
      e.preventDefault();
      setupSay("Testing…");
      const f = e.target, r = await setupPost("/api/mail/login", { slot: "p", user: f.user.value, password: f.password.value });
      if (r.ok) f.password.value = "";
      setupSay(r.ok ? "Mail connected." : r.error, r.ok ? "ok" : "bad");
    };
  },

  whatsapp() {
    setupWatch("whatsapp", w => {
      $("#setupWa").hidden = !w.available || w.state !== "off";
      if (!w.available) return setupSay("This Cortex was installed without WhatsApp. Run the installer again and tick it.");
      $("#setupQr").innerHTML = w.qr ? `<img class="setup-qr" src="/api/whatsapp/qr.svg?t=${Date.now()}" alt="QR code to link WhatsApp">` : "";
      if (w.state === "connected") setupSay("WhatsApp is linked.", "ok");
      else if (w.qr) setupSay("On your phone: WhatsApp → Linked devices → Link a device, and scan the code.");
      else if (w.state === "off") setupSay("Not linked.");
      else if (w.state === "error") setupSay("The bridge closed; retrying…", "bad");
      else setupSay("Preparing the code…");
    });
    $("#setupWaMode").onchange = e => saveSettings({ whatsapp_mode: e.target.value });
    $("#setupWa").onclick = async () => {
      setupSay("Preparing the code…");
      const r = await setupPost("/api/setup/whatsapp");
      if (!r.ok) setupSay(r.error || "Could not start the WhatsApp bridge.", "bad");
    };
  },
};

$("#openSetup").addEventListener("click", () => openSetup(SETUP_STEPS.map(s => s[0])));
$("#quitApp").addEventListener("click", async () => {
  quitting = true;
  const r = await setupPost("/api/quit");
  if (r.ok) toast("Cortex has quit. You can close this tab now.");
  else { quitting = false; toast(r.error); }
});

(async function firstRun() {
  try {
    const s = (await (await fetch("/api/settings")).json()).settings;
    if (s.setup_done) return;
    while (!layout.length) await new Promise(r => setTimeout(r, 100));   // the dashboard loads its cards in parallel
    settings = { ...settings, ...s };
    openSetup(setupIds(s.setup_accounts), sessionStorage.getItem("setupStep"));
  } catch {}
})();
