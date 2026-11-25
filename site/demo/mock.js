"use strict";
// Web demo: acts as the Cortex server inside the browser. The panel (/static/app.js) is the same one the program uses.
// Everything is made up and stored only in this browser (localStorage); no account is ever signed in.

const DEMO_WORKS = ["today", "tasks", "avisos", "timers", "compra", "gym", "meals", "betis"];   // the rest: concept only
const DEMO_ONLY_APP = "Only in the Windows app: here you just see the idea.";
const PHRASES = ["What do I have today?", "Set a timer for 5 minutes", "Add bread to the list", "What's the weather like?"];
const EXAMPLES = "Try: “add milk to the list”, “timer for 5 minutes”, “add a task call the dentist”, “remind me in 2 minutes to drink water”, “what's the weather like?” or “dark mode”.";

// Same as CODES in src/hub/weather.py
function wmo(code) {
  const at = [[0, "Clear", "sun"], [1, "Mostly clear", "sun"], [2, "Partly cloudy", "cloud-sun"], [3, "Overcast", "cloud"],
    [48, "Fog", "fog"], [57, "Drizzle", "drizzle"], [67, "Rain", "rain"], [77, "Snow", "snow"], [82, "Showers", "rain"],
    [86, "Snow", "snow"], [99, "Thunderstorm", "storm"]].find(([max]) => code <= max);
  return at ? { text: at[1], icon: at[2] } : { text: "", icon: "cloud" };
}

function parseAmount(text) {   // "5 minutes", "half an hour", "un minuto" (Spanish too) → seconds
  const t = text.toLowerCase();
  if (/media hora|half an hour/.test(t)) return 1800;
  const m = t.match(/\b(\d+|un|una|dos|tres|cuatro|cinco|diez|quince|veinte|treinta|a|an|one|two|three|four|five|ten|fifteen|twenty|thirty)\s*(segundo|second|sec\b|minuto|minute|min\b|hora|hour|hr\b)/);
  if (!m) return 0;
  const words = { un: 1, una: 1, a: 1, an: 1, one: 1, dos: 2, two: 2, tres: 3, three: 3, cuatro: 4, four: 4, cinco: 5, five: 5, diez: 10, ten: 10, quince: 15, fifteen: 15, veinte: 20, twenty: 20, treinta: 30, thirty: 30 };
  return (words[m[1]] || Number(m[1])) * (/^(seg|sec)/.test(m[2]) ? 1 : /^(hora|hour|hr)/.test(m[2]) ? 3600 : 60);
}

function freshState(now) {
  const t = Math.floor(now / 1000);
  return {
    settings: {}, layout: [], timers: [], nextId: 20,
    // a morning that already happened, so the panel looks used
    history: [
      { role: "user", text: "Good morning, what do I have today?" },
      { role: "assistant", text: "Morning! Focus block until 11, the design review at 11:30 and gym at 18:30. Two emails need a reply." },
      { role: "user", text: "Remind me to call the garage after lunch" },
      { role: "assistant", text: "Done: I'll remind you this afternoon to call the garage." },
      { role: "user", text: "Put on something to focus" },
      { role: "assistant", text: "Playing Golden Hour Drive on the laptop." },
    ],
    tasks: [
      { i: 1, tarea: "Finish the slides for Friday's demo", cuando: "Friday", estado: "in progress", prioridad: "alta", notas: "Add the latency chart" },
      { i: 2, tarea: "Call the garage about the MOT", cuando: "Today", estado: "", prioridad: "alta", notas: "" },
      { i: 3, tarea: "Renew the gym membership", cuando: "This week", estado: "", prioridad: "media", notas: "" },
      { i: 4, tarea: "Review Ana's pull request", cuando: "Tomorrow", estado: "", prioridad: "media", notas: "" },
      { i: 5, tarea: "Book flights for the wedding", cuando: "", estado: "", prioridad: "media", notas: "Compare Friday vs Saturday" },
      { i: 6, tarea: "Back up the photos to the NAS", cuando: "", estado: "", prioridad: "baja", notas: "" },
      { i: 7, tarea: "Read chapter 4 of the RL book", cuando: "", estado: "in progress", prioridad: "baja", notas: "" },
    ],
    compra: ["Oat milk", "Eggs", "Spinach", "Greek yoghurt", "Bananas", "Coffee beans", "Chicken thighs", "Rice"].map((text, k) => ({ text, done: k === 5 })),
    avisos: [
      { id: "a1", at: now + 45 * 60e3, texto: "Stand up and stretch", repetir: "" },
      { id: "a2", at: now + 3.5 * 3600e3, texto: "Call the garage", repetir: "" },
      { id: "a3", at: now + 7 * 3600e3, texto: "Take out the recycling", repetir: "weekly" },
      { id: "a4", at: now + 26 * 3600e3, texto: "Water the plants", repetir: "" },
    ],
    spotify: { connected: true, client_id: "demo", playing: true, track: 0, volume: 55, progress: 96_000, duration: 231_000, device: "Laptop" },
    chats: [
      { jid: "c1", name: "Weekend crew", real: "Weekend crew", group: true, muted: false,
        messages: [{ id: "1", from: "Lucy", text: "Paddle surf on Saturday?", time: t - 7200 }, { id: "2", from: "Mark", text: "In! 11 at the beach?", time: t - 6900 },
          { id: "3", from: "Lucy", text: "Perfect, I'll bring the boards", time: t - 6600 }] },
      { jid: "c2", name: "Mum", real: "Mum", group: false, muted: false,
        messages: [{ id: "4", from: "Mum", text: "Are you coming for lunch on Sunday?", time: t - 3000 }] },
      { jid: "c3", name: "Football 5-a-side", real: "Football 5-a-side", group: true, muted: false,
        messages: [{ id: "5", from: "Dani", text: "We need one more for Thursday 21:00", time: t - 12_000 }] },
      { jid: "c4", name: "Bakery", real: "Bakery", group: false, muted: true,
        messages: [{ id: "6", from: "Bakery", text: "Your order is ready", time: t - 90_000 }] },
    ],
  };
}

// invented artists, so no real album is implied
const TRACKS = [["Golden Hour Drive", "Marlow Keys", "#1db954"], ["Paper Lanterns", "Ivy & the Coast", "#7c3aed"], ["Night Bus", "Okabe", "#2563eb"],
  ["Slow Sunday", "The Lindens", "#ea580c"], ["Glasshouse", "Nora Vale", "#db2777"], ["Tidal", "Southpaw Club", "#0891b2"]];
const art = (label, color) => "data:image/svg+xml," + encodeURIComponent(
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 90"><rect width="160" height="90" fill="${color}"/><text x="80" y="54" font-size="28" text-anchor="middle">${label}</text></svg>`);

const MAILS = [
  { id: "m1", account: "principal", from: "Ana Ruiz", addr: "ana@example.com", subject: "Re: design review moved to 11:30", ago: 1500 },
  { id: "m2", account: "principal", from: "Garage", addr: "bookings@example.com", subject: "Your MOT is due on the 28th", ago: 5400 },
  { id: "m3", account: "principal", from: "Airline", addr: "no-reply@example.com", subject: "Price alert: Seville to London from 39 EUR", ago: 14_000 },
  { id: "m4", account: "principal", from: "Library", addr: "alerts@example.com", subject: "Your reservation is ready to collect", ago: 30_000 },
  { id: "m5", account: "principal", from: "Hiking club", addr: "club@example.com", subject: "Sunday route: Grazalema loop, 14 km", ago: 70_000 },
];

/** The "server". `emit` sends a message to the panel; `weather(place)` fetches the weather. No browser dependencies: it is tested with node. */
function createDemo({ state, defaults, emit = () => {}, save = () => {}, clock = () => Date.now(), weather = async () => ({ ok: false }) }) {
  const S = state;
  const settings = () => ({ ...defaults, ...S.settings });
  const day = offset => new Date(clock() + offset * 864e5).toLocaleDateString("en-GB", { weekday: "short", day: "numeric" });
  const changed = (type, extra) => { save(S); emit({ type, ...extra }); };

  function data() {
    const week = ["Legs", "Rest", "Back", "Chest", "Rest", "Easy run", "Rest"];
    const meals = [["Lentil stew", "lunch and dinner"], ["Roast chicken and potatoes", "lunch"], ["Pasta with pesto", "lunch and dinner"],
      ["Salmon and rice", "lunch"], ["Chickpea curry", "lunch and dinner"], ["Homemade pizza", "dinner"], ["Paella", "lunch"]];
    const wd = (new Date(clock()).getDay() + 6) % 7;
    return {
      ok: true,
      today: { mode: "Work day", day: "", items: [
        { hora: "08:00", fin: "08:30", que: "Breakfast" }, { hora: "09:00", fin: "11:00", que: "Focus block" },
        { hora: "11:30", fin: "12:15", que: "Design review" }, { hora: "14:00", fin: "15:00", que: "Lunch" },
        { hora: "15:30", fin: "17:30", que: "Pull requests" }, { hora: "18:30", fin: "19:45", que: "Gym" }, { hora: "21:00", fin: "22:00", que: "Dinner" }] },
      tasks: S.tasks,
      betis: [{ partido: "Betis vs Valencia", cuando: day(3), hora: "21:00" }, { partido: "Girona vs Betis", cuando: day(9), hora: "18:30" },
        { partido: "Betis vs Celta", cuando: day(16), hora: "16:15" }],
      gym: { today: week[wd] === "Rest" ? "" : week[wd], tomorrow: week[(wd + 1) % 7], notes: ["Squat 3x8 at 70 kg last time", "Stretch 10 min after"],
        week: week.map((que, i) => ({ dia: ["L", "M", "X", "J", "V", "S", "D"][i], que, hoy: i === wd })) },
      menu: meals.map(([plato, cubre], i) => ({ dia: day(i), plato, cubre, hoy: i === 0 })),
      money: { colchon: 3200, queda: 610, gastos: 1040, ingresos: 1650 },
      dates: [{ cuando: day(2), que: "Lucy's birthday" }, { cuando: day(5), que: "Dentist, 10:00" }, { cuando: day(12), que: "Wedding in Cadiz" }],
    };
  }

  function spotify() {
    const [title, artist, color] = TRACKS[S.spotify.track];
    return S.spotify.connected ? { ...S.spotify, title, artist, image: art("♪", color) } : { ...S.spotify, playing: false };
  }

  const avisos = () => [...S.avisos].sort((a, b) => a.at - b.at).map(a => ({ id: a.id, texto: a.texto, repetir: a.repetir,
    di: new Date(a.at).toLocaleString("en-GB", { weekday: "short", hour: "2-digit", minute: "2-digit" }) }));
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
  const devices = () => ({ music: [{ id: "d1", name: "Laptop", type: "Computer", active: true }, { id: "d2", name: "Living room speaker", type: "Speaker", active: false }],
    lan: [{ name: "Living room TV", kinds: ["Chromecast"], ip: "192.0.2.10" }], scanned_at: clock() / 1000 });
  const settingsPage = () => ({ settings: settings(), devices: { inputs: ["Microphone (demo)"], outputs: ["Speakers (demo)"] }, options: {
    voices: { es: ["es-ES-XimenaNeural", "es-ES-ElviraNeural", "es-ES-AlvaroNeural"], en: ["en-US-AvaMultilingualNeural"] },
    stt: ["parakeet", "whisper-small", "whisper-base"], claude: ["haiku", "sonnet", "opus"] } });
  const status = () => ({ type: "status", mic: { ok: true, name: "simulated", muted: false }, speaker: { ok: true, name: "browser voice" },
    claude: { ok: true, plan: null, model: settings().claude_model }, wake: settings().wake_enabled, wake_samples: 0, last_heard: "", wake_method: "detector" });

  /** The demo assistant: a few simple commands (English or Spanish) really work; anything else gets a note that there is no Claude here. */
  async function ask(text) {
    const t = text.trim().replace(/[¿?¡!.]+/g, "").replace(/^cortex[, ]+/i, ""), low = t.toLowerCase();
    let m;
    if (/temporizador|timer/.test(low)) {
      const s = parseAmount(low);
      if (!s) return "For how long? For example: timer for 5 minutes.";
      addTimer(s);
      return `Done, timer set for ${s < 60 ? `${s} seconds` : s % 60 ? `${Math.round(s / 60)} minutes` : s === 60 ? "1 minute" : `${s / 60} minutes`}.`;
    }
    if ((m = t.match(/(?:recu[eé]rdame|remind me) (.+)/i))) {
      const s = parseAmount(m[1]);
      const what = m[1].replace(/\b(en|dentro de|in|within)\s+((\d+|un|una|dos|tres|cuatro|cinco|diez|quince|veinte|treinta|a|an|one|two|three|four|five|ten|fifteen|twenty|thirty)\s*(segundos?|minutos?|min|horas?|seconds?|minutes?|hours?|hrs?)|media hora|half an hour)\b/i, "").replace(/^\s*(que|de|to|that)\s+/i, "").replace(/\s+/g, " ").trim();
      if (!s || !what) return "Tell me when and what: “remind me in 10 minutes to take the pizza out”.";
      addAviso(what, clock() + s * 1000);
      return `I'll remind you: ${what}.`;
    }
    if ((m = t.match(/(?:añade|apunta|pon|mete|add|put)\s+(.+?)\s+(?:a|en|to|on|in)\s+(?:la|the|my)\s+(?:shopping\s+|grocery\s+)?(?:lista|compra|list)/i))) { addCompra(m[1]); return `Added to the shopping list: ${m[1]}.`; }
    if ((m = t.match(/(?:apunta|añade|crea|nueva|add|create|new)\s+(?:la |una |a |the )?(?:tarea|task):?\s+(.+)/i))) { addTask({ tarea: m[1] }); return `Task added: ${m[1]}.`; }
    if (/lista|compra|shopping|grocer|\blist\b/.test(low) && !/tarea|task/.test(low)) { const left = S.compra.filter(i => !i.done).map(i => i.text); return left.length ? `Still to buy: ${left.join(", ")}.` : "The list is empty."; }
    if (/modo oscuro|modo claro|tema|dark mode|light mode|theme/.test(low)) {
      const theme = /oscuro|dark/.test(low) ? "dark" : /claro|light/.test(low) ? "light" : settings().theme === "dark" ? "light" : "dark";
      S.settings.theme = theme;
      changed("settings", { settings: settings() });
      return theme === "dark" ? "Dark mode." : "Light mode.";
    }
    if (/tiempo|llov|lluvia|temperatura|weather|rain|temperature|forecast/.test(low)) {
      const w = await weather(settings().weather_place);
      return w.ok ? `In ${w.place}: ${w.text.toLowerCase()}, ${w.temp} degrees.` : "I can't reach the weather service right now.";
    }
    if (/(m[uú]sica|canci[oó]n|spotify|music|song)/.test(low)) {
      S.spotify.playing = !/para|pausa|quita|\b(stop|pause)\b/.test(low);
      changed("spotify", spotify());
      return S.spotify.playing ? "Playing. In the app this plays on your Spotify." : "Music stopped.";
    }
    if (/tareas?|tasks?/.test(low)) return S.tasks.length ? `You have ${S.tasks.length} tasks. The first one: ${S.tasks[0].tarea}.` : "You have no tasks.";
    if (/qu[eé] tengo|mi d[ií]a|resumen|matinal|buenos d[ií]as|what do i have|my day|summary|briefing|good morning/.test(low)) {
      const d = data();
      return `Today: ${d.today.items.map(i => `${i.que.toLowerCase()} at ${i.hora}`).join(", ")}. You have ${S.tasks.length} tasks and ${MAILS.length} unread emails.`;
    }
    if (/qu[eé] hora|what time/.test(low)) return `It's ${new Date(clock()).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}.`;
    return `This is the demo: it understands a few commands and does not use Claude. ${EXAMPLES}`;
  }

  /** Reminders and timers that are due. Returns what should be said. */
  function due() {
    const now = clock(), say = [];
    for (const t of S.timers.filter(t => t.end * 1000 <= now)) { say.push(t.label ? `Time's up: ${t.label}.` : "The timer is done."); emit({ type: "timer_done" }); }
    const aviso = S.avisos.filter(a => a.at <= now);
    if (say.length) { S.timers = S.timers.filter(t => t.end * 1000 > now); changed("timers", { timers: S.timers }); }
    if (aviso.length) {
      for (const a of aviso) {
        say.push(`Reminder: ${a.texto}.`);
        // shortcut: every repeat moves to the next day; build the real calendar if the demo ever stores real reminders
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
      if (b.action !== "delete" && b.action !== "done" && !String(b.tarea || "").trim()) return { ok: false, error: "Enter the task." };
      if (b.action === "add") addTask(b);
      else if (!t) return { ok: false, error: "That task is gone." };
      else if (b.action === "edit") Object.assign(t, { tarea: String(b.tarea).slice(0, 200), cuando: String(b.cuando || "").slice(0, 80), prioridad: ["alta", "media", "baja"].includes(b.prioridad) ? b.prioridad : "media" });
      else S.tasks = S.tasks.filter(x => x !== t);
      save(S);
      return { ok: true, data: data() };
    },
    "GET /api/bank": () => ({ ok: true, fetched_at: clock() / 1000, accounts: [{ balance: { amount: 842.5 }, recent: [
      ...[["Supermarket", -23.4, 0], ["Coffee shop", -3.2, 0], ["Gym membership", -34.9, 1], ["Train tickets", -18.6, 2], ["Salary", 1650, 3], ["Electricity", -41.75, 4]]
        .map(([description, amount, d]) => ({ date: new Date(clock() - d * 864e5).toISOString().slice(0, 10), description, amount, pending: d === 0 }))] }] }),
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
      if (!String(b.texto || "").trim()) return { error: "Enter the reminder." };
      if (!(at > clock())) return { error: "Pick a time in the future." };
      addAviso(String(b.texto).trim(), at, b.repetir || "");
      return { ok: true };
    },
    "POST /api/avisos/delete": b => { S.avisos = S.avisos.filter(a => a.id !== b.id); changed("avisos", { items: avisos() }); return { ok: true }; },
    "GET /api/weather": () => weather(settings().weather_place),
    "POST /api/weather/place": async b => {
      const place = await weather.find?.(String(b.name || "").trim());
      if (!place) return { error: "I can't find that place." };
      S.settings.weather_place = place;
      save(S);
      return weather(place);
    },
    "GET /api/mail": () => ({ configured: true, accounts: [{}], error: "", checked_at: clock() / 1000,
      slots: [{ slot: "p", label: "Main", user: "sample@example.com", ok: true }],
      unread: MAILS.map(m => ({ ...m, time: clock() / 1000 - m.ago })) }),
    "GET /api/mail/open": (_, q) => { const m = MAILS.find(x => x.id === q.get("id")); return m ? { subject: m.subject, from: `${m.from} <${m.addr}>`, date: new Date(clock() - m.ago * 1000).toISOString(), files: [] } : { error: "That email is gone." }; },
    "GET /api/whatsapp": whatsapp,
    "GET /api/whatsapp/chat": (_, q) => { const c = S.chats.find(x => x.jid === q.get("jid")); return c ? chat(c) : { error: "No chat", messages: [] }; },
    "POST /api/whatsapp/mute": b => { const c = S.chats.find(x => x.jid === b.jid); if (c) { c.muted = !!b.muted; changed("whatsapp", whatsapp()); } return { ok: true }; },
    "POST /api/whatsapp/mote": b => { const c = S.chats.find(x => x.real === b.name); if (c) { c.mote = String(b.mote || "").trim().slice(0, 60); changed("whatsapp", whatsapp()); } return { ok: true }; },
    "POST /api/whatsapp/send": b => {
      const c = S.chats.find(x => x.jid === b.jid), text = String(b.text || "").trim().slice(0, 4000);
      if (!c || !text || settings().whatsapp_mode !== "write") return { ok: false, error: "Cannot send." };
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
    "GET /api/youtube": (_, q) => ({ ok: true, results: ["Full highlights", "Press conference", "Tactical analysis"].map((title, i) => ({
      id: "demo" + i, title: `${title}: ${q.get("q") || ""}`, thumb: art("▶", TRACKS[i][2]), length: `${i + 2}:30`, channel: "Football Daily" })) }),
    "GET /api/video/services": () => ({ services: settings().video_services.filter(s => s.enabled).map(({ name, url }) => ({ name, url })) }),
    "POST /api/video": () => { emit({ type: "notice", text: DEMO_ONLY_APP }); return no; },
    "GET /api/web/accounts": () => ({ accounts: [] }),
    "POST /api/quiet": b => { S.settings.quiet_mode = !!b.on; changed("settings", { settings: settings() }); return { quiet: !!b.on }; },
    "GET /api/file": () => ({ ok: true, name: "demo", file: "demo.yaml", text: "# In the app you edit your files here (tasks, menu, gym…).\n" }),
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

// ---------- in the browser ----------
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
      const g = (await (await realFetch("https://geocoding-api.open-meteo.com/v1/search?" + new URLSearchParams({ name, count: 1, language: "en" }))).json()).results?.[0];
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
    voice.lang = "en-US";
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
  async function listen() {   // the demo mic: one sample phrase, without touching the microphone
    const id = ++run;
    emit({ type: "notice", text: "Demo: sample phrase, your microphone is not used." });
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

  // ---------- bar, labels and what does not happen here ----------
  const works = DEMO_WORKS.map(w => `.card[data-w="${w}"] .card-head h2::after`).join(",");
  document.head.insertAdjacentHTML("beforeend", `<style>
    .card[data-w] .card-head h2::after, .demo-tag { content: "Concept"; padding: 2px 8px; border-radius: 99px; font-size: .68rem; font-weight: 700; background: var(--card-2); color: var(--muted); }
    ${works} { content: "Works here"; background: var(--accent-soft); color: var(--accent); }
    .demo-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 14px; padding: 8px 14px; margin: 6px 0 0; border-radius: 14px; background: var(--card); box-shadow: var(--shadow); font-size: .88rem; }
    .demo-bar a, .demo-bar button { color: var(--accent); font: inherit; font-weight: 700; text-decoration: underline; text-underline-offset: 3px; }
    .demo-bar > :first-child { margin-right: auto; }
  </style>`);
  document.body.insertAdjacentHTML("afterbegin", `<div class="demo-bar" role="note"><span><b>Web demo</b> · made-up data, stored only in this browser. “Concept” cards show the idea; they work in the app.</span>
    <a href="/hub/">About Hub</a><button type="button" id="demoReset">Reset</button></div>`);
  document.getElementById("demoReset").onclick = () => { localStorage.removeItem(KEY); sessionStorage.removeItem("setupStep"); location.reload(); };

  addEventListener("click", e => {
    if (e.target.closest('a[href="/spotify/login"]')) {
      e.preventDefault();
      state.spotify.connected = true;
      save(state);
      demo.hello().filter(m => m.type === "spotify").forEach(emit);
      emit({ type: "notice", text: "Demo: simulated account, you are not signed in to Spotify." });
    } else if (e.target.closest("#cameraBtn, [data-open-camera]")) {
      e.preventDefault();
      e.stopImmediatePropagation();
      location.href = "/sense/";
    }
  }, true);
  // No password can be typed in the demo (not even in forms created later)
  const noPasswords = () => document.querySelectorAll('input[type="password"]:not(:disabled)').forEach(i => { i.value = ""; i.disabled = true; i.placeholder = "Disabled in the demo"; });
  noPasswords();
  new MutationObserver(noPasswords).observe(document.body, { childList: true, subtree: true });
})();
