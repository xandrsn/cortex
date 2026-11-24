"use strict";
// Demo web: hace de servidor de Cortex dentro del navegador. El panel (/static/app.js) es el mismo del programa.
// Todo es inventado y se guarda solo en este navegador (localStorage); no se inicia sesión en ninguna cuenta.

const DEMO_WORKS = ["today", "tasks", "avisos", "timers", "compra", "gym", "meals", "betis"];   // el resto: concepto
const DEMO_ONLY_APP = "Solo en el programa de Windows: aquí se enseña la idea.";
const PHRASES = ["¿Qué tengo hoy?", "Pon un temporizador de 5 minutos", "Añade pan a la lista", "¿Qué tiempo hace?"];
const EXAMPLES = "Prueba: «añade leche a la lista», «temporizador de 5 minutos», «apunta la tarea llamar al dentista», «recuérdame en 2 minutos beber agua», «¿qué tiempo hace?» o «modo oscuro».";

// Igual que CODES en src/pepito/weather.py
function wmo(code) {
  const at = [[0, "Despejado", "sun"], [1, "Casi despejado", "sun"], [2, "Algunas nubes", "cloud-sun"], [3, "Nublado", "cloud"],
    [48, "Niebla", "fog"], [57, "Llovizna", "drizzle"], [67, "Lluvia", "rain"], [77, "Nieve", "snow"], [82, "Chubascos", "rain"],
    [86, "Nieve", "snow"], [99, "Tormenta", "storm"]].find(([max]) => code <= max);
  return at ? { text: at[1], icon: at[2] } : { text: "", icon: "cloud" };
}

function parseAmount(text) {   // «5 minutos», «media hora», «un minuto» → segundos
  const t = text.toLowerCase();
  if (/media hora/.test(t)) return 1800;
  const m = t.match(/(\d+|un|una|dos|tres|cuatro|cinco|diez|quince|veinte|treinta)\s*(segundo|minuto|min\b|hora)/);
  if (!m) return 0;
  const words = { un: 1, una: 1, dos: 2, tres: 3, cuatro: 4, cinco: 5, diez: 10, quince: 15, veinte: 20, treinta: 30 };
  return (words[m[1]] || Number(m[1])) * (m[2] === "segundo" ? 1 : m[2] === "hora" ? 3600 : 60);
}

function freshState(now) {
  const t = Math.floor(now / 1000);
  return {
    settings: {}, layout: [], timers: [], nextId: 10, history: [],
    tasks: [
      { i: 1, tarea: "Preparar la presentación", cuando: "viernes", estado: "", prioridad: "alta", notas: "" },
      { i: 2, tarea: "Llamar al taller", cuando: "", estado: "", prioridad: "media", notas: "" },
      { i: 3, tarea: "Ordenar las fotos", cuando: "", estado: "a medias", prioridad: "baja", notas: "" },
    ],
    compra: [{ text: "Leche", done: false }, { text: "Tomates", done: false }, { text: "Café", done: true }],
    avisos: [{ id: "a1", at: now + 3 * 3600e3, texto: "Regar las plantas", repetir: "" }],
    spotify: { connected: true, client_id: "demo", playing: false, track: 0, volume: 40, progress: 42_000, duration: 215_000, device: "Portátil (demo)" },
    chats: [
      { jid: "c1", name: "Grupo del finde", real: "Grupo del finde", group: true, muted: false,
        messages: [{ id: "1", from: "Lucía", text: "¿Quedamos el sábado?", time: t - 5400 }, { id: "2", from: "Marcos", text: "Por mí sí, a las 12", time: t - 5000 }] },
      { jid: "c2", name: "Panadería", real: "Panadería", group: false, muted: true,
        messages: [{ id: "3", from: "Panadería", text: "Tu encargo ya está listo", time: t - 90_000 }] },
    ],
  };
}

const TRACKS = [["Marea baja", "Los Ejemplos", "#0b8f55"], ["Neón", "Banda Inventada", "#7c3aed"], ["Domingo lento", "Trío de Muestra", "#ea580c"]];
const art = (label, color) => "data:image/svg+xml," + encodeURIComponent(
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 90"><rect width="160" height="90" fill="${color}"/><text x="80" y="54" font-size="28" text-anchor="middle">${label}</text></svg>`);

const MAILS = [
  { id: "m1", account: "principal", from: "Biblioteca", addr: "avisos@example.com", subject: "Tu reserva está lista", ago: 3600 },
  { id: "m2", account: "principal", from: "Club de senderismo", addr: "club@example.com", subject: "Ruta del domingo", ago: 20_000 },
];

/** El «servidor». `emit` manda un mensaje al panel; `weather(place)` trae el tiempo. Sin nada del navegador: se prueba con node. */
function createDemo({ state, defaults, emit = () => {}, save = () => {}, clock = () => Date.now(), weather = async () => ({ ok: false }) }) {
  const S = state;
  const settings = () => ({ ...defaults, ...S.settings });
  const day = offset => new Date(clock() + offset * 864e5).toLocaleDateString("es-ES", { weekday: "short", day: "numeric" });
  const changed = (type, extra) => { save(S); emit({ type, ...extra }); };

  function data() {
    const week = ["Pierna", "Descanso", "Espalda", "Pecho", "Descanso", "Carrera suave", "Descanso"];
    const meals = [["Lentejas", "comida y cena"], ["Pollo al horno", "comida"], ["Pasta con verduras", "comida y cena"], ["Salmón", "comida"]];
    const wd = (new Date(clock()).getDay() + 6) % 7;
    return {
      ok: true,
      today: { mode: "Día de ejemplo", day: "", items: [
        { hora: "09:00", fin: "11:00", que: "Trabajo profundo" }, { hora: "11:30", fin: "12:30", que: "Reunión de ejemplo" },
        { hora: "14:30", fin: "15:30", que: "Comer" }, { hora: "18:30", fin: "19:45", que: "Gym" }, { hora: "21:00", fin: "22:00", que: "Cena" }] },
      tasks: S.tasks,
      betis: [{ partido: "Partido de ejemplo", cuando: day(3), hora: "21:00" }, { partido: "Otro partido", cuando: day(9), hora: "18:30" }],
      gym: { today: week[wd] === "Descanso" ? "" : week[wd], tomorrow: week[(wd + 1) % 7], notes: ["Datos inventados"],
        week: week.map((que, i) => ({ dia: ["L", "M", "X", "J", "V", "S", "D"][i], que, hoy: i === wd })) },
      menu: meals.map(([plato, cubre], i) => ({ dia: day(i), plato, cubre, hoy: i === 0 })),
      money: { colchon: 1500, queda: 320, gastos: 480, ingresos: 800 },
      dates: [{ cuando: day(5), que: "Cumpleaños de ejemplo" }],
    };
  }

  function spotify() {
    const [title, artist, color] = TRACKS[S.spotify.track];
    return S.spotify.connected ? { ...S.spotify, title, artist, image: art("♪", color) } : { ...S.spotify, playing: false };
  }

  const avisos = () => [...S.avisos].sort((a, b) => a.at - b.at).map(a => ({ id: a.id, texto: a.texto, repetir: a.repetir,
    di: new Date(a.at).toLocaleString("es-ES", { weekday: "short", hour: "2-digit", minute: "2-digit" }) }));
  const timers = () => ({ ok: true, now: clock() / 1000, timers: S.timers });

  function addTimer(seconds, label = "") {
    seconds = Math.min(86_400, Math.max(1, Math.round(Number(seconds) || 0)));
    S.timers.push({ id: "t" + S.nextId++, end: clock() / 1000 + seconds, seconds, label: String(label).slice(0, 60) });
    changed("timers", { timers: S.timers });
  }
  function addAviso(texto, at, repetir = "") {
    S.avisos.push({ id: "a" + S.nextId++, at, texto: texto.slice(0, 200), repetir });
    changed("avisos", { items: avisos() });
  }
  function addCompra(text) {
    S.compra.push({ text: text.slice(0, 80), done: false });
    changed("compra", { items: S.compra });
  }
  function addTask(t) {
    S.tasks.unshift({ i: S.nextId++, tarea: String(t.tarea).slice(0, 200), cuando: String(t.cuando || "").slice(0, 80), estado: "", notas: "",
      prioridad: ["alta", "media", "baja"].includes(t.prioridad) ? t.prioridad : "media" });
    changed("data", { data: data() });
  }
  function chat(c) {
    return { jid: c.jid, name: c.mote || c.real, real: c.real, group: c.group, muted: c.muted,
      messages: c.messages.map(m => ({ media: "", caption: "", mine: false, ...m })) };
  }
  function whatsapp() {
    return { available: true, state: "connected", qr: "", chats: S.chats.map(c => {
      const last = c.messages[c.messages.length - 1];
      return { jid: c.jid, name: c.mote || c.real, real: c.real, group: c.group, muted: c.muted, time: last.time, mine: !!last.mine, text: last.text, from: c.group && !last.mine ? last.from : "" };
    }) };
  }
  const devices = () => ({ music: [{ id: "d1", name: "Portátil (demo)", type: "Computer", active: true }, { id: "d2", name: "Altavoz del salón", type: "Speaker", active: false }],
    lan: [{ name: "Tele de ejemplo", kinds: ["Chromecast"], ip: "192.0.2.10" }], scanned_at: clock() / 1000 });
  const settingsPage = () => ({ settings: settings(), devices: { inputs: ["Micro (demo)"], outputs: ["Altavoces (demo)"] }, options: {
    voices: { es: ["es-ES-XimenaNeural", "es-ES-ElviraNeural", "es-ES-AlvaroNeural"], en: ["en-US-AvaMultilingualNeural"] },
    stt: ["parakeet", "whisper-small", "whisper-base"], claude: ["haiku", "sonnet", "opus"] } });
  const status = () => ({ type: "status", mic: { ok: true, name: "simulado", muted: false }, speaker: { ok: true, name: "voz del navegador" },
    claude: { ok: true, plan: null, model: settings().claude_model }, wake: settings().wake_enabled, wake_samples: 0, last_heard: "", wake_method: "detector" });

  /** Respuesta del asistente de la demo: órdenes sencillas de verdad; lo demás, un aviso de que aquí no hay Claude. */
  async function ask(text) {
    const t = text.trim().replace(/[¿?¡!.]+/g, "").replace(/^cortex[, ]+/i, ""), low = t.toLowerCase();
    let m;
    if (/temporizador/.test(low)) {
      const s = parseAmount(low);
      if (!s) return "¿De cuánto? Por ejemplo: temporizador de 5 minutos.";
      addTimer(s);
      return `Hecho, temporizador de ${s < 60 ? `${s} segundos` : s % 60 ? `${Math.round(s / 60)} minutos` : s === 60 ? "un minuto" : `${s / 60} minutos`}.`;
    }
    if ((m = t.match(/recu[eé]rdame (.+)/i))) {
      const s = parseAmount(m[1]);
      const what = m[1].replace(/\b(en|dentro de)\s+((\d+|un|una|dos|tres|cuatro|cinco|diez|quince|veinte|treinta)\s*(segundos?|minutos?|min|horas?)|media hora)\b/i, "").replace(/^\s*(que|de)\s+/i, "").replace(/\s+/g, " ").trim();
      if (!s || !what) return "Dime cuándo y qué: «recuérdame en 10 minutos sacar la pizza».";
      addAviso(what, clock() + s * 1000);
      return `Te aviso: ${what}.`;
    }
    if ((m = t.match(/(?:añade|apunta|pon|mete)\s+(.+?)\s+(?:a|en)\s+la\s+(?:lista|compra)/i))) { addCompra(m[1]); return `Añadido a la compra: ${m[1]}.`; }
    if ((m = t.match(/(?:apunta|añade|crea|nueva)\s+(?:la |una )?tarea:?\s+(.+)/i))) { addTask({ tarea: m[1] }); return `Apuntada la tarea: ${m[1]}.`; }
    if (/lista|compra/.test(low)) { const left = S.compra.filter(i => !i.done).map(i => i.text); return left.length ? `Queda por comprar: ${left.join(", ")}.` : "La lista está vacía."; }
    if (/modo oscuro|modo claro|tema/.test(low)) {
      const theme = /oscuro/.test(low) ? "dark" : /claro/.test(low) ? "light" : settings().theme === "dark" ? "light" : "dark";
      S.settings.theme = theme;
      changed("settings", { settings: settings() });
      return theme === "dark" ? "Modo oscuro." : "Modo claro.";
    }
    if (/tiempo|llov|lluvia|temperatura/.test(low)) {
      const w = await weather(settings().weather_place);
      return w.ok ? `En ${w.place}: ${w.text.toLowerCase()}, ${w.temp} grados.` : "Ahora mismo no llego al servicio del tiempo.";
    }
    if (/(m[uú]sica|canci[oó]n|spotify)/.test(low)) {
      S.spotify.playing = !/para|pausa|quita/.test(low);
      changed("spotify", spotify());
      return S.spotify.playing ? "Música de ejemplo: en el programa sonaría en tu Spotify." : "Música parada.";
    }
    if (/tareas?/.test(low)) return S.tasks.length ? `Tienes ${S.tasks.length} tareas. La primera: ${S.tasks[0].tarea}.` : "No tienes tareas.";
    if (/qu[eé] tengo|mi d[ií]a|resumen|matinal|buenos d[ií]as/.test(low)) {
      const d = data();
      return `Hoy: ${d.today.items.map(i => `${i.que.toLowerCase()} a las ${i.hora}`).join(", ")}. Tienes ${S.tasks.length} tareas y ${MAILS.length} correos de ejemplo.`;
    }
    if (/qu[eé] hora/.test(low)) return `Son las ${new Date(clock()).toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" })}.`;
    return `Esta es la demo: entiende unas pocas órdenes y no usa Claude. ${EXAMPLES}`;
  }

  /** Avisos y temporizadores que ya tocan. Devuelve lo que hay que decir. */
  function due() {
    const now = clock(), say = [];
    for (const t of S.timers.filter(t => t.end * 1000 <= now)) { say.push(t.label ? `Se acabó el tiempo: ${t.label}.` : "Se acabó el temporizador."); emit({ type: "timer_done" }); }
    const aviso = S.avisos.filter(a => a.at <= now);
    if (say.length) { S.timers = S.timers.filter(t => t.end * 1000 > now); changed("timers", { timers: S.timers }); }
    if (aviso.length) {
      for (const a of aviso) {
        say.push(`Aviso: ${a.texto}.`);
        // shortcut: todas las repeticiones se pasan al día siguiente; hacer el calendario real si la demo guarda avisos de verdad
        if (a.repetir) while (a.at <= now) a.at += 864e5;
      }
      S.avisos = S.avisos.filter(a => a.at > now);
      changed("avisos", { items: avisos() });
    }
    return say;
  }

  const no = { ok: false, error: DEMO_ONLY_APP };
  const routes = {
    "GET /api/layout": () => S.layout,
    "POST /api/layout": b => { if (Array.isArray(b)) { S.layout = b; save(S); } return { ok: true }; },
    "GET /api/settings": settingsPage,
    "POST /api/settings": b => {
      for (const k of Object.keys(b)) if (k in defaults) S.settings[k] = b[k];
      changed("settings", { settings: settings() });
      emit(status());
      return settingsPage();
    },
    "POST /api/devices/refresh": settingsPage,
    "GET /api/data": data,
    "POST /api/tasks": b => {
      const t = S.tasks.find(x => x.i === b.i);
      if (b.action !== "delete" && b.action !== "done" && !String(b.tarea || "").trim()) return { ok: false, error: "Escribe la tarea." };
      if (b.action === "add") addTask(b);
      else if (!t) return { ok: false, error: "Esa tarea ya no está." };
      else if (b.action === "edit") Object.assign(t, { tarea: String(b.tarea).slice(0, 200), cuando: String(b.cuando || "").slice(0, 80), prioridad: ["alta", "media", "baja"].includes(b.prioridad) ? b.prioridad : "media" });
      else S.tasks = S.tasks.filter(x => x !== t);
      save(S);
      return { ok: true, data: data() };
    },
    "GET /api/bank": () => ({ ok: true, fetched_at: clock() / 1000, accounts: [{ balance: { amount: 842.5 }, recent: [
      { date: new Date(clock() - 864e5).toISOString().slice(0, 10), description: "Supermercado (ejemplo)", amount: -23.4, pending: false },
      { date: new Date(clock() - 3 * 864e5).toISOString().slice(0, 10), description: "Nómina (ejemplo)", amount: 800, pending: false }] }] }),
    "GET /api/timers": timers,
    "POST /api/timers": b => {
      if (b.cancel) { S.timers = S.timers.filter(t => t.id !== b.cancel); save(S); }
      else if (Number(b.seconds) > 0) addTimer(b.seconds, b.label);
      return timers();
    },
    "GET /api/compra": () => ({ items: S.compra }),
    "POST /api/compra": b => {
      if (String(b.add || "").trim()) addCompra(String(b.add).trim());
      else if (b.toggle != null) { const it = S.compra[b.toggle]; if (it) it.done = !it.done; }
      else if (b.clear_done) S.compra = S.compra.filter(i => !i.done);
      save(S);
      return { ok: true, items: S.compra };
    },
    "GET /api/avisos": avisos,
    "POST /api/avisos": b => {
      const at = Date.parse(String(b.cuando || "").replace(" ", "T"));
      if (!String(b.texto || "").trim()) return { error: "Escribe el aviso." };
      if (!(at > clock())) return { error: "Elige una hora futura." };
      addAviso(String(b.texto).trim(), at, b.repetir || "");
      return { ok: true };
    },
    "POST /api/avisos/delete": b => { S.avisos = S.avisos.filter(a => a.id !== b.id); changed("avisos", { items: avisos() }); return { ok: true }; },
    "GET /api/weather": () => weather(settings().weather_place),
    "POST /api/weather/place": async b => {
      const place = await weather.find?.(String(b.name || "").trim());
      if (!place) return { error: "No encuentro ese sitio." };
      S.settings.weather_place = place;
      save(S);
      return weather(place);
    },
    "GET /api/mail": () => ({ configured: true, accounts: [{}], error: "", checked_at: clock() / 1000,
      slots: [{ slot: "p", label: "Principal", user: "ejemplo@example.com", ok: true }],
      unread: MAILS.map(m => ({ ...m, time: clock() / 1000 - m.ago })) }),
    "GET /api/mail/open": (_, q) => { const m = MAILS.find(x => x.id === q.get("id")); return m ? { subject: m.subject, from: `${m.from} <${m.addr}>`, date: new Date(clock() - m.ago * 1000).toISOString(), files: [] } : { error: "Ese correo no está." }; },
    "GET /api/whatsapp": whatsapp,
    "GET /api/whatsapp/chat": (_, q) => { const c = S.chats.find(x => x.jid === q.get("jid")); return c ? chat(c) : { error: "Sin chat", messages: [] }; },
    "POST /api/whatsapp/mute": b => { const c = S.chats.find(x => x.jid === b.jid); if (c) { c.muted = !!b.muted; changed("whatsapp", whatsapp()); } return { ok: true }; },
    "POST /api/whatsapp/mote": b => { const c = S.chats.find(x => x.real === b.name); if (c) { c.mote = String(b.mote || "").trim().slice(0, 60); changed("whatsapp", whatsapp()); } return { ok: true }; },
    "POST /api/whatsapp/send": b => {
      const c = S.chats.find(x => x.jid === b.jid), text = String(b.text || "").trim().slice(0, 4000);
      if (!c || !text || settings().whatsapp_mode !== "write") return { ok: false, error: "No se puede enviar." };
      c.messages.push({ id: "m" + S.nextId++, text, mine: true, time: clock() / 1000 });
      changed("whatsapp", whatsapp());
      return { ok: true };
    },
    "POST /api/spotify": b => {
      const s = S.spotify;
      if (b.action === "toggle") s.playing = !s.playing;
      else if (b.action === "next" || b.action === "previous") { s.track = (s.track + (b.action === "next" ? 1 : TRACKS.length - 1)) % TRACKS.length; s.progress = 0; s.playing = true; }
      else if (b.action === "volume") s.volume = Math.max(0, Math.min(100, Number(b.value) || 0));
      else if (b.action === "logout") s.connected = false;
      changed("spotify", spotify());
      return { ok: true, devices: devices().music };
    },
    "GET /api/devices": devices,
    "POST /api/devices/scan": devices,
    "GET /api/youtube": (_, q) => ({ ok: true, results: ["Vídeo de ejemplo", "Otro vídeo", "Directo inventado"].map((title, i) => ({
      id: "demo" + i, title: `${title}: ${q.get("q") || ""}`, thumb: art("▶", TRACKS[i][2]), length: `${i + 2}:30`, channel: "Canal de muestra" })) }),
    "GET /api/video/services": () => ({ services: settings().video_services.filter(s => s.enabled).map(({ name, url }) => ({ name, url })) }),
    "POST /api/video": () => { emit({ type: "notice", text: DEMO_ONLY_APP }); return no; },
    "GET /api/web/accounts": () => ({ accounts: [] }),
    "POST /api/quiet": b => { S.settings.quiet_mode = !!b.on; changed("settings", { settings: settings() }); return { quiet: !!b.on }; },
    "GET /api/file": () => ({ ok: true, name: "demo", file: "demo.yaml", text: "# En el programa aquí se editan tus archivos (tareas, menú, gym…).\n" }),
    "GET /api/setup": () => ({ claude: { installed: true, logged: false }, spotify: { client_id: false, connected: false, redirect_uri: "http://127.0.0.1:8765/spotify/callback" },
      email: { connected: false }, whatsapp: { available: true, state: "off", qr: false } }),
  };

  async function api(method, path, query = "", body = {}) {
    const key = `${method} ${path.startsWith("/api/file/") ? "/api/file" : path}`;
    const r = routes[key];
    return r ? r(body || {}, new URLSearchParams(query)) : no;
  }
  const hello = () => [{ type: "hello", state: "idle", history: S.history, settings: settings(), web: "demo" }, status(),
    { type: "spotify", ...spotify() }, { type: "whatsapp", ...whatsapp() },
    { type: "vision", enabled: false, camera_ok: false, engaged: false, looking: false, hand: "", label: "", last_event: "", custom: [], fps: 0 }];
  return { api, ask, due, hello, settings, status, S };
}

if (typeof module !== "undefined") module.exports = { createDemo, freshState, parseAmount, wmo };

// ---------- en el navegador ----------
if (typeof window !== "undefined") (() => {
  window.CORTEX_DEMO = true;
  const KEY = "cortex-demo", realFetch = window.fetch.bind(window), wait = ms => new Promise(r => setTimeout(r, ms));
  let state = null, sockets = [], run = 0, phrase = 0;
  try { state = JSON.parse(localStorage.getItem(KEY)); } catch {}
  if (!state || !Array.isArray(state.tasks)) state = freshState(Date.now());
  const save = s => { try { localStorage.setItem(KEY, JSON.stringify(s)); } catch {} };
  const emit = msg => sockets.forEach(s => s.onmessage?.({ data: JSON.stringify(msg) }));

  const cache = {};
  async function weather(place) {
    const key = `${place.lat},${place.lon}`;
    if (cache[key] && Date.now() - cache[key].at < 900e3) return cache[key].data;
    try {
      const r = await realFetch("https://api.open-meteo.com/v1/forecast?" + new URLSearchParams({ latitude: place.lat, longitude: place.lon, timezone: "auto", forecast_days: 4,
        current: "temperature_2m,apparent_temperature,weather_code,is_day,wind_speed_10m,relative_humidity_2m",
        hourly: "temperature_2m,precipitation_probability,weather_code,is_day",
        daily: "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max" }));
      const d = await r.json(), c = d.current, h = d.hourly, y = d.daily;
      const from = Math.max(0, h.time.findIndex(t => t.slice(0, 13) >= c.time.slice(0, 13)));
      const data = { ok: true, place: place.name, ...wmo(c.weather_code), day: !!c.is_day, temp: Math.round(c.temperature_2m), feels: Math.round(c.apparent_temperature),
        wind: Math.round(c.wind_speed_10m), humidity: c.relative_humidity_2m,
        hours: [0, 2, 4, 6, 8, 10].map(n => from + n).filter(i => i < h.time.length).map(i => ({ hour: h.time[i].slice(11, 16), temp: Math.round(h.temperature_2m[i]), rain: h.precipitation_probability[i], day: !!h.is_day[i], icon: wmo(h.weather_code[i]).icon })),
        days: y.time.map((date, i) => ({ date, max: Math.round(y.temperature_2m_max[i]), min: Math.round(y.temperature_2m_min[i]), rain: y.precipitation_probability_max[i], ...wmo(y.weather_code[i]) })) };
      cache[key] = { at: Date.now(), data };
      return data;
    } catch { return { ok: false }; }
  }
  weather.find = async name => {
    if (!name) return null;
    try {
      const g = (await (await realFetch("https://geocoding-api.open-meteo.com/v1/search?" + new URLSearchParams({ name, count: 1, language: "es" }))).json()).results?.[0];
      return g && { name: g.name, lat: g.latitude, lon: g.longitude };
    } catch { return null; }
  };

  const demo = createDemo({ state, defaults: window.CORTEX_DEFAULTS, emit, save, weather });

  window.fetch = async (url, opts = {}) => {
    const u = new URL(typeof url === "string" ? url : url.url, location.href);
    if (u.origin !== location.origin || !u.pathname.startsWith("/api/")) return realFetch(url, opts);
    let body = {};
    try { body = JSON.parse(opts.body || "{}"); } catch {}
    return new Response(JSON.stringify(await demo.api((opts.method || "GET").toUpperCase(), u.pathname, u.search, body)), { headers: { "Content-Type": "application/json" } });
  };

  function speak(text) {
    const synth = window.speechSynthesis;
    if (!synth || demo.settings().quiet_mode) return wait(900);
    synth.cancel();
    const voice = new SpeechSynthesisUtterance(text);
    voice.lang = "es-ES";
    return new Promise(done => { voice.onend = voice.onerror = done; setTimeout(done, 15_000); synth.speak(voice); });
  }
  function keep(m) { state.history = [...state.history, m].slice(-40); save(state); }

  async function reply(text, spoken) {
    const id = ++run;
    keep({ role: "user", text });
    emit({ type: "user", role: "user", text });
    emit({ type: "state", state: "thinking" });
    const answer = await demo.ask(text);
    await wait(500);
    if (id !== run) return;
    await say(answer, id, spoken);
  }
  async function say(answer, id, spoken) {
    keep({ role: "assistant", text: answer });
    emit({ type: "state", state: "speaking" });
    emit({ type: "assistant_start" });
    for (const word of answer.split(/(?<= )/)) { if (id === run) emit({ type: "assistant_delta", text: word }); await wait(25); }
    emit({ type: "assistant_end", text: answer });
    if (spoken && id === run) await speak(answer);
    if (id === run) emit({ type: "state", state: "idle" });
  }
  async function listen() {   // el micro de la demo: una frase de ejemplo, sin tocar el micrófono
    const id = ++run;
    emit({ type: "notice", text: "Demo: frase de ejemplo, tu micrófono no se usa." });
    emit({ type: "state", state: "listening" });
    for (let i = 0; i < 12 && id === run; i++) { emit({ type: "level", level: Math.random() * .06 }); await wait(110); }
    if (id === run) reply(PHRASES[phrase++ % PHRASES.length], true);
  }

  class DemoSocket {
    static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
    readyState = 1;
    constructor() {
      sockets.push(this);
      setTimeout(() => demo.hello().forEach(m => this.onmessage?.({ data: JSON.stringify(m) })), 0);
    }
    send(raw) {
      const m = JSON.parse(raw);
      if (m.action === "press") listen();
      else if (m.action === "stop") { run++; window.speechSynthesis?.cancel(); emit({ type: "state", state: "idle" }); }
      else if (m.action === "text" && String(m.text || "").trim()) reply(String(m.text).slice(0, 500), true);
      else if (m.action === "wake_toggle") { state.settings.wake_enabled = !demo.settings().wake_enabled; save(state); emit(demo.status()); }
      else if (m.action === "new_conversation") { state.history = []; save(state); emit({ type: "cleared" }); }
      else emit({ type: "notice", text: DEMO_ONLY_APP });
    }
    close() {}
  }
  window.WebSocket = DemoSocket;

  setInterval(() => { const list = demo.due(); if (list.length) say(list.join(" "), ++run, true); }, 1000);

  // ---------- barra, etiquetas y lo que aquí no se hace ----------
  const works = DEMO_WORKS.map(w => `.card[data-w="${w}"] .card-head h2::after`).join(",");
  document.head.insertAdjacentHTML("beforeend", `<style>
    .card[data-w] .card-head h2::after, .demo-tag { content: "Concepto"; padding: 2px 8px; border-radius: 99px; font-size: .68rem; font-weight: 700; background: var(--card-2); color: var(--muted); }
    ${works} { content: "Funciona aquí"; background: var(--accent-soft); color: var(--accent); }
    .demo-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 14px; padding: 8px 14px; margin: 6px 0 0; border-radius: 14px; background: var(--card); box-shadow: var(--shadow); font-size: .88rem; }
    .demo-bar a, .demo-bar button { color: var(--accent); font: inherit; font-weight: 700; text-decoration: underline; text-underline-offset: 3px; }
    .demo-bar > :first-child { margin-right: auto; }
  </style>`);
  document.body.insertAdjacentHTML("afterbegin", `<div class="demo-bar" role="note"><span><b>Demo web</b> · datos inventados, guardados solo en este navegador. Las tarjetas «Concepto» enseñan la idea; funcionan en el programa.</span>
    <a href="/">Inicio y descarga</a><a href="/demo/gestures.html">Probar los gestos</a><button type="button" id="demoReset">Reiniciar</button></div>`);
  document.getElementById("demoReset").onclick = () => { localStorage.removeItem(KEY); sessionStorage.removeItem("setupStep"); location.reload(); };

  addEventListener("click", e => {
    if (e.target.closest('a[href="/spotify/login"]')) {
      e.preventDefault();
      state.spotify.connected = true;
      save(state);
      demo.hello().filter(m => m.type === "spotify").forEach(emit);
      emit({ type: "notice", text: "Demo: cuenta simulada, no se ha iniciado sesión en Spotify." });
    } else if (e.target.closest("#cameraBtn, [data-open-camera]")) {
      e.preventDefault();
      e.stopImmediatePropagation();
      location.href = "/demo/gestures.html";
    }
  }, true);
  // Ninguna contraseña se puede escribir en la demo (tampoco en formularios que se crean después)
  const noPasswords = () => document.querySelectorAll('input[type="password"]:not(:disabled)').forEach(i => { i.value = ""; i.disabled = true; i.placeholder = "Desactivado en la demo"; });
  noPasswords();
  new MutationObserver(noPasswords).observe(document.body, { childList: true, subtree: true });
})();
