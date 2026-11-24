"""Local server and orchestrator: hearing → transcription → router/Claude → voice."""

import asyncio
import json
import logging
import os
import random
import re
import time
from datetime import date, datetime
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import webaccounts
from . import audio, avisos, bank, compra, datos, music, search, timers, tts, video_sites, weather, youtube
from .video import VideoPlayer
from .webplayer import WebPlayer
from .devices import Devices
from .mail import Mail, save_env
from .whatsapp import WhatsApp
from .brain import one_shot
from .brain import Brain, BrainError, claude_login, claude_status
from .config import MODELS_DIR, ROOT, WEB_DIR, settings

WHO = settings["user_name"] or "the user"   # configurable name in config.yaml (user_name)
from .listener import BargeIn, Listener, find_wake
from .router import is_ack, route
from .spotify import LIKED, Spotify, SpotifyError, norm
from .stt import MODELS as STT_MODELS, Transcriber, guess_lang, normalize, save_clip
from .sense_link import SenseLink

log = logging.getLogger("hub")

EDGE_VOICES = {
    # the multilingual ones are the newer generation (more natural) and speak Spanish; Andrew is dropped: slow and cuts sentences off
    "es": ["en-US-BrianMultilingualNeural", "en-US-EmmaMultilingualNeural", "es-ES-XimenaNeural", "es-ES-ElviraNeural",
           "es-ES-AlvaroNeural", "es-MX-DaliaNeural", "es-MX-JorgeNeural", "en-US-AvaMultilingualNeural"],
    "en": ["en-US-AvaMultilingualNeural", "en-US-EmmaMultilingualNeural", "en-US-BrianMultilingualNeural",
           "en-US-AndrewMultilingualNeural", "en-GB-SoniaNeural", "en-GB-RyanNeural"],
}
CLAUDE_MODELS = ["haiku", "sonnet", "opus"]
SEARCH_TOOLS = {"WebSearch", "WebFetch"}
FILLERS = {
    "es": ["Dame un segundo, que lo busco.", "Espera, que lo miro.", "Un momento, que lo busco."],
    "en": ["Give me a sec, I'll look it up.", "Hang on, let me check."],
}
MUSIC_TAG = re.compile(r"\[\[\s*m[uú]sica\s*:\s*(.+?)\]\]", re.I | re.S)
ANY_TAG = re.compile(r"\[\[.*?\]\]", re.S)   # system tags: never read aloud or shown
SENTENCE_END = re.compile(r"(.+?[.!?…:;](?=\s)|.+?\n)", re.S)
LAYOUT_FILE = ROOT / "layout.json"


# the detector fires before the word ends: «…tex, pon música» or «Cortex, pon música» → «pon música»
WAKE_TAIL = re.compile(r"^\s*(?:[ck][oó]r)?te(?:x|[ck]s)\b[\s,.;:!?]*", re.I)


def strip_wake(text: str) -> str:
    rest = find_wake(text, 2)
    if rest:
        text = rest
    return WAKE_TAIL.sub("", text, count=1).strip()


# «añade leche y huevos a la lista (de la compra)» (add to shopping list) · «qué hay en la lista de la compra» (read it)
COMPRA_ADD_RE = re.compile(r"^(?:a[ñn]ade|apunta|pon|mete|agrega)\s+(.+?)\s+(?:a|en)\s+la\s+lista(?:\s+de\s+la\s+compra)?[.!]*$",
                           re.I)
COMPRA_LIST_RE = re.compile(r"(qu[eé] (hay|tengo|falta) en|l[eé]eme|dime) la lista de la compra|qu[eé] (tengo|hay) que comprar",
                            re.I)


# its own voice: «habla más despacio», «cambia de voz», «pon la voz de Elvira», «voz de mujer» (slower, change voice, a named voice, female voice)
VOICE_NAMES = {"alvaro": "es-ES-AlvaroNeural", "elvira": "es-ES-ElviraNeural", "ximena": "es-ES-XimenaNeural",
               "dalia": "es-MX-DaliaNeural", "jorge": "es-MX-JorgeNeural",
               "brian": "en-US-BrianMultilingualNeural", "emma": "en-US-EmmaMultilingualNeural",
               "ava": "en-US-AvaMultilingualNeural", "natural": "en-US-BrianMultilingualNeural",
               "hombre": "es-ES-AlvaroNeural", "chico": "es-ES-AlvaroNeural", "masculina": "es-ES-AlvaroNeural",
               "mujer": "es-ES-ElviraNeural", "chica": "es-ES-ElviraNeural", "femenina": "es-ES-ElviraNeural",
               "mexicana": "es-MX-DaliaNeural", "mexicano": "es-MX-JorgeNeural"}
QUIET_OFF_RE = re.compile(r"(quita|desactiva|apaga|sal del?) (el )?modo silencio|ya puedes (molestar|avisar)", re.I)
QUIET_ON_RE = re.compile(r"^(?!.*(quita|desactiva|apaga|sal)).*(modo silencio|no molestes|no me molestes)", re.I)
FASTER_RE = re.compile(r"\bhabla\w*\b.*\bm[aá]s (r[aá]pido|deprisa)|\bve m[aá]s r[aá]pido", re.I)
SLOWER_RE = re.compile(r"\bhabla\w*\b.*\bm[aá]s (despacio|lento)|\bve m[aá]s despacio", re.I)
NORMAL_RATE_RE = re.compile(r"velocidad normal|habla (a velocidad )?normal", re.I)
VOICE_RE = re.compile(r"\b(?:cambia(?:te)?|pon(?:te)?|usa)\b.{0,15}\bvoz\b(?:.{0,12}?\b(?P<name>" + "|".join(VOICE_NAMES)
                      + r")\b)?",
                      re.I)
# [[ver: URL]] or [[ver: service | search]] (the latter is resolved by video_sites using the site's API)
WATCH_TAG = re.compile(r"\[\[\s*ver\s*:\s*(.+?)\s*\]\]", re.I)
# without Claude: «pon el último telediario de RTVE», «pon La Revuelta de anoche en RTVE», «pon la 1», «pon antena 3 en directo»
WATCH_SITE_RE = re.compile(r"^(?:pon(?:me)?|quiero ver|echa|reproduce)\s+(?P<q>.+?)\s+(?:en|de)\s+"
                           r"(?P<s>rtve(?: play)?|pluto(?: tv)?|atres ?player)\s*[.!?]*$", re.I)
WATCH_LIVE_RE = re.compile(r"^(?:pon(?:me)?|quiero ver)\s+(?:el canal\s+)?(?P<q>la ?[12]|(?:el )?(?:canal )?24 horas|clan|"
                           r"teledeporte|antena ?3|la ?sexta|neox|nova|mega|atreseries)(?:\s+en directo)?\s*[.!?]*$", re.I)
# «pon la última de La Revuelta en RTVE», «quiero ver Stranger Things»… → Claude is given the active services
WATCH_RE = re.compile(r"\b(ver|pon(me)?|reproduce|echa)\b.*\b(serie|peli|pel[ií]cula|cap[ií]tulo|episodio|temporada|"
                      r"documental|programa|directo|canal|telediario|la 1|la 2|netflix|prime|rtve|pluto|disney|"
                      r"atres|movistar)", re.I)
# [[guardar_web: name | URL]] (without a URL: the site currently open in the video window)
SAVE_WEB_TAG = re.compile(r"\[\[\s*guardar[ _]web\s*:\s*(.+?)\]\]", re.I)
VOICE_TAG = re.compile(r"\[\[\s*voz\s*:\s*(.+?)\]\]", re.I)
# mail: [[correo: to | subject | text | account?]] and [[responder: ID | text]] (ID like p123 / s45)
MAIL_SEND_TAG = re.compile(r"\[\[\s*correo\s*:\s*(.+?)\]\]", re.I | re.S)
MAIL_REPLY_TAG = re.compile(r"\[\[\s*responder\s*:\s*([ps]\d+)\s*\|\s*(.+?)\]\]", re.I | re.S)
MAIL_ID_RE = re.compile(r"\b([ps]\d{1,9})\b")
RATE_TAG = re.compile(r"\[\[\s*velocidad\s*:\s*([+-]?\d+)\s*%?\s*\]\]", re.I)


def set_voice(name: str | None) -> str:
    """Change the Spanish voice (no name: the next one in the list). Returns the short name."""
    name = norm(name or "")
    if name in VOICE_NAMES:
        voice = VOICE_NAMES[name]
    else:
        order = list(dict.fromkeys(v for v in VOICE_NAMES.values()))
        cur = settings["voice_es"]
        voice = order[(order.index(cur) + 1) % len(order)] if cur in order else order[0]
    settings.update({"voice_es": voice, "tts_engine": "edge"})
    log.info("Voice changed to %s", voice)
    return voice_label(voice)


def voice_label(voice: str) -> str:
    """«en-US-BrianMultilingualNeural» → «Brian»."""
    return voice.split("-")[2].removesuffix("Neural").removesuffix("Multilingual")


def set_rate(rate: int) -> int:
    rate = max(-30, min(60, int(rate)))
    settings.update({"tts_rate": rate})
    log.info("Speech rate: %+d%%", rate)
    return rate


def local_tools(text: str) -> str | None:
    """Timers, shopping list and its own voice: resolved instantly, without going through Claude."""
    t = text.strip()
    if QUIET_ON_RE.search(t):
        settings.update({"quiet_mode": True})
        return "Okay, quiet mode: notices will only show in the panel."
    if QUIET_OFF_RE.search(t):
        settings.update({"quiet_mode": False})
        return "Okay, I'll announce notices out loud again."
    if FASTER_RE.search(t):
        set_rate(settings["tts_rate"] + 15)
        return "Okay, speaking faster."
    if SLOWER_RE.search(t):
        set_rate(settings["tts_rate"] - 15)
        return "Okay, speaking slower."
    if NORMAL_RATE_RE.search(t):
        set_rate(15)
        return "Okay, back to normal speed."
    m = VOICE_RE.search(norm(t))   # accent-free: «Álvaro», «cámbiate»
    if m:
        return f"Hi, I'm {set_voice(m.group("name"))} now. How do I sound?"
    if timers.CANCEL_RE.search(t):
        n = timers.cancel()
        return "Timer removed." if n == 1 else f"Removed {n} timers." if n else "There were none."
    if timers.LEFT_RE.search(t):
        act = timers.active()
        if not act:
            return "There are no timers."
        return ". ".join(f"{a['label'] + ' has' if a['label'] else 'It has'} "
                         f"{timers.say_duration(a['end'] - time.time())} left" for a in act) + "."
    parsed = timers.parse(t)
    if parsed:
        seconds, label = parsed
        timers.add(seconds, label)
        return f"Okay, {timers.say_duration(seconds)}" + (f" for {label}." if label else ".")
    m = COMPRA_ADD_RE.match(t.rstrip())
    if m:
        compra.add(m.group(1))
        return "Added."
    if COMPRA_LIST_RE.search(t):
        pending = [i["text"] for i in compra.items() if not i["done"]]
        return ("On the list: " + ", ".join(pending) + ".") if pending else "The list is empty."
    return None


# questions about the bank account: Claude is given the Revolut data (read-only)
MORNING_RE = re.compile(r"resumen (matinal|de la ma[ñn]ana|del d[ií]a)|buenos d[ií]as,? cortex|morning (summary|briefing)|good morning,? cortex", re.I)
BANK_RE = re.compile(r"revolut|saldo|movimientos|en el banco|en la cuenta|he gastado|mis gastos|presupuesto|"
                     r"c[oó]mo voy (de|con el) dinero|deber[ií]a tener",
                     re.I)
BANK_FRESH_RE = re.compile(r"actualiza|ahora mismo|en (este momento|vivo)|refresca", re.I)


# questions about gestures: Claude is given the real table (the one configured right now)
GESTURE_RE = re.compile(r"\bgestos?\b|con la mano|con las manos|la c[aá]mara|\bse[ñn]as\b|qu[eé] hago para", re.I)
GESTURE_NAMES = {
    "swipe_left": "swipe hand left", "swipe_right": "swipe hand right",
    "swipe_up": "swipe up", "swipe_down": "swipe down",
    "open_palm": "open palm", "closed_fist": "closed fist", "thumb_up": "thumb up", "thumb_down": "thumb down",
    "victory": "victory (two fingers)", "pointing_up": "index finger up", "i_love_you": "horns with thumb",
    "ok": "OK (thumb and index in a circle)", "shaka": "shaka (thumb and pinky)", "rock": "horns (index and pinky)",
    "three": "three fingers", "shush": "shush (index finger on the lips)",
    "flick_right": "index flick to the right", "flick_left": "index flick to the left",
    "circle_cw": "finger circle (clockwise)", "circle_ccw": "finger circle (counter-clockwise)",
    "wag": "finger wag (\"no\")", "wave": "wave hello", "push": "push toward the camera",
    "both_palms": "two open palms", "both_thumbs_up": "two thumbs up", "both_thumbs_down": "two thumbs down",
    "both_fists": "two fists", "hands_apart": "hands apart", "hands_together": "hands together",
}
ACTION_NAMES = {
    "next": "next song", "previous": "previous song", "toggle": "pause / resume the music",
    "volume_up": "volume up", "volume_down": "volume down", "listen": "start listening (like saying «Cortex»)",
    "stop": "stop the assistant (halts everything)", "quiet": "mute the assistant (stops talking but carries on with the task)",
    "ambient": "idle mode", "wake_toggle": "pause/enable the «Cortex» wake word",
    "new_chat": "new conversation", "theme": "switch light/dark theme",
}


def site_url(service: str) -> str | None:
    """«cuevana.biz», «https://x.tv/…» or the name of a service in Settings → the URL of its home page."""
    s = service.strip().strip("/")
    for v in settings["video_services"]:
        if v["name"].lower() == s.lower():
            return v["url"]
    if re.fullmatch(r"(https?://)?[\w-]+(\.[\w-]+)+(/\S*)?", s, re.I):
        return s if s.lower().startswith("http") else "https://" + s
    return None


def save_web(name: str, url: str | None, web) -> str:
    """Save a website as a video service under that name (Settings → Video), so «busca X en <name>» works later."""
    if not url:
        page = web._page()
        url = page.get("url") if page else None
        if not url or not url.startswith("http"):
            return "There is no open website to save."
    url = site_url(url) or url
    p = urlparse(url)
    root = f"{p.scheme}://{p.netloc}/"
    domain = ".".join(p.hostname.removeprefix("www.").split(".")[-2:]) if p.hostname else ""
    services = [v for v in settings["video_services"] if v["name"].lower() != name.lower()]
    services.append({"name": name, "url": root, "domains": [domain], "enabled": True})
    settings.update({"video_services": services})
    log.info("Website saved: %s → %s", name, root)
    return f"Saved «{name}» ({domain})."


def watch_context() -> str:
    on = [s for s in settings["video_services"] if s.get("enabled", True)]
    return ("[Enabled video services (system data): " + "; ".join(f"{s['name']} ({', '.join(s['domains'])})"
            for s in on) + ". To play something, write at the end [[ver: service | what]] (e.g. [[ver: RTVE | latest "
            "news bulletin]], [[ver: RTVE | La Revuelta from last night]], [[ver: RTVE | La 1 live]], [[ver: Pluto TV | "
            "action movie channel]], [[ver: Atresplayer | El Hormiguero from last night]]): Cortex searches the site "
            "itself, opens it and presses play. Use [[ver: URL]] only if you have the exact URL of the video page; "
            "never give the home page. RTVE live streams require an account; themed channels without one: Pluto TV. "
            "Netflix, Prime Video and Disney+: it searches the title and plays the first result. Any other video "
            "site works too: [[ver: domain.com | what]] (e.g. [[ver: dailymotion.com | Betis highlights]]) searches "
            f"that site, removes ads and pop-ups and plays the first result; if {WHO} does not give the address and "
            "you are not sure of it, ask them.]")


UPDATE_PROMPT = (
    "Automatic update (nobody is waiting for a spoken answer). 1) Use WebSearch to find Real Betis's upcoming "
    "matches (date, time, opponent, competition) and fix betis.yaml only if something has changed or is missing. "
    "2) Read tareas.yaml without changing anything and note what is due today or tomorrow. Do not touch other files. "
    f"Reply ONLY with the important news for {WHO}, each on a line starting with '- ' (max 3, very short), or exactly "
    "NOTHING. Write in the user's language (default English).")
STATE_FILE = ROOT / "logs" / "auto_update.json"
YES_RE = __import__("re").compile(r"^(s[ií]|yes|yeah|sure|please|vale|venga|dale|claro|porfa|por favor|res[uú]melo|l[eé]emelo|ok|okey)\b", 2)


def claude_usage(plan: dict) -> dict:
    """What is left of the Claude plan (5 h window), from the latest limits notice that claude itself sends."""
    win = ((plan or {}).get("unifiedWindows") or {}).get("five_hour") or {}
    if "utilization" not in win:
        return {}
    resets = datetime.fromtimestamp(win["resetsAt"]).strftime("%H:%M") if win.get("resetsAt") else ""
    week = ((plan.get("unifiedWindows") or {}).get("seven_day") or {}).get("utilization")
    return {"left": max(0.0, 100 - win["utilization"] * 100), "resets": f"at {resets}" if resets else "",
            "week_left": None if week is None else max(0.0, 100 - week * 100)}


def web_version() -> int:
    """Changes when the panel (html/js/css) is touched: the open panel reloads itself."""
    return int(max(p.stat().st_mtime for p in WEB_DIR.iterdir() if p.is_file()))


def gesture_context(vision: "SenseLink | None") -> str:
    actions = settings["gesture_actions"]
    used = [f"- {GESTURE_NAMES.get(g, g)} → {ACTION_NAMES.get(a, a)}" for g, a in actions.items() if a and a != "none"]
    free = [GESTURE_NAMES.get(g, g) for g, a in actions.items() if not a or a == "none"]
    custom = vision.list_custom() if vision else []
    lines = [f"[Gestures configured right now (system data, not said by {WHO}):"] + used
    for c in custom:
        a = actions.get(c["id"], "none")
        lines.append(f"- custom gesture «{c['name']}» → {ACTION_NAMES.get(a, 'nothing')}")
    lines.append(f"- Free (no action): {', '.join(free)}")
    lines.append("- Cortex Sense (the gesture app) " + ("connected" if vision and vision.connected else "off")
                 + (". They only count when looking at the screen" if settings["gestures_require_look"] else "")
                 + ". To change them or record a custom one: in the panel, Settings (the gear, top right) → Gestures "
                 "tab, or the Cortex Sense panel (http://127.0.0.1:8766), which also calibrates the camera and assigns "
                 "keys, programs and windows. You cannot change them yourself.]")
    return "\n".join(lines)


def bank_context(text: str) -> str:
    """Block with the balance and latest transactions to append to the question (empty if no bank is connected)."""
    data = bank.summary(force=bool(BANK_FRESH_RE.search(text)))
    if not data.get("ok") and not data.get("accounts"):
        return ""
    when = datetime.fromtimestamp(data.get("fetched_at", time.time())).strftime("%d/%m %H:%M")
    lines = [f"[Revolut data (read-only), fetched on {when}"
             + (", may be out of date" if data.get("stale") else "") + ":"]
    for a in data.get("accounts", []):
        b = a.get("balance") or {}
        lines.append(f"- Account {a.get('masked')}: balance {b.get('amount')} {b.get('currency')}")
        for t in a.get("transactions", [])[:10]:
            lines.append(f"  {t['date']}  {t['amount']:+.2f} {t['currency']}  {t['description']}"
                         + (" (pending)" if t.get("pending") else ""))
    for a in data.get("accounts", []):
        sp = a.get("spending") or {}
        if sp:
            cats = ", ".join(f"{k} {v:.0f} €" for k, v in sp.get("categorias", [])[:6])
            lines.append(f"- Card spending this month ({sp['mes']}, day {sp['dia']} of {sp['dias_mes']}): "
                         f"{sp['gastado']:.0f} € ({sp['media_diaria']:.0f} €/day, at this rate {sp['proyeccion']:.0f} € "
                         f"by month end; last month {sp['mes_anterior']:.0f} €). By category: {cats}")
    if data.get("consent_days") is not None:
        lines.append(f"- The bank consent expires in {data['consent_days']} days]")
    else:
        lines[-1] += "]"
    return "\n".join(lines)


class Assistant:
    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[WebSocket] = set()
        self.state = "idle"
        self.player = audio.Player()
        self.mic = audio.Mic()
        self.stt = Transcriber()
        self.brain = Brain()
        self.estado = datos.EstadoWriter()
        self.vision: SenseLink | None = None
        self._retraining = False
        self._retrained_day = None
        self._vision_sent: dict = {}
        self.listener: Listener | None = None
        self.barge: BargeIn | None = None
        self.video = VideoPlayer()
        self.web = WebPlayer()
        self.devices: Devices | None = None
        self.mail = Mail()
        self.wa = WhatsApp()
        self._offer: tuple[float, str] | None = None   # «do you want a summary?»: (time, what would be done)
        self.notices: list[dict] = []
        self._notice_lock = asyncio.Lock()
        self._video_status: dict = {"playing": False}
        self.history: list[dict] = []
        self._turn: asyncio.Task | None = None
        self._sentences: asyncio.Queue = asyncio.Queue()
        self._speak_lang = "en"
        self._muted_until = 0.0
        self._outputs: str | None = None
        self._pending = ""
        self._raw = ""        # Claude's text as is, with the [[musica: …]] tags
        self._shown = ""      # what has already been shown/spoken (without tags)
        self._spoke = False   # has it already said something this turn?
        self._turn_t = 0.0    # when the turn started (0 = already spoke)
        self._gen = 0         # each turn has a number: audio from an interrupted turn is discarded
        self._quiet_gen = -1   # turn that was told to be quiet (shush gesture): it continues, but silently
        self._enroll_left = 0  # «Cortex» samples still to record
        self.mic_mute = audio.WindowsMicMute()
        self.spotify = Spotify(settings["ui_port"])
        self._duck_lock = asyncio.Lock()
        self._no_duck = False   # during a music command the volume is not ducked
        self._followup = False  # is the current listening the one after a reply?
        self._tasks: set[asyncio.Task] = set()

    # ---------- startup ----------
    async def start(self):
        self.loop = asyncio.get_running_loop()
        self.loop.set_exception_handler(_quiet_resets)
        keep_awake()
        timers.load()   # timers survive a restart
        self.devices = Devices()
        self.open_devices()
        self.spawn(self._start_brain())
        self.spawn(self._start_ears())   # in the background: the first time it downloads the model and the panel does not wait
        self.spawn(asyncio.to_thread(tts.preload_piper))
        self.vision = SenseLink(settings["sense_url"], on_gesture=self._threadsafe(self._on_gesture),
                                on_presence=self._threadsafe(self._on_presence))
        settings.sense = self.vision
        self.vision.start()
        self.wa.start()
        self.spawn(self._speaker())
        self.spawn(self._monitor())
        log.info("Cortex ready")

    async def _start_brain(self):
        try:   # no data folder, Claude Code not installed…: the panel starts anyway and it can be fixed from there
            datos.folder().mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(self.brain.start)
        except (OSError, BrainError) as e:
            log.warning("Claude does not start: %s", e)

    async def _start_ears(self):
        try:
            await asyncio.to_thread(self.stt.load, settings["stt_model"])
            self.listener = await asyncio.to_thread(
                Listener, self.mic, self.stt, self._threadsafe(self._on_wake),
                self._threadsafe(self._on_command), self._threadsafe(self._on_timeout))
            self.barge = BargeIn(self.mic, self.stt, self._threadsafe(self._on_barge))
        except Exception:
            log.exception("Could not load speech recognition")
            await self.broadcast({"type": "error", "text": "Could not load speech recognition. Is there an internet connection? "
                                  "Restart Cortex to retry; text input still works."})
        await self.broadcast(self.status())

    def open_devices(self):
        self.player.close()
        self.mic.close()
        audio.refresh_devices()
        for dev, key in ((self.player, "speaker_device"), (self.mic, "mic_device")):
            try:
                dev.open(settings[key])
            except Exception as e:
                log.error("Cannot open %s: %s", key, e)
                dev.ok = False

    def _threadsafe(self, coro_fn):
        """So that threads (the hearing) can launch coroutines on the main loop."""
        def call(*args):
            self.loop.call_soon_threadsafe(lambda: self.spawn(coro_fn(*args)))
        return call

    def spawn(self, coro) -> asyncio.Task:
        """Launch a fire-and-forget task keeping a reference (otherwise the garbage collector may drop it midway) and
        logging its errors (otherwise they only show up as «Task exception was never retrieved»)."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task):
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("Error in a background task", exc_info=task.exception())

    # ---------- communication with the panel ----------
    async def broadcast(self, msg: dict):
        for ws in list(self.clients):
            try:
                # with a timeout: a hung panel that does not read cannot stall the monitor loop (and the mic)
                await asyncio.wait_for(ws.send_json(msg), 5)
            except Exception:
                if ws in self.clients:
                    self.clients.discard(ws)
                    self.spawn(self._drop(ws))

    async def _drop(self, ws: WebSocket):
        """Close a panel that does not respond: if it is still alive, it reconnects by itself."""
        try:
            await asyncio.wait_for(ws.close(), 3)
        except Exception:
            pass

    def status(self) -> dict:
        return {
            "type": "status",
            "web": web_version(),
            "mic": {"ok": self.mic.ok and not self.mic_mute.muted, "name": self.mic.device_name,
                    "muted": bool(self.mic_mute.muted)},
            "speaker": {"ok": self.player.ok, "name": self.player.device_name},
            "claude": {"ok": self.brain.ok, "plan": self.brain.plan, "model": settings["claude_model"]},
            "claude_usage": claude_usage(self.brain.plan),
            "wake": settings["wake_enabled"],
            "wake_samples": self.listener.print.user_samples if self.listener else 0,
            "last_heard": self.listener.last_heard if self.listener else {},
            "wake_method": "detector" if self.listener and self.listener.detector.available else "texto",
        }

    async def set_state(self, state: str):
        if state != self.state:
            self.state = state
            if state in ("listening", "thinking"):
                tts.warm()   # the voice connection, ready for when it is time to answer
            if self.vision:
                self.vision.yield_cpu = state == "transcribing"
            await self.broadcast({"type": "state", "state": state})
            if self.spotify.connected or self.video.playing() or self._video_status.get("source") == "web":
                self.spawn(self._duck(state != "idle" and not self._no_duck))

    async def _duck(self, down: bool):
        """Lower the music while Cortex listens or speaks, and restore it afterwards."""
        async with self._duck_lock:
            try:
                if self.video.playing():
                    await asyncio.to_thread(self.video.command, "volume", -30 if down else 30)
                elif await asyncio.to_thread(self.web.active):
                    await asyncio.to_thread(self.web.command, "duck" if down else "unduck")
                await asyncio.to_thread(self.spotify.duck if down else self.spotify.unduck)
            except SpotifyError:
                pass
            except Exception:
                log.exception("Error ducking the music")

    # ---------- inputs ----------
    async def press(self):
        """Panel button."""
        if self.state == "listening":
            self.listener.finish()
            return
        await self.interrupt()
        await self._listen()

    async def send_text(self, text: str):
        await self.interrupt()
        self._start_turn(self._handle(text.strip(), guess_lang(text), "texto"))

    async def _video_command(self, text: str) -> str | None:
        """«pon en youtube X» / «quita el vídeo» / «pausa el vídeo» / «adelanta 30 segundos»… (play on YouTube / close / pause / seek; via mpv)."""
        t = text.strip()
        player = await asyncio.to_thread(self._current_player)
        if player:
            m = youtube.SEEK_RE.search(t)
            simple = {youtube.CLOSE_RE: ("stop", None, "Removed."), youtube.PAUSE_RE: ("pause", None, "Paused."),
                      youtube.RESUME_RE: ("resume", None, "Resuming."), youtube.BIG_RE: ("fullscreen", 1, "There you go."),
                      youtube.SMALL_RE: ("fullscreen", 0, "Okay.")}
            for rx, (action, value, reply) in simple.items():
                if rx.search(t):
                    await asyncio.to_thread(player.command, action, value)
                    self.spawn(self._video_refresh())
                    return reply
            if m:
                n = {"un": 1, "una": 1, "medio": 0.5}.get((m.group(2) or "").lower()) or float(m.group(2) or 10)
                secs = n * (60 if (m.group(3) or "").lower().startswith("m") else 1)
                back = m.group(1).lower() in ("atrasa", "retrocede", "rebobina")
                await asyncio.to_thread(player.command, "seek", -secs if back else secs)
                return "Done."
        # with their own search; if nothing is found, Claude takes over
        m = WATCH_SITE_RE.match(t) or WATCH_LIVE_RE.match(t)
        if m:
            service = m.groupdict().get("s") or m.group("q")
            query = m.group("q") + ("" if m.re is WATCH_SITE_RE else " directo")
            if video_sites.service_key(service) and any(video_sites.service_key(s["name"]) == video_sites.service_key(service)
                                                        and s.get("enabled", True) for s in settings["video_services"]):
                ok, said = await self.watch_find(service, query)
                if ok:
                    return said
        m = youtube.PLAY_RE.match(t)
        if not m:
            return None
        try:
            results = await asyncio.to_thread(youtube.search, m.group(1))
        except RuntimeError as e:
            return str(e)
        if not results:
            return "I found no videos."
        await self.play_video(results[0]["id"], results[0]["title"])
        await self.broadcast({"type": "video_results", "results": results, "query": m.group(1)})
        return f"Playing «{results[0]['title']}»."

    async def notify(self, text: str, offer: str | None = None, kind: str = "info"):
        """Notice the user asked for (new mail, update news). In quiet mode it only shows in the panel."""
        item = {"text": text, "kind": kind, "time": time.time()}
        self.notices = ([item] + self.notices)[:20]
        await self.broadcast({"type": "notice", **item})
        log.info("Notice (%s): %s", kind, text)
        if settings["quiet_mode"]:
            return
        if self.state != "idle":   # it is speaking or listening: say it as soon as it finishes, so it is not lost
            self.spawn(self._notify_later(text, offer))
            return
        await self._announce(text, offer)

    async def _notify_later(self, text: str, offer: str | None):
        for _ in range(600):
            await asyncio.sleep(1)
            if self.state == "idle" and not self.player.busy():
                await asyncio.sleep(1.5)
                if self.state == "idle":
                    await self._announce(text, offer)
                    return

    async def _announce(self, text: str, offer: str | None):
        async with self._notice_lock:   # several notices in a row: one after another
            if self.state != "idle":       # while waiting for its turn the user spoke (or it is answering the previous one)
                self.spawn(self._notify_later(text, offer))
                return
            self._offer = (time.time(), offer) if offer else None
            self.player.play(audio.chime(self.player.rate), self.player.rate, mutes_mic=False)
            self._speak_lang = settings["notice_language"]
            await self._say(text)
            await self._wait_speech()
            if offer:
                await self._listen_followup(self._gen)

    async def check_mail(self):
        new = await asyncio.to_thread(self.mail.check)
        await self.broadcast({"type": "mail", **self.mail.summary()})
        if len(new) >= 3:
            # many at once (back from suspend, first connection): a single notice instead of a string of them
            whos = list(dict.fromkeys(m["from"].split("<")[0].strip() or m["addr"] for m in new))
            more = f" and {len(whos) - 3} more" if len(whos) > 3 else ""
            self.spawn(self.notify(f"You have {len(new)} new emails: from {', '.join(whos[:3])}{more}.", kind="mail"))
            return
        for m in new:
            who = m["from"].split("<")[0].strip() or m["addr"]
            where = "" if len(self.mail.accounts) < 2 else f" (to {m['account']})"
            # the notice goes on its own: the check finishes now and does not pile up with the next one while it talks
            self.spawn(self.notify(f"New email from {who}{where}: {m['subject']}. Want me to summarize it?",
                                   offer=f"Give me a short summary of email {m['id']}.", kind="mail"))

    async def check_whatsapp(self):
        """WhatsApp (read-only): refresh the panel and announce new messages, except from muted chats (groups, by default)."""
        new = await asyncio.to_thread(self.wa.check)
        await self.broadcast({"type": "whatsapp", **self.wa.summary()})
        media = [m for m in new if m["media"] in ("audio", "image", "sticker")]
        if media:   # WhatsApp deletes audio and photos from its servers after ~2 weeks: save them now
            self.spawn(self._keep_media(media))
        by_chat: dict[str, list[str]] = {}
        for m in new:
            if not m["muted"]:
                by_chat.setdefault(m["chat"], []).append((f"{m['from']}: " if m["group"] else "") + m["text"])
        if len(by_chat) > 2:
            chats = list(by_chat)
            more = f" and {len(chats) - 3} more" if len(chats) > 3 else ""
            self.spawn(self.notify(f"You have new WhatsApp messages from {', '.join(chats[:3])}{more}.", kind="whatsapp"))
            return
        for chat, texts in by_chat.items():
            text = texts[-1] if len(texts[-1]) <= 140 else texts[-1][:137] + "…"
            more = f" (and {len(texts) - 1} more)" if len(texts) > 1 else ""
            self.spawn(self.notify(f"WhatsApp from {chat}{more}: {text}", kind="whatsapp"))

    async def _keep_media(self, media: list[dict]):
        for m in media:
            try:
                await asyncio.to_thread(self.wa.media_file, m["jid"], m["id"])
            except Exception as e:
                log.info("WhatsApp: could not save %s %s: %s", m["media"], m["id"], e)

    async def _mail_action(self, fn, *args):
        """Send or reply (the user does not want confirmations): it is done and reported in the panel."""
        try:
            said = await asyncio.to_thread(fn, *args)
            await self.broadcast({"type": "notice", "text": "✉️ " + said})
            self.spawn(self.check_mail())
        except Exception as e:
            log.warning("Mail: %s", e)
            msg = f"I could not send the email: {e}"
            await self.broadcast({"type": "error", "text": msg})
            await self._say(msg)

    async def auto_update(self):
        """Every X hours: Claude reviews Betis and tasks (in a separate query); the user is notified only if there is news."""
        usage = claude_usage(self.brain.plan)
        if usage and usage["left"] < 25:
            log.info("Automatic update postponed: little Claude plan left (%.0f%%)", usage["left"])
            return False
        news = []
        w = await asyncio.to_thread(weather.forecast)
        today = (w.get("days") or [{}])[0] if w.get("ok") else {}
        if (today.get("rain") or 0) >= 60:
            news.append(f"it may rain today in {w['place']} ({today['rain']}%)")
        try:
            out = await asyncio.to_thread(one_shot, UPDATE_PROMPT)
        except Exception as e:
            # no network or the laptop suspended midway: it is not marked as done, retried in 10 min
            log.warning("Automatic update: %s", e)
            return False
        news += [l[2:].strip() for l in out.splitlines() if l.startswith("- ")][:3]
        log.info("Automatic update: %s", news or "nothing new")
        STATE_FILE.write_text(json.dumps({"last_update": time.time()}), encoding="utf-8")
        if news:
            await self.notify("News: " + "; ".join(news) + ".", kind="update")
        return True

    async def morning_context(self) -> str:
        """What is needed for the morning summary (🌅 button or «resumen matinal»); Claude decides what to report."""
        now = datetime.now()
        parts = []
        if self.mail.configured:
            mails = await asyncio.to_thread(self.mail.recent, 12)
            parts.append("Emails from the last 12 h: " + ("; ".join(
                f"{m['id']} ({m['account']}{', unread' if not m['read'] else ''}) from {m['from']}: «{m['subject']}»"
                for m in mails) or "none"))
        w = await asyncio.to_thread(weather.forecast)
        if w.get("ok"):
            d = (w.get("days") or [{}])[0]
            parts.append(f"Weather today in {w['place']}: now {w['temp']}° and {w['text']}; max {d.get('max')}°, "
                         f"min {d.get('min')}°, rain {d.get('rain', 0)}%")
        data = await asyncio.to_thread(datos.summary)
        today = data["today"]
        parts.append(f"Today is {today['day']} ({today['mode']}). What is left for today: " + ("; ".join(
            f"{i.get('hora', '')} {i.get('que', '')}".strip() for i in today["items"]) or "nothing"))
        parts.append("Pending tasks (all of them; you pick the ones relevant for today or the next few days): " + "; ".join(
            f"{t['tarea']} [{t['prioridad']}{', ' + t['cuando'] if t['cuando'] else ''}{', ' + t['estado'] if t['estado'] else ''}]"
            for t in data["tasks"]))
        if data["betis"]:
            parts.append("Upcoming Betis matches: " + "; ".join(
                f"{m.get('partido', '')} {m['cuando']} {m.get('hora', '')}" for m in data["betis"][:2]))
        hoy = [a for a in avisos.upcoming(now) if a["cuando"].startswith(now.strftime("%Y-%m-%d"))]
        parts.append("Reminders for today: " + ("; ".join(f"{a['cuando'][11:]} {a.get('texto', '')}" for a in hoy) or "none"))
        return ("[Morning summary (system data). Give a short, natural spoken summary (about 8 sentences, no long "
                "lists, do not read out IDs): 1) the emails that matter (ignore ads and automatic notifications); "
                "2) today's weather; 3) use WebSearch to check whether there is a good football match today (LaLiga, "
                "Champions League, Europa League, national team or Betis) with the time and where to watch it; "
                "4) what is on today; 5) only the relevant tasks; 6) today's reminders. Answer in the language the "
                "user spoke or wrote (default English).\n" + "\n".join(parts) + "]")

    def live_context(self) -> str:
        """What is on screen and playing right now (system data), so Claude does not have to ask."""
        parts = []
        sp = self.spotify.summary() if self.spotify.connected else {}
        if sp.get("title"):
            parts.append(f"music: «{sp['title']}» by {sp['artist']} on {sp.get('device') or '?'}"
                         + ("" if sp.get("playing") else " (paused)"))
        else:
            parts.append("music: nothing")
        v = self._video_status
        if v.get("playing"):
            parts.append(f"video: «{v.get('title', '')}»" + (" (paused)" if v.get("paused") else ""))
        w = weather.forecast()   # cached for 15 min
        if w.get("ok"):
            parts.append(f"weather in {w['place']}: {w['temp']}° ({w['text'].lower()}, feels like {w['feels']}°)")
        act = timers.active()
        if act:
            parts.append("timers: " + ", ".join(f"{a['label'] or 'unnamed'} {timers.say_duration(a['end'] - time.time())}"
                                                     for a in act))
        try:
            lay = json.loads(LAYOUT_FILE.read_text(encoding="utf-8"))
            parts.append("panel (visible widgets): " + ", ".join(i["id"] for i in lay if not i.get("hidden")))
        except (OSError, ValueError):
            pass
        if self.mail.configured:
            unread = self.mail.unread[:6]
            parts.append("unread emails: " + ("; ".join(f"[{m['id']}] {m['from']} → {m['account']}: «{m['subject']}»"
                                                           for m in unread) if unread else "none"))
        if self.wa.state == "connected" and self.wa.chats:
            parts.append("WhatsApp (latest chats, read-only): " + "; ".join(
                f"{c['name']}{' (group)' if c['group'] else ''}: {'me' if c['mine'] else (c['from'] or c['name'])} «{c['text'][:80]}»"
                for c in self.wa.chats[:6]))
        parts.append(f"voice {voice_label(settings['voice_es'])} {int(settings['tts_rate']):+d}%, "
                     f"theme {settings['theme']}, gestures {'yes' if self.vision and self.vision.connected else 'no'}")
        return "[Right now (system data): " + " · ".join(parts) + "]"

    def _current_player(self):
        if self.video.playing():
            return self.video
        return self.web if self.web.active() else None

    async def watch(self, url: str) -> str | None:
        """Open a video site in the controlled window. Returns the reason if it cannot."""
        try:
            await asyncio.to_thread(self.video.stop)
            try:
                await asyncio.to_thread(self.spotify.pause)
            except Exception:
                pass
            await asyncio.to_thread(self.web.open, url)
        except RuntimeError as e:
            log.info("Not opening %s: %s", url, e)
            await self.broadcast({"type": "error", "text": str(e)})
            return str(e)
        self.spawn(self._video_refresh(delay=4))
        self.spawn(self._watch_wall(url))
        return None

    async def _watch_wall(self, url: str):
        """If the site asks for an account (RTVE live, Netflix without a session…), tell the user: they sign in themselves."""
        for _ in range(16):
            await asyncio.sleep(2)
            if self.web.wall:
                name = next((s["name"] for s in settings["video_services"]
                             if any(self.web.wall.endswith(d) for d in s["domains"])), "that site")
                msg = f"{name} requires signing in to watch this. Sign in in the window, then ask me again."
                await self.broadcast({"type": "error", "text": msg})
                await self._say(msg)
                return

    async def watch_find(self, service: str, query: str) -> tuple[bool, str]:
        """Search «query» on that service (video_sites) and play the first hit. Returns (ok, sentence to say)."""
        if video_sites.service_key(service) is None:   # any other site: search on the page itself
            url = site_url(service)
            if not url:
                return False, f"I do not know which site {service} is; tell me its address."
            self.spawn(self._search_web(url, service, query))
            return True, f"Searching «{query}» on {service} and playing it."
        try:
            results = await asyncio.to_thread(video_sites.find, service, query)
        except RuntimeError as e:
            return False, str(e)
        if not results:
            return False, f"I did not find «{query}» on {service}."
        best = results[0]
        log.info("Watch %r on %s → %s", query, service, best["url"])
        if best.get("id"):   # YouTube: via mpv, no ads
            await self.play_video(best["id"], best["title"])
        else:
            reason = await self.watch(best["url"])
            if reason:
                return False, reason
        if best["kind"] == "búsqueda":   # Netflix, Prime, Disney+: open the search and play the first result
            self.spawn(self._play_first(video_sites.service_key(service), service, query))
            return True, f"Searching «{query}» on {service} and playing it."
        day = f" from {best['date'][8:10]}/{best['date'][5:7]}" if best.get("date") else ""
        return True, f"Playing «{best['title']}»{day}."

    async def _search_web(self, url: str, name: str, query: str):
        """Site without a recipe: open the home page, search like a person and play the first result that is not an ad."""
        reason = await self.watch(url)
        if reason:
            await self._say(reason)
            return
        found = await asyncio.to_thread(self.web.search_site, url, query)
        if not found and not self.web.wall:   # at the very least, via a search engine («site:web query»)
            found = await asyncio.to_thread(self.web.search_engine, url, query)
        if found:
            log.info("First result for %r on %s → %s", query, name, found)
            await self.watch(found)
        elif not self.web.wall:
            msg = f"I did not find «{query}» on {name}. I'll leave the site open."
            await self.broadcast({"type": "error", "text": msg})
            await self._say(msg)

    async def _play_first(self, key: str, service: str, query: str):
        url = await asyncio.to_thread(self.web.pick_result, key)
        if url:
            log.info("First result for %r on %s → %s", query, service, url)
            await self.watch(url)
        elif not self.web.wall:
            msg = f"I did not find «{query}» on {service}. I'll leave the search open."
            await self.broadcast({"type": "error", "text": msg})
            await self._say(msg)

    async def play_video(self, video_id: str, title: str, rect: dict | None = None):
        try:   # so the music and the video do not overlap
            await asyncio.to_thread(self.spotify.pause)
        except Exception:
            pass
        await asyncio.to_thread(self.web.close)
        await asyncio.to_thread(self.video.play, video_id, title, rect)
        self.spawn(self._video_refresh(delay=3))

    async def _video_refresh(self, delay: float = 0.3):
        await asyncio.sleep(delay)
        # in a thread: checking for a video site is an HTTP request that, with no video, takes ~1 s to fail (Windows)
        player = await asyncio.to_thread(self._current_player)
        st = await asyncio.to_thread(player.status) if player else {"playing": False}
        if st != self._video_status:
            self._video_status = st
            await self.broadcast({"type": "video", **st})

    async def _on_barge(self, text: str, just_stop: bool):
        """The user spoke while it was talking: everything is cut (the task too) and it listens."""
        if self.state != "speaking" and not self.player.busy():
            return
        log.info("Interrupted by voice: %r", text)
        await self.interrupt()
        if just_stop:
            self.player.play(audio.chime(self.player.rate, up=False), self.player.rate, mutes_mic=False)
        elif text:
            rest = find_wake(text, int(settings["wake_sensitivity"]))
            self.player.play(audio.chime(self.player.rate), self.player.rate, mutes_mic=False)
            self._start_turn(self._handle(rest if rest else text, "es", "voz"))
        else:
            await self._listen()

    async def _on_wake(self, rest: str, lang: str):
        await self.interrupt()
        if rest:
            self.player.play(audio.chime(self.player.rate), self.player.rate, mutes_mic=False)
            self._start_turn(self._handle(rest, lang, "voz"))
        else:
            await self._listen()

    async def _listen(self):
        if not self.listener:
            self._enroll_left = 0
            await self.broadcast({"type": "error", "text": "Speech recognition is still loading "
                                  "(the first time it downloads). Meanwhile, type to me."})
            return
        if self.mic_mute.muted:
            await self.broadcast({"type": "error", "text": "The microphone is muted in Windows. Unmute it from Status."})
        self._followup = False
        self.player.play(audio.chime(self.player.rate), self.player.rate, mutes_mic=False)
        self.listener.activate()
        await self.set_state("listening")

    async def _listen_followup(self, gen: int):
        """After replying, keep the mic open for a few seconds to answer without saying «Cortex»."""
        if not self.listener or gen != self._gen or self.state != "idle" or self._enroll_left or \
                not settings["followup_seconds"]:
            return
        self._followup = True
        # the mic stays muted for a moment after speaking (echo): the time counts from when it opens
        self.listener.activate(timeout=float(settings["followup_seconds"]) + 0.5)
        await self.set_state("listening")

    async def _on_command(self, clip: np.ndarray, spec=None):
        if self._enroll_left:
            await self._enroll_sample(clip)
            return
        followup, self._followup = self._followup, False
        self._start_turn(self._voice_turn(clip, followup, spec))

    async def _on_timeout(self):
        if self._enroll_left:
            self._enroll_left = 0
            await self.broadcast({"type": "enroll", "done": True, "count": self.listener.print.user_samples,
                                  "message": "I did not hear you; recording stopped."})
        if self.state == "listening":
            await self.set_state("idle")

    # ---------- «Cortex» samples ----------
    async def enroll(self, total: int = 6):
        await self.interrupt()
        self._enroll_left = total
        self._enroll_total = total
        await self._enroll_next()

    async def _enroll_next(self):
        done = self._enroll_total - self._enroll_left
        await self.broadcast({"type": "enroll", "step": done + 1, "total": self._enroll_total})
        await asyncio.sleep(0.4)
        await self._listen()

    async def _enroll_sample(self, clip: np.ndarray):
        count = await asyncio.to_thread(self.listener.print.add_sample, clip)
        self._enroll_left -= 1
        if self._enroll_left:
            await self._enroll_next()
        else:
            await self.set_state("idle")
            await self.broadcast({"type": "enroll", "done": True, "count": count,
                                  "message": "Done! Now retraining the detector with your voice (a few minutes)."})
            self.spawn(self.retrain_wake())
        await self.broadcast(self.status())

    async def interrupt(self):
        self._gen += 1
        self._enroll_left = 0
        self.brain.cancel()
        self.player.stop()
        if self.listener:
            self.listener.deactivate()
        while not self._sentences.empty():
            self._sentences.get_nowait()
            self._sentences.task_done()
        if self._turn and not self._turn.done():
            self._turn.cancel()
            try:
                await self._turn
            except (asyncio.CancelledError, Exception):
                pass
        await self.set_state("idle")

    def _start_turn(self, coro):
        self._gen += 1
        self._no_duck = False
        self._spoke = False
        self._pending = ""
        self._raw = self._shown = ""
        self._turn_t = time.monotonic()   # to measure how long it takes to start speaking
        self._turn = self.spawn(self._guarded(coro))

    async def _guarded(self, coro):
        """An unexpected failure in a turn does not leave the panel stuck on «thinking»."""
        try:
            await coro
        except Exception:
            log.exception("Error in the turn")
            await self.broadcast({"type": "error", "text": "Something went wrong; it is in the log."})
            await self.set_state("idle")

    # ---------- turns ----------
    async def _voice_turn(self, clip: np.ndarray, followup: bool = False, spec=None):
        await self.set_state("transcribing")
        t = time.monotonic()
        text = None
        if spec is not None:   # transcription already started during the pause (see Listener)
            try:
                text, lang = await asyncio.wrap_future(spec)
                self.spawn(asyncio.to_thread(save_clip, normalize(clip), f"{lang}: {text}"))
            except Exception:
                log.exception("The early transcription failed; redoing it")
                text = None
        if text is None:
            text, lang = await asyncio.to_thread(self.stt.transcribe_command, clip, self._speak_lang)
        log.info("Transcribed in %.2fs%s (%s): %s", time.monotonic() - t, " (early)" if spec else "", lang, text)
        text = strip_wake(text)
        last = next((h["text"] for h in reversed(self.history) if h["role"] == "assistant"), "")
        asked = last.rstrip().endswith("?")
        if not text or (followup and not asked and is_ack(text)):
            # «ah vale», «okey»… after a reply: no need to answer
            # (unless Cortex had just asked something: then «sí» or «vale» is the answer)
            await self.set_state("idle")
            return
        await self._handle(text, lang, "voz")

    async def _handle(self, text: str, lang: str, channel: str):
        self._speak_lang = lang
        await self._add("user", text, lang=lang)
        if self._offer and time.time() - self._offer[0] < 90 and YES_RE.match(text.strip()):
            text = self._offer[1]
        self._offer = None
        tool = await asyncio.to_thread(local_tools, text) if lang == "es" else None
        if not tool and lang == "es":
            tool = await self._video_command(text)
        if tool:
            await self.broadcast({"type": "timers", "timers": timers.active()})
            await self.broadcast({"type": "compra", "items": compra.items()})
            await self._add("assistant", tool, local=True)
            await self._say(tool)
            await self._wait_speech()
            return
        watch = WATCH_RE.search(text) and not re.search(r"m[uú]sica|canci[oó]n|playlist|disco|[aá]lbum", text, re.I)
        intent = None if watch else music.parse(text)
        if intent:
            await self._music(intent, lang)
            return
        local = route(text, lang) or datos.route(text)
        if local:
            await self._add("assistant", local, local=True)
            await self._say(local)
            await self._wait_speech()
            await self._listen_followup(self._gen)
            return

        await self.set_state("thinking")
        await self.broadcast({"type": "assistant_start"})
        on_delta = lambda chunk: self.loop.call_soon_threadsafe(self._on_delta, chunk)
        on_tool = lambda name: self.loop.call_soon_threadsafe(self._on_tool, name)
        ask = f"{text}\n\n{await asyncio.to_thread(self.live_context)}"
        for mid in list(dict.fromkeys(MAIL_ID_RE.findall(text) if self.mail.configured else []))[:2]:
            try:
                m = await asyncio.to_thread(self.mail.read, mid)
                ask += (f"\n\n[Email {mid} (account {m['account']}) · From: {m['from']} · Subject: {m['subject']} · "
                        f"{m['date']}]\n{m['text']}\n[End of email {mid}]")
            except Exception as e:
                log.warning("Could not read email %s: %s", mid, e)
        if BANK_RE.search(text):
            try:
                extra = await asyncio.to_thread(bank_context, text)
            except Exception:
                log.exception("Could not read the bank data")
                extra = ""
            if extra:
                ask = f"{text}\n\n{extra}"
        if WATCH_RE.search(text):
            ask = f"{ask}\n\n{watch_context()}"
        if GESTURE_RE.search(text):
            ask = f"{ask}\n\n{gesture_context(self.vision)}"
        if MORNING_RE.search(text):
            ask = f"{ask}\n\n{await self.morning_context()}"
        try:
            reply = await asyncio.to_thread(self.brain.ask, ask, lang, channel, on_delta, on_tool)
        except BrainError as e:
            await self.broadcast(self.status())
            if e.kind == "cancelled":
                return
            await self.broadcast({"type": "assistant_end", "text": ""})
            await self._fail(e.message)
            return
        rest = self._pending.strip()
        self._pending = ""
        if rest:
            await self._say(rest)
        commands = [c.strip() for c in MUSIC_TAG.findall(reply)]
        for target in WATCH_TAG.findall(reply):
            if re.match(r"https?://", target):
                await self.watch(target)
            elif "|" in target:
                ok, said = await self.watch_find(*[p.strip() for p in target.split("|", 1)])
                if not ok:
                    await self._say(said)
        for raw in SAVE_WEB_TAG.findall(reply):
            name, _, url = (x.strip() for x in raw.partition("|"))
            said = await asyncio.to_thread(save_web, name, url or None, self.web)
            await self.broadcast({"type": "notice", "text": "🎬 " + said})
        for name in VOICE_TAG.findall(reply):
            set_voice(name.strip())
        for raw in MAIL_SEND_TAG.findall(reply):
            p = [x.strip() for x in raw.split("|")]
            if len(p) >= 3:
                # to | subject | text | account (optional: primary or secondary)
                self.spawn(self._mail_action(self.mail.send, p[0], p[1], p[2], p[3] if len(p) > 3 else None))
        for mid, body in MAIL_REPLY_TAG.findall(reply):
            self.spawn(self._mail_action(self.mail.reply, mid, body.strip()))
        for rate in RATE_TAG.findall(reply):
            set_rate(int(rate))
        await self._apply_aviso_tags(reply)
        reply = ANY_TAG.sub("", reply).strip()
        self.history = (self.history + [{"role": "assistant", "text": reply, "time": time.time()}])[-100:]
        await self.broadcast({"type": "assistant_end", "text": reply})
        await self.broadcast(self.status())
        for command in commands:
            intent = music.parse(command)
            log.info("Claude requests music: %r → %s", command, intent)
            if intent:
                await self._music(intent, lang, announce=False)
        await self._wait_speech()
        await self._listen_followup(self._gen)

    async def _music(self, intent: dict, lang: str, announce: bool = True):
        en = lang == "en"
        kind, value = intent["intent"], intent.get("value")
        if kind == "query" and norm(value) in LIKED:
            kind = "playlist"
        self._no_duck = True
        async with self._duck_lock:
            try:
                await asyncio.to_thread(self.spotify.unduck)
            except SpotifyError:
                pass
        try:
            if kind == "playlist":
                try:
                    name = await asyncio.to_thread(self.spotify.play_playlist, value)
                except SpotifyError as e:
                    if not str(e).startswith("I cannot find any playlist"):
                        raise
                    name = await asyncio.to_thread(self.spotify.play_query, value)   # not a playlist: a song
                reply = f"Playing {name}." if en else f"Pongo {name}."
            elif kind in ("artist", "query"):
                name = await asyncio.to_thread(self.spotify.play_query, value, "artist" if kind == "artist" else "any")
                reply = f"Playing {name}." if en else f"Pongo {name}."
            elif kind == "device":
                name = await asyncio.to_thread(self.spotify.transfer, value)
                reply = f"Now on {name}." if en else f"Ahora suena en {name}."
            elif kind == "now":
                name = await asyncio.to_thread(self.spotify.now_playing)
                if name:
                    reply = f"This is {name}." if en else f"Suena {name}."
                else:
                    reply = "Nothing's playing." if en else "No suena nada."
            elif kind == "list":
                names = [p["name"] for p in await asyncio.to_thread(self.spotify.playlists)][:8]
                if not names:
                    reply = "You have no playlists." if en else "No tienes playlists."
                else:
                    joined = names[0] if len(names) == 1 else                         ", ".join(names[:-1]) + (" and " if en else " y ") + names[-1]
                    reply = f"You have {joined}." if en else f"Tienes {joined}."
            else:
                action = {"pause": self.spotify.pause, "resume": self.spotify.resume, "next": self.spotify.next,
                          "previous": self.spotify.previous}.get(kind)
                if action:
                    await asyncio.to_thread(action)
                elif kind == "volume_up":
                    await asyncio.to_thread(self.spotify.change_volume, 8)
                elif kind == "volume_down":
                    await asyncio.to_thread(self.spotify.change_volume, -8)
                elif kind == "volume_set":
                    await asyncio.to_thread(self.spotify.set_volume, value)
                reply = ""
        except SpotifyError as e:
            await self._fail(str(e))
            return
        if kind in ("playlist", "artist", "query", "device"):
            # playing music: no spoken reply, the music is the answer (it is still written in the chat)
            await self._add("assistant", reply, local=True)
        elif not announce:
            self.player.play(audio.chime(self.player.rate, up=False), self.player.rate, mutes_mic=False)
        elif reply:
            await self._add("assistant", reply, local=True)
            await self._say(reply)
        else:
            self.player.play(audio.chime(self.player.rate, up=False), self.player.rate, mutes_mic=False)
            await self._add("assistant", "Done." if en else "Hecho.", local=True)
        await self._wait_speech()
        self.spawn(self._spotify_refresh(delay=1.0))

    async def _spotify_refresh(self, delay: float = 0.0):
        if delay:
            await asyncio.sleep(delay)
        if self.spotify.connected:
            try:
                await asyncio.to_thread(self.spotify.state)
            except SpotifyError:
                pass
            except Exception:
                log.exception("Error reading Spotify")
        await self.broadcast({"type": "spotify", **self.spotify.summary()})

    def _on_delta(self, chunk: str):
        """Accumulate text and send each complete sentence to be spoken as soon as it arrives."""
        self._raw += chunk
        visible = ANY_TAG.sub("", self._raw)
        cut = visible.find("[[")   # tag only partly arrived: held back until it closes
        if cut >= 0:
            visible = visible[:cut]
        elif visible.endswith("["):
            visible = visible[:-1]
        chunk = visible[len(self._shown):]
        self._shown = visible
        if not chunk:
            return
        self.spawn(self.broadcast({"type": "assistant_delta", "text": chunk}))
        self._pending += chunk
        while True:
            m = SENTENCE_END.match(self._pending)
            if not m or len(m.group(1).strip()) < 2:
                break
            self._pending = self._pending[m.end():]
            self._queue_sentence(m.group(1).strip())

    def _on_tool(self, name: str):
        if name in SEARCH_TOOLS and not self._spoke:
            lang = "en" if self._speak_lang == "en" else "es"
            self._queue_sentence(random.choice(FILLERS[lang]))
        if name in SEARCH_TOOLS:
            self.spawn(self.broadcast({"type": "activity", "text": "Searching the web…"}))

    def _queue_sentence(self, text: str):
        self._spoke = True
        self._sentences.put_nowait((self._gen, text))

    async def _say(self, text: str):
        self._queue_sentence(text)

    async def _wait_speech(self):
        await self._sentences.join()
        while self.player.busy():
            await asyncio.sleep(0.05)
        await self.set_state("idle")

    async def _fail(self, message: str):
        log.warning(message)
        await self.broadcast({"type": "error", "text": message})
        self._speak_lang = settings["notice_language"]
        await self._say(message)
        await self._wait_speech()

    async def _add(self, role: str, text: str, **extra):
        item = {"role": role, "text": text, "time": time.time(), **extra}
        self.history = (self.history + [item])[-100:]
        await self.broadcast({"type": role, **item})

    # ---------- voz ----------
    async def _speaker(self):
        while True:
            gen, sentence = await self._sentences.get()
            try:
                clean = tts.clean_for_speech(sentence)
                if clean and gen == self._gen and gen != self._quiet_gen:
                    voice = tts.synthesize(clean, self._speak_lang)
                    try:
                        async for pcm, rate in voice:
                            if gen != self._gen or gen == self._quiet_gen:
                                break
                            await self.set_state("speaking")
                            if self._turn_t and gen == self._gen and time.monotonic() - self._turn_t < 60:
                                log.info("Starts speaking after %.2f s", time.monotonic() - self._turn_t)
                                self._turn_t = 0.0
                            if self.barge:
                                self.barge.said(clean)
                            self.player.play(pcm, rate)
                    finally:
                        await voice.aclose()   # cut off midway: release the voice connection now
            except Exception:
                log.exception("Error synthesizing speech")
            finally:
                self._sentences.task_done()

    async def _monitor(self):
        """Mute the mic while Cortex is audible (with a tail for Bluetooth echo)."""
        while True:
            try:
                await self._monitor_loop()
            except Exception:
                # if this loop died, the mic would stay muted forever
                log.exception("Error in the monitor loop; resuming")
                await asyncio.sleep(1)

    async def _monitor_loop(self):
        last_status = last_maintain = time.monotonic()
        last_estado = last_aviso = last_vision = 0.0
        last_bank = -1e9
        last_timer = last_video = last_web_check = 0.0
        last_mail = last_wa = -1e9
        last_update_try = time.monotonic() - 540   # the first one, a minute after startup
        while True:
            now = time.monotonic()
            if self.player.busy():
                self._muted_until = now + 0.4
            self.mic.muted = now < self._muted_until
            if self.barge:
                self.barge.enabled = self.mic.muted and self.state == "speaking"
            if self.state == "listening":
                await self.broadcast({"type": "level", "level": self.mic.level})
            if now - last_status > 5:
                last_status = now
                if self.clients:
                    self.spawn(self._spotify_refresh())
                await self.broadcast(self.status())
            if now - last_maintain > 60 and self.state == "idle":
                last_maintain = now
                self.spawn(asyncio.to_thread(self.brain.maintain))
            if now - last_estado > 30:
                last_estado = now
                self.spawn(self._refresh_data())
            if now - last_vision > (0.4 if self.vision and self.vision.preview_clients else 1.5):
                last_vision = now
                self.spawn(self._send_vision(force=bool(self.vision and self.vision.preview_clients)))
            outs = self.mic_mute.outputs
            if outs is not None and outs != self._outputs and self.state == "idle" and not self.player.busy():
                if self._outputs is not None:
                    # a speaker was connected or disconnected (Bluetooth): reopen so it does not talk into the void
                    log.info("Speakers changed (%s); reopening audio", outs)
                    await asyncio.to_thread(self.open_devices)
                self._outputs = outs
            if now - last_video >= 2 and (self.video.playing() or self._video_status.get("playing")
                                          or now - last_web_check >= 10):
                if now - last_web_check >= 10:
                    last_web_check = now
                last_video = now
                self.spawn(self._video_refresh(delay=0))
            if self.wa.available and now - last_wa >= (15 if self.wa.state == "connected" else 4):
                last_wa = now
                self.spawn(self.check_whatsapp())
            if self.mail.configured and now - last_mail >= 60:
                last_mail = now
                self.spawn(self.check_mail())
            hours = float(settings["auto_update_hours"] or 0)
            if hours and now - last_update_try >= 600 and self.state == "idle":
                last_update_try = now
                try:
                    done_at = json.loads(STATE_FILE.read_text(encoding="utf-8"))["last_update"]
                except (OSError, ValueError, KeyError):
                    done_at = 0
                if time.time() - done_at >= hours * 3600:
                    self.spawn(self.auto_update())
            if now - last_timer >= 1:
                last_timer = now
                for t in timers.due():
                    self.spawn(self._timer_done(t))
            if now - last_aviso > 15:
                last_aviso = now
                self.spawn(self._check_avisos())
            if now - last_bank > 1800:
                # the balance refreshes itself every 6 h (decided by the bank.summary cache: ~4 queries a day)
                last_bank = now
                self.spawn(asyncio.to_thread(bank.summary))
                self._maybe_nightly_retrain()
            t = time.monotonic()
            await asyncio.sleep(0.08)
            lag = time.monotonic() - t - 0.08
            if lag > 120:   # the laptop was suspended, it is not a hang; the network takes a while to come back
                log.info("Back from a %.0f min suspend", lag / 60)
                last_update_try = time.monotonic()
                last_mail = time.monotonic() - 40   # mail, after 20 s
            elif lag > 0.5:   # something blocked the main loop (a slow call without to_thread): leave a trace
                log.warning("The main loop was stalled for %.1f s", lag)

    # ---------- gestures (camera) ----------
    async def _on_gesture(self, gesture: str, action: str):
        log.info("Gesture %s → %s", gesture, action)
        await self.broadcast({"type": "gesture", "gesture": gesture, "action": action})
        sp = self.spotify
        try:
            player = await asyncio.to_thread(self._current_player) \
                if action in ("next", "previous", "toggle", "volume_up", "volume_down") else None
            if player:   # while a video is playing, gestures control the video
                cmd = {"next": ("seek", 10), "previous": ("seek", -10), "toggle": ("toggle", None),
                       "volume_up": ("volume", 5), "volume_down": ("volume", -5)}[action]
                await asyncio.to_thread(player.command, *cmd)
                self.spawn(self._video_refresh())
                return
            if action in ("next", "previous"):
                await asyncio.to_thread(getattr(sp, action))
            elif action == "toggle":
                await asyncio.to_thread(sp.pause if sp.last_state.get("is_playing") else sp.resume)
            elif action in ("volume_up", "volume_down"):
                await asyncio.to_thread(sp.change_volume, 5 if action == "volume_up" else -5)   # small steps: gestures repeat
            elif action == "listen" and self.state == "idle":
                await self._listen()
            elif action == "stop":
                await self.interrupt()
            elif action == "quiet":
                # goes quiet but does not cut what it is doing (playing music, searching…)
                self._quiet_gen = self._gen
                self.player.stop()
            elif action == "wake_toggle":
                settings.update({"wake_enabled": not settings["wake_enabled"]})
                await self.broadcast(self.status())
            elif action == "new_chat":
                await self.interrupt()
                await asyncio.to_thread(self.brain.new_conversation)
                self.history = []
                await self.broadcast({"type": "cleared"})
            elif action == "theme":
                settings.update({"theme": "light" if settings["theme"] == "dark" else "dark"})
                await self.broadcast({"type": "settings", "settings": settings.all()})
            else:
                return
        except SpotifyError as e:
            await self.broadcast({"type": "error", "text": str(e)})
            return
        except Exception:
            log.exception("Error handling gesture %s", gesture)
            return
        if action in ("next", "previous", "toggle", "volume_up", "volume_down"):
            self.spawn(self._spotify_refresh(delay=0.5))

    async def _on_presence(self, engaged: bool):
        await self.broadcast({"type": "presence", "engaged": engaged})
        await self._send_vision(force=True)

    async def _send_vision(self, force: bool = False):
        if not self.vision or not self.clients:
            return
        state = self.vision.state()
        light = {k: v for k, v in state.items() if k not in ("fps", "yaw", "pitch", "score")}
        if force or light != self._vision_sent:
            self._vision_sent = light
            await self.broadcast({"type": "vision", **state})

    # ---------- retrain «Cortex» ----------
    def _maybe_nightly_retrain(self):
        t = datetime.now()
        if t.hour != 4 or self._retrained_day == t.date() or self.state != "idle":
            return
        self._retrained_day = t.date()
        model = MODELS_DIR / "cortex_wake.npz"
        real = list((MODELS_DIR / "wake_train_cortex").glob("*_real/*.wav"))
        if model.exists() and real and max(f.stat().st_mtime for f in real) > model.stat().st_mtime:
            self.spawn(self.retrain_wake())

    async def retrain_wake(self):
        """Retrain the detector with the latest data (including real activations) in the background, at low priority."""
        if self._retraining:
            return
        self._retraining = True
        await self.broadcast({"type": "wake_train", "running": True})
        log.info("Retraining the «Cortex» detector")
        try:
            proc = await asyncio.create_subprocess_exec(
                str(ROOT / ".venv" / "Scripts" / "python.exe"), str(ROOT / "tools" / "train_wake.py"),
                cwd=str(ROOT), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                creationflags=0x00004000 | 0x08000000)   # BELOW_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW
            out, _ = await proc.communicate()
            (ROOT / "logs" / "train_wake.log").write_bytes(out)
            ok = proc.returncode == 0
            if ok and self.listener:
                self.listener.reload_detector()
            summary = [l for l in out.decode("utf-8", "replace").splitlines() if l.startswith("umbral 0.85")]
            log.info("Retrained (%s): %s", "ok" if ok else "failed", summary)
            await self.broadcast({"type": "wake_train", "running": False, "ok": ok,
                                  "summary": summary[0] if summary else ""})
        finally:
            self._retraining = False

    # ---------- reminders ----------
    async def _apply_aviso_tags(self, reply: str):
        changed = False
        for body in avisos.TAG.findall(reply):
            changed |= await asyncio.to_thread(avisos.from_tag, body) is not None
        for aviso_id in avisos.DELETE_TAG.findall(reply):
            changed |= await asyncio.to_thread(avisos.delete, aviso_id.strip())
        if changed:
            await self.broadcast_avisos()

    async def broadcast_avisos(self):
        items = await asyncio.to_thread(avisos.upcoming, datetime.now())
        await self.broadcast({"type": "avisos", "items": items})
        self.spawn(self._refresh_data())

    async def _timer_done(self, t: dict):
        label = t.get("label") or ""
        log.info("Timer finished: %s", label or timers.say_duration(t["seconds"]))
        await self.broadcast({"type": "timer_done", "timer": t})
        await self.broadcast({"type": "timers", "timers": timers.active()})
        for _ in range(3):   # so it is heard even with music playing
            self.player.play(audio.chime(self.player.rate), self.player.rate, mutes_mic=False)
            await asyncio.sleep(0.5)
        self._speak_lang = settings["notice_language"]
        await self._say(f"Time's up! {label} is ready." if label else "Time's up! The timer has finished.")

    async def _check_avisos(self):
        try:
            fired = await asyncio.to_thread(avisos.due, datetime.now())
        except Exception:
            log.exception("Error checking reminders")
            return
        for a in fired:
            # wait for Cortex to finish what it is doing (max 2 min) so as not to talk over it
            for _ in range(240):
                if self.state == "idle":
                    break
                await asyncio.sleep(0.5)
            log.info("Reminder: %s", a.get("texto"))
            await self.broadcast({"type": "aviso", "texto": a.get("texto", "")})
            await self._add("assistant", f"Reminder: {a.get('texto', '')}", local=True)
            self.player.play(audio.chime(self.player.rate), self.player.rate, mutes_mic=False)
            self._speak_lang = settings["notice_language"]
            await self._say(f"Hey, a reminder: {a.get('texto', '')}.")
            await self._wait_speech()
        if fired:
            await self.broadcast_avisos()

    async def _refresh_data(self):
        """Regenerate estado.md if anything changed and notify the widgets."""
        try:
            changed = await asyncio.to_thread(self.estado.refresh)
            if changed and self.clients:
                await self.broadcast({"type": "data", "data": await asyncio.to_thread(datos.summary)})
        except Exception:
            log.exception("Error updating estado.md")


def keep_awake():
    """Keep the laptop from sleeping or turning off the screen while Cortex is alive (undone on exit)."""
    import ctypes
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x00000001, 0x00000002
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
    except Exception:
        log.warning("Could not prevent the laptop from sleeping")


assistant = Assistant()


def _quiet_resets(loop, context):
    """Windows prints a traceback every time the browser drops a connection (WinError 10054): it is normal."""
    if isinstance(context.get("exception"), ConnectionResetError) and "connection_lost" in str(context.get("handle", "")):
        return
    loop.default_exception_handler(context)


@asynccontextmanager
async def lifespan(_app):
    await assistant.start()
    yield
    assistant.brain.stop()
    assistant.wa.stop()
    if assistant.vision:
        assistant.vision.stop()
    assistant.player.close()
    assistant.mic.close()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.middleware("http")
async def same_origin_only(request, call_next):
    """Commands (POST…) only from the panel: the browser sends «Origin» on cross-site requests,
    and without this any website could blindly trigger retrains, updates…"""
    origin = request.headers.get("origin")
    local = not origin or urlparse(origin).hostname in ("127.0.0.1", "localhost")
    if request.method not in ("GET", "HEAD", "OPTIONS") and not local:
        log.warning("Request %s %s rejected from %s", request.method, request.url.path, origin)
        return Response(status_code=403)
    return await call_next(request)


@app.middleware("http")
async def no_cache(request, call_next):
    """So that Edge does not keep an old version of the panel."""
    response = await call_next(request)
    if not request.url.path.startswith("/api"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/settings")
async def get_settings():
    return {
        "settings": settings.all(),
        "devices": audio.list_devices(),
        "options": {"voices": EDGE_VOICES, "stt": STT_MODELS, "claude": CLAUDE_MODELS},
    }


@app.post("/api/settings")
async def save_settings(changes: dict):
    old = settings.all()
    settings.update(changes)
    changed = {k for k, v in settings.all().items() if old.get(k) != v}
    if changed & {"mic_device", "speaker_device"}:
        await asyncio.to_thread(assistant.open_devices)
    if "stt_model" in changed:
        await asyncio.to_thread(assistant.stt.load, settings["stt_model"])
    if "data_dir" in changed:
        await asyncio.to_thread(assistant.brain.start)
        await assistant._refresh_data()
    if "claude_model" in changed and assistant.state == "idle":
        await asyncio.to_thread(assistant.brain.maintain)
    await assistant.broadcast(assistant.status())
    await assistant.broadcast({"type": "settings", "settings": settings.all()})
    return await get_settings()


@app.get("/api/data")
async def get_data():
    return await asyncio.to_thread(datos.summary)


@app.get("/api/search")
async def do_search(q: str = ""):
    try:
        return {"ok": True, "results": await asyncio.to_thread(search.search, q)}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/youtube")
async def youtube_search(q: str = ""):
    try:
        return {"ok": True, "results": await asyncio.to_thread(youtube.search, q)}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}


@app.get("/favicon.ico")
async def favicon():
    """The same icon as the tray one (created by tools/tray.py)."""
    ico = ROOT / "logs" / "cortex.ico"
    return FileResponse(ico, media_type="image/x-icon") if ico.exists() else Response(status_code=204)


@app.get("/api/mail")
async def get_mail():
    return assistant.mail.summary()


@app.get("/api/whatsapp")
async def get_whatsapp():
    return assistant.wa.summary()


@app.get("/api/whatsapp/chat")
async def whatsapp_chat(jid: str):
    return await asyncio.to_thread(assistant.wa.messages, jid)


@app.get("/api/whatsapp/media")
async def whatsapp_media(jid: str, id: str):
    try:
        path = await asyncio.to_thread(assistant.wa.media_file, jid, id)
    except Exception as e:
        gone = any(k in str(e) for k in ("403", "404", "410", "incomplete media"))
        log.info("WhatsApp: could not download %s: %s", id, e)
        return Response("WhatsApp no longer has it" if gone else "Could not download", status_code=410 if gone else 502)
    kind = {".ogg": "audio/ogg", ".opus": "audio/ogg", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".mp4": "video/mp4"}.get(path.suffix.lower())
    return FileResponse(path, media_type=kind, headers={"Cache-Control": "private, max-age=86400"})


@app.post("/api/whatsapp/mote")
async def whatsapp_mote(body: dict):
    """A contact's nickname (empty = remove it): Cortex uses it in notices, panel and replies."""
    await asyncio.to_thread(assistant.wa.set_mote, str(body.get("name", "")), str(body.get("mote", "")))
    await assistant.check_whatsapp()
    return {"ok": True}


@app.post("/api/whatsapp/send")
async def whatsapp_send(body: dict):
    """Send from the chat open in the panel, if the user chose «read and write». Claude does not have this tool."""
    error = await asyncio.to_thread(assistant.wa.send, str(body.get("jid", "")), str(body.get("text", "")))
    return {"ok": not error, "error": error}


@app.post("/api/whatsapp/check")
async def whatsapp_check():
    """«Reload» button in the panel: check for new messages now."""
    await assistant.check_whatsapp()
    return assistant.wa.summary()


@app.post("/api/whatsapp/mute")
async def whatsapp_mute(body: dict):
    """Mute or unmute a chat (whether it notifies). Groups come muted."""
    await asyncio.to_thread(assistant.wa.set_muted, str(body.get("jid", "")), str(body.get("name", "")), bool(body.get("muted")))
    await assistant.broadcast({"type": "whatsapp", **assistant.wa.summary()})
    return {"ok": True}


@app.get("/api/whatsapp/qr.svg")
async def whatsapp_qr():
    """The QR to link WhatsApp (Settings → Linked devices), as an image for the panel."""
    import io
    import qrcode
    import qrcode.image.svg
    code = assistant.wa.qr
    if not code:
        return Response(status_code=404)
    buf = io.BytesIO()
    qrcode.make(code, image_factory=qrcode.image.svg.SvgPathFillImage, box_size=10, border=2).save(buf)
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


MAIL_ID = re.compile(r"^[a-z]\d+$")
MAIL_CSP = ("default-src 'none'; script-src 'none'; object-src 'none'; img-src * data: blob:; media-src * data:; "
            "style-src 'unsafe-inline' *; font-src * data:; frame-src 'none'; form-action 'none'")


@app.get("/api/mail/open")
async def mail_open(id: str):
    if not MAIL_ID.match(id):
        return Response(status_code=400)
    try:
        return await asyncio.to_thread(assistant.mail.open, id)
    except Exception as e:
        log.info("Mail: could not open %s: %s", id, e)
        return Response(f"Could not open the email: {e}", status_code=502)


@app.get("/api/mail/view")
async def mail_view(id: str):
    """The email body for the panel iframe: no scripts or forms (CSP + sandbox)."""
    if not MAIL_ID.match(id):
        return Response(status_code=400)
    try:
        page = await asyncio.to_thread(assistant.mail.html, id)
    except Exception as e:
        page = f"<p style='font:15px system-ui;color:#b91c1c'>Could not open the email: {e}</p>"
    return HTMLResponse(page, headers={"Content-Security-Policy": MAIL_CSP, "Cache-Control": "no-store"})


@app.get("/api/mail/part")
async def mail_part(id: str, n: int, dl: int = 0):
    if not MAIL_ID.match(id):
        return Response(status_code=400)
    try:
        data, kind, name = await asyncio.to_thread(assistant.mail.part, id, n)
    except Exception:
        return Response(status_code=404)
    from urllib.parse import quote
    how = "attachment" if dl else "inline"
    # anything the browser would execute (html, svg…) is always downloaded, never opened here
    if not dl and not kind.startswith(("image/", "audio/", "video/")) and kind != "application/pdf" or kind == "image/svg+xml":
        how = "attachment"
    return Response(data, media_type=kind, headers={"Content-Disposition": f"{how}; filename*=UTF-8''{quote(name)}",
                                                   "X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"})


@app.post("/api/mail/send")
async def send_mail(body: dict):
    """Send (to, subject, text, account) or reply (reply_to = ID, text) from the panel."""
    try:
        if body.get("reply_to"):
            said = await asyncio.to_thread(assistant.mail.reply, str(body["reply_to"]), str(body.get("text", "")))
        else:
            said = await asyncio.to_thread(assistant.mail.send, str(body["to"]), str(body.get("subject", "")),
                                           str(body.get("text", "")), body.get("account"))
    except Exception as e:
        return {"ok": False, "error": str(e)}
    assistant.spawn(assistant.check_mail())
    return {"ok": True, "message": said}


@app.post("/api/mail/login")
async def mail_login(body: dict):
    """Connect an account from the panel: the user pastes their app password and it is tested immediately."""
    err = await asyncio.to_thread(assistant.mail.connect, str(body.get("slot", "")), str(body.get("user", "")),
                                  str(body.get("password", "")))
    if err:
        return {"ok": False, "error": err}
    await assistant.check_mail()
    return {"ok": True, **assistant.mail.summary()}


@app.post("/api/mail/apppasswords")
async def mail_apppasswords(body: dict):
    """Disabled in Cortex: it does not open sign-in pages. The app password is created by hand."""
    return {"ok": False, "error": "Create the app password by hand in your Google account."}


@app.post("/api/mail/check")
async def mail_check():
    await assistant.check_mail()
    return assistant.mail.summary()


@app.get("/api/notices")
async def get_notices():
    return {"notices": assistant.notices, "quiet": settings["quiet_mode"]}


@app.post("/api/quiet")
async def set_quiet(body: dict):
    settings.update({"quiet_mode": bool(body.get("on"))})
    await assistant.broadcast({"type": "settings", "settings": settings.all()})
    return {"quiet": settings["quiet_mode"]}


@app.post("/api/update")
async def run_update():
    assistant.spawn(assistant.auto_update())
    return {"ok": True}


@app.get("/api/devices")
async def get_devices():
    """Home devices: Spotify Connect ones (music can be sent to them) and those seen on the network."""
    sp = assistant.spotify
    music = []
    if sp.connected:
        try:
            for d in await asyncio.to_thread(sp.devices):
                music.append({"id": d["id"], "name": "This laptop" if sp._is_laptop(d["name"]) else d["name"],
                              "type": d.get("type", ""), "active": bool(d.get("is_active")),
                              "volume": d.get("volume_percent")})
        except SpotifyError:
            pass
    return {"music": music, "lan": assistant.devices.list() if assistant.devices else [],
            "scanned_at": assistant.devices.scanned_at if assistant.devices else 0}


@app.post("/api/devices/music")
async def music_to_device(body: dict):
    """Move Spotify playback to that device (by Spotify Connect id)."""
    sp = assistant.spotify
    try:
        await asyncio.to_thread(sp.move_to, str(body["id"]))
    except (SpotifyError, KeyError) as e:
        return {"ok": False, "error": str(e) or "Missing device"}
    assistant.spawn(assistant._spotify_refresh(delay=1))
    return {"ok": True}


@app.post("/api/devices/scan")
async def scan_devices():
    if assistant.devices:
        await asyncio.to_thread(assistant.devices.scan)
    return await get_devices()


@app.get("/api/video")
async def video_status():
    player = await asyncio.to_thread(assistant._current_player)
    return await asyncio.to_thread(player.status) if player else {"playing": False}


@app.get("/api/video/services")
async def video_services():
    return {"services": [{"name": s["name"], "url": s["url"]} for s in settings["video_services"] if s.get("enabled", True)]}


@app.get("/api/web/accounts")
async def web_accounts():
    return webaccounts.accounts()   # no passwords


@app.post("/api/web/accounts")
async def save_web_account(body: dict):
    try:
        site = await asyncio.to_thread(webaccounts.save, str(body.get("site", "")), str(body.get("user", "")),
                                       str(body.get("password", "")))
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "site": site}


@app.post("/api/web/accounts/delete")
async def delete_web_account(body: dict):
    await asyncio.to_thread(webaccounts.delete, str(body.get("site", "")))
    return {"ok": True}


@app.post("/api/video")
async def video_control(body: dict):
    action = body.get("action")
    if action == "play" and body.get("id"):
        try:
            await assistant.play_video(str(body["id"]), str(body.get("title", ""))[:200], body.get("rect"))
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}
    elif action == "rect" and isinstance(body.get("rect"), dict):
        await asyncio.to_thread(assistant.video.set_rect, body["rect"])
        if assistant.web.docked and await asyncio.to_thread(assistant.web.active):
            await asyncio.to_thread(assistant.web.dock, body["rect"])
        else:
            assistant.web.rect = body["rect"]
        return {"ok": True}
    elif action == "dock":
        await asyncio.to_thread(assistant.web.dock, body.get("rect") if isinstance(body.get("rect"), dict) else None)
    elif action == "undock":
        await asyncio.to_thread(assistant.web.undock)
    elif action == "hide":
        await asyncio.to_thread(assistant.web.hide)
        return {"ok": True}
    elif action == "open" and body.get("url"):
        if isinstance(body.get("rect"), dict):
            assistant.web.rect = body["rect"]
        reason = await assistant.watch(str(body["url"]))
        if reason:
            return {"ok": False, "error": reason}
    elif action == "find" and body.get("service") and body.get("query"):   # search on a service or on any site
        ok, said = await assistant.watch_find(str(body["service"]), str(body["query"]))
        return {"ok": ok, "text": said}
    elif action in ("toggle", "seek", "volume", "fullscreen", "stop", "pause", "resume"):
        player = await asyncio.to_thread(assistant._current_player)
        if player:
            await asyncio.to_thread(player.command, action, body.get("value"))
    assistant.spawn(assistant._video_refresh())
    return {"ok": True}


@app.get("/api/timers")
async def get_timers():
    return {"timers": timers.active(), "now": time.time()}


@app.post("/api/timers")
async def set_timer(body: dict):
    if body.get("cancel") is not None:
        timers.cancel(str(body["cancel"]) or None)
    else:
        seconds = int(float(body.get("seconds", 0)))
        if not 0 < seconds <= 24 * 3600:
            return {"ok": False, "error": "Invalid duration"}
        timers.add(seconds, str(body.get("label", ""))[:40])
    await assistant.broadcast({"type": "timers", "timers": timers.active()})
    return {"ok": True, "timers": timers.active(), "now": time.time()}


@app.get("/api/compra")
async def get_compra():
    return {"items": await asyncio.to_thread(compra.items)}


@app.post("/api/compra")
async def change_compra(body: dict):
    try:
        if body.get("add"):
            items = await asyncio.to_thread(compra.add, str(body["add"]))
        elif body.get("toggle") is not None:
            items = await asyncio.to_thread(compra.toggle, int(body["toggle"]))
        elif body.get("clear_done"):
            items = await asyncio.to_thread(compra.clear_done)
        else:
            items = await asyncio.to_thread(compra.items)
    except OSError as e:
        return {"ok": False, "error": f"Could not save the list: {e}"}
    await assistant.broadcast({"type": "compra", "items": items})
    return {"ok": True, "items": items}


@app.post("/api/tasks")
async def edit_task(body: dict):
    """Manual tasks from the panel: add, edit, done or delete (by its position «i» in tareas.yaml). No confirmation."""
    from ruamel.yaml import YAML
    from ruamel.yaml.scalarstring import DoubleQuotedScalarString as Q
    action = body.get("action")
    path = datos.folder() / "tareas.yaml"
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)

    def apply():
        raw = path.read_bytes() if path.exists() else b""
        crlf = b"\r\n" in raw
        data = y.load(raw.decode("utf-8")) if raw else None
        data = data if data is not None else {}
        pend = data.setdefault("pendientes", [])
        text = str(body.get("tarea") or "").strip()[:200]
        fields = {k: str(body[k]).strip()[:200] for k in ("cuando", "prioridad", "notas") if k in body}
        if action == "add":
            if not text:
                return "Write the task"
            item = {"tarea": Q(text), "prioridad": fields.get("prioridad") or "media", "estado": "pendiente"}
            if fields.get("cuando"):
                item["cuando"] = Q(fields["cuando"])
            pend.insert(0, item)   # at the top (so the gap before «recurrentes» is not disturbed)
        else:
            i = body.get("i")
            if not isinstance(i, int) or not 0 <= i < len(pend):
                return "Task not found"
            t = pend[i]
            if action == "delete":
                del pend[i]
            elif action == "done":
                del pend[i]
                t["estado"] = "hecha"
                t["fecha"] = date.today()
                data.setdefault("hechas", []).insert(0, t)
            elif action == "edit":
                if text:
                    t["tarea"] = Q(text)
                for k, v in fields.items():
                    if v:
                        t[k] = Q(v) if k != "prioridad" else v
                    elif k in t and k != "prioridad":
                        del t[k]
            else:
                return "Unknown action"
        backup = ROOT / "logs" / "backups"
        backup.mkdir(parents=True, exist_ok=True)
        if path.exists():
            (backup / f"tareas-{time.strftime('%Y%m%d-%H%M%S')}.yaml").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        with path.open("w", encoding="utf-8", newline="\r\n" if crlf else "\n") as f:
            y.dump(data, f)
        return None

    err = await asyncio.to_thread(apply)
    if err:
        return {"ok": False, "error": err}
    log.info("Task from the panel: %s", action)
    await assistant._refresh_data()
    return {"ok": True, "data": await asyncio.to_thread(datos.summary)}


@app.get("/api/bank")
async def get_bank():
    """Revolut balance for the panel (from the cache: the bank only allows about 4 queries a day without you present)."""
    data = await asyncio.to_thread(bank.summary)
    if not data.get("accounts"):
        return {"ok": False}
    return {"ok": True, "consent_days": data.get("consent_days"), "fetched_at": data.get("fetched_at"),
            "stale": bool(data.get("stale")),
            "accounts": [{"masked": a.get("masked"), "balance": a.get("balance"), "spending": a.get("spending"),
                          "recent": (a.get("transactions") or [])[:12]} for a in data["accounts"]]}


# data files that can be edited from the panel (reminders have their own form)
EDITABLE = {"tareas": "tareas.yaml", "comidas": "comidas.yaml", "gym": "gym.yaml", "dinero": "dinero.yaml",
            "fechas": "fechas.yaml", "betis": "betis.yaml", "horario": "horario.yaml", "compra": "compra.md"}


@app.get("/api/file/{name}")
async def read_file(name: str):
    if name not in EDITABLE:
        return {"ok": False, "error": "That file cannot be edited"}
    path = datos.folder() / EDITABLE[name]
    text = await asyncio.to_thread(path.read_text, encoding="utf-8") if path.exists() else ""
    return {"ok": True, "name": name, "file": EDITABLE[name], "text": text}


@app.post("/api/file/{name}")
async def write_file(name: str, body: dict):
    if name not in EDITABLE:
        return {"ok": False, "error": "That file cannot be edited"}
    text = str(body.get("text", ""))
    file = EDITABLE[name]
    if file.endswith(".yaml"):
        import yaml
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            where = f" (line {mark.line + 1})" if mark else ""
            return {"ok": False, "error": f"Invalid format{where}: {getattr(e, 'problem', None) or e}"}
        if data is not None and not isinstance(data, dict):
            return {"ok": False, "error": "The file must have the form «key: value»"}
    path = datos.folder() / file
    # copy of the previous version just in case (the last 20, in the code folder)
    backup = ROOT / "logs" / "backups"
    backup.mkdir(parents=True, exist_ok=True)
    if path.exists():
        (backup / f"{path.stem}-{time.strftime('%Y%m%d-%H%M%S')}{path.suffix}").write_text(
            path.read_text(encoding="utf-8"), encoding="utf-8")
        for old in sorted(backup.glob(f"{path.stem}-*"))[:-20]:
            old.unlink(missing_ok=True)
    await asyncio.to_thread(path.write_text, text, encoding="utf-8")
    log.info("Edited from the panel: %s", file)
    assistant.spawn(assistant._refresh_data())
    return {"ok": True}


@app.get("/api/camera.mjpg")
async def camera_stream():
    """The camera video comes from Cortex Sense, which is the one holding the camera open."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(settings["sense_url"] + "/api/camera.mjpg")


@app.post("/api/gestures/record")
async def record_gesture(body: dict):
    name = str(body.get("name", "")).strip()
    if not assistant.vision or not await asyncio.to_thread(assistant.vision.record_custom, name):
        return {"error": "Cannot record right now: is the camera on and does the name have no «:»?"}
    return {"ok": True}


@app.post("/api/gestures/delete")
async def delete_gesture(body: dict):
    ok = bool(assistant.vision and await asyncio.to_thread(assistant.vision.delete_custom, str(body.get("name", ""))))
    await assistant._send_vision(force=True)
    return {"ok": ok}


@app.post("/api/wake/retrain")
async def wake_retrain():
    assistant.spawn(assistant.retrain_wake())
    return {"ok": True}


@app.get("/api/avisos")
async def get_avisos():
    return await asyncio.to_thread(avisos.upcoming, datetime.now())


@app.post("/api/avisos")
async def add_aviso(body: dict):
    when = avisos.parse_when(body.get("cuando"))
    if not when or not str(body.get("texto", "")).strip():
        return {"error": "The reminder is missing its date or text."}
    try:
        await asyncio.to_thread(avisos.add, when, str(body["texto"]), str(body.get("repetir", "")))
    except ValueError as e:
        return {"error": f"Not saved: {e}."}
    await assistant.broadcast_avisos()
    return {"ok": True}


@app.post("/api/avisos/delete")
async def delete_aviso(body: dict):
    ok = await asyncio.to_thread(avisos.delete, str(body.get("id", "")))
    await assistant.broadcast_avisos()
    return {"ok": ok}


@app.get("/api/weather")
async def get_weather():
    return await asyncio.to_thread(weather.forecast)


@app.post("/api/weather/place")
async def set_weather_place(body: dict):
    import httpx
    name = str(body.get("name", "")).strip()
    try:
        r = await asyncio.to_thread(httpx.get, "https://geocoding-api.open-meteo.com/v1/search",
                                    params={"name": name, "count": 1, "language": "es"}, timeout=8)
        found = (r.json().get("results") or [None])[0]
    except Exception:
        return {"error": "Cannot reach the city search; is there an internet connection?"}
    if not found:
        return {"error": f"Could not find «{name}»."}
    settings.update({"weather_place": {"name": found["name"], "lat": found["latitude"], "lon": found["longitude"]}})
    await assistant.broadcast({"type": "settings", "settings": settings.all()})
    return await asyncio.to_thread(weather.forecast)


@app.post("/api/voice/test")
async def test_voice(body: dict):
    lang = body.get("lang", "es")
    text = {"es": "¡Hola! Soy Cortex, tu asistente de voz. Así sueno ahora mismo.", "en": "Hi! I'm Cortex. This is how I sound."}[lang]   # one sample per voice language
    assistant._speak_lang = lang
    assistant._queue_sentence(text)
    return {"ok": True}


def _page(message: str) -> HTMLResponse:
    return HTMLResponse(f"<p style='font:18px system-ui;padding:40px'>{message} <a href='/'>Back</a></p>")


# ---------- first run ----------
@app.get("/api/setup")
async def setup_state():
    """Which accounts are already connected (for the setup wizard). No account data."""
    return {"claude": await asyncio.to_thread(claude_status),
            "spotify": {"client_id": bool(assistant.spotify.client_id), "connected": assistant.spotify.connected,
                        "redirect_uri": assistant.spotify.redirect_uri},
            "email": {"connected": assistant.mail.configured},
            "whatsapp": {"available": assistant.wa.available, "state": assistant.wa.state, "qr": bool(assistant.wa.qr)}}


@app.post("/api/setup/claude")
async def setup_claude():
    try:
        await asyncio.to_thread(claude_login)
    except (OSError, BrainError) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


@app.post("/api/setup/claude/start")
async def setup_claude_start():
    """After signing in: start the brain without restarting Cortex."""
    assistant.spawn(assistant._start_brain())
    return {"ok": True}


@app.post("/api/setup/spotify")
async def setup_spotify(body: dict):
    client_id = str(body.get("client_id", "")).strip()
    if not re.fullmatch(r"[0-9a-fA-F]{32}", client_id):
        return {"ok": False, "error": "The Client ID is 32 letters and digits (a-f, 0-9)."}
    await asyncio.to_thread(save_env, {"SPOTIFY_CLIENT_ID": client_id})
    return {"ok": True}


@app.post("/api/setup/whatsapp")
async def setup_whatsapp():
    assistant.wa.start(link=True)
    return {"ok": assistant.wa.available}


@app.post("/api/quit")
async def quit_app():
    """Quit Cortex entirely (with no console there is no other window to close)."""
    await asyncio.to_thread(assistant.brain.stop)
    assistant.wa.stop()
    asyncio.get_running_loop().call_later(0.3, os._exit, 0)
    return {"ok": True}


@app.get("/spotify/login")
async def spotify_login():
    try:
        return RedirectResponse(assistant.spotify.login_url())
    except SpotifyError as e:
        return _page(f"{e}.")


@app.get("/spotify/callback")
async def spotify_callback(code: str = "", state: str = "", error: str = ""):
    if error:
        return _page(f"Spotify said: {error}.")
    try:
        await asyncio.to_thread(assistant.spotify.finish_login, code, state)
    except SpotifyError as e:
        return _page(f"{e}.")
    assistant.spawn(assistant._spotify_refresh())
    return RedirectResponse("/")


@app.post("/api/spotify")
async def spotify_command(body: dict):
    sp = assistant.spotify
    action, value = body.get("action"), body.get("value")
    try:
        if action == "logout":
            sp.logout()
        elif action == "devices":
            return {"devices": [d["name"] for d in await asyncio.to_thread(sp.devices)]}
        elif action == "toggle":
            await asyncio.to_thread(sp.pause if sp.last_state.get("is_playing") else sp.resume)
        elif action in ("next", "previous"):
            await asyncio.to_thread(getattr(sp, action))
        elif action == "volume":
            await asyncio.to_thread(sp.set_volume, int(value))
    except SpotifyError as e:
        return {"error": str(e)}
    assistant.spawn(assistant._spotify_refresh(delay=0.4))
    return {"ok": True}


@app.get("/api/layout")
async def get_layout():
    return json.loads(LAYOUT_FILE.read_text(encoding="utf-8")) if LAYOUT_FILE.exists() else []


@app.post("/api/layout")
async def save_layout(items: list[dict]):
    # a panel running an old version does not know the new widgets: any it does not send are kept
    sent = {i.get("id") for i in items}
    try:
        old = json.loads(LAYOUT_FILE.read_text(encoding="utf-8")) if LAYOUT_FILE.exists() else []
    except ValueError:
        old = []
    items = items + [i for i in old if i.get("id") not in sent]
    LAYOUT_FILE.write_text(json.dumps(items, indent=1), encoding="utf-8")
    return {"ok": True}


@app.post("/api/devices/refresh")
async def refresh_devices():
    await asyncio.to_thread(assistant.open_devices)
    await assistant.broadcast(assistant.status())
    return await get_settings()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    # WebSockets have no CORS protection: without this, any website open in the browser could
    # connect, read the conversation and give it orders (send emails…). Only the panel (or local scripts without Origin).
    origin = ws.headers.get("origin")
    if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
        log.warning("WebSocket rejected from %s", origin)
        await ws.close(code=1008)
        return
    await ws.accept()
    assistant.clients.add(ws)
    await ws.send_json({"type": "hello", "state": assistant.state, "history": assistant.history,
                        "settings": settings.all(), "web": web_version()})
    await ws.send_json(assistant.status())
    await ws.send_json({"type": "spotify", **assistant.spotify.summary()})
    await ws.send_json({"type": "whatsapp", **assistant.wa.summary()})
    await ws.send_json({"type": "mail", **assistant.mail.summary()})
    if assistant.vision:
        await ws.send_json({"type": "vision", **assistant.vision.state()})
    try:
        while True:
            msg = await ws.receive_json()
            action = msg.get("action")
            if action == "press":
                await assistant.press()
            elif action == "stop":
                await assistant.interrupt()
            elif action == "text" and msg.get("text", "").strip():
                await assistant.send_text(msg["text"])
            elif action == "enroll":
                await assistant.enroll()
            elif action == "enroll_clear" and assistant.listener:
                await asyncio.to_thread(assistant.listener.print.clear_samples)
                await assistant.broadcast(assistant.status())
            elif action == "unmute_mic":
                assistant.mic_mute.unmute()
                await asyncio.sleep(0.5)
                await assistant.broadcast(assistant.status())
            elif action == "wake_toggle":
                settings.update({"wake_enabled": not settings["wake_enabled"]})
                await assistant.broadcast(assistant.status())
            elif action == "new_conversation":
                await assistant.interrupt()
                await asyncio.to_thread(assistant.brain.new_conversation)
                assistant.history = []
                await assistant.broadcast({"type": "cleared"})
                await assistant.broadcast(assistant.status())
    except WebSocketDisconnect:
        pass
    finally:
        assistant.clients.discard(ws)
