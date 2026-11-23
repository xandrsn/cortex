"""Servidor local y orquestador: oído → transcripción → router/Claude → voz."""

import asyncio
import json
import logging
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
from .mail import Mail
from .whatsapp import WhatsApp
from .brain import one_shot
from .brain import Brain, BrainError
from .config import MODELS_DIR, ROOT, WEB_DIR, settings

WHO = settings["user_name"] or "el usuario"   # nombre configurable en config.yaml (user_name)
from .listener import BargeIn, Listener, find_wake
from .router import is_ack, route
from .spotify import LIKED, Spotify, SpotifyError, norm
from .stt import MODELS as STT_MODELS, Transcriber, guess_lang, normalize, save_clip
from .vision import Vision

log = logging.getLogger("pepito")

EDGE_VOICES = {
    # las multilingües son la generación nueva (más naturales) y hablan español; Andrew se descarta: lento y corta frases
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
ANY_TAG = re.compile(r"\[\[.*?\]\]", re.S)   # etiquetas para el sistema: nunca se leen ni se ven
SENTENCE_END = re.compile(r"(.+?[.!?…:;](?=\s)|.+?\n)", re.S)
LAYOUT_FILE = ROOT / "layout.json"


# el detector salta antes de que acabe la palabra: «…to, pon música» o «Pepito, pon música» → «pon música»
WAKE_TAIL = re.compile(r"^\s*(?:(?:pe|pa|be)?pi)?to\b[\s,.;:!?]*", re.I)


def strip_wake(text: str) -> str:
    rest = find_wake(text, 2)
    if rest:
        text = rest
    return WAKE_TAIL.sub("", text, count=1).strip()


# «añade leche y huevos a la lista (de la compra)» · «qué hay en la lista de la compra»
COMPRA_ADD_RE = re.compile(r"^(?:a[ñn]ade|apunta|pon|mete|agrega)\s+(.+?)\s+(?:a|en)\s+la\s+lista(?:\s+de\s+la\s+compra)?[.!]*$",
                           re.I)
COMPRA_LIST_RE = re.compile(r"(qu[eé] (hay|tengo|falta) en|l[eé]eme|dime) la lista de la compra|qu[eé] (tengo|hay) que comprar",
                            re.I)


# su propia voz: «habla más despacio», «cambia de voz», «pon la voz de Elvira», «voz de mujer»
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
# [[ver: URL]] o [[ver: servicio | búsqueda]] (esta la resuelve video_sites con la API de la web)
WATCH_TAG = re.compile(r"\[\[\s*ver\s*:\s*(.+?)\s*\]\]", re.I)
# sin Claude: «pon el último telediario de RTVE», «pon La Revuelta de anoche en RTVE», «pon la 1», «pon antena 3 en directo»
WATCH_SITE_RE = re.compile(r"^(?:pon(?:me)?|quiero ver|echa|reproduce)\s+(?P<q>.+?)\s+(?:en|de)\s+"
                           r"(?P<s>rtve(?: play)?|pluto(?: tv)?|atres ?player)\s*[.!?]*$", re.I)
WATCH_LIVE_RE = re.compile(r"^(?:pon(?:me)?|quiero ver)\s+(?:el canal\s+)?(?P<q>la ?[12]|(?:el )?(?:canal )?24 horas|clan|"
                           r"teledeporte|antena ?3|la ?sexta|neox|nova|mega|atreseries)(?:\s+en directo)?\s*[.!?]*$", re.I)
# «pon la última de La Revuelta en RTVE», «quiero ver Stranger Things»… → a Claude se le pasan los servicios activos
WATCH_RE = re.compile(r"\b(ver|pon(me)?|reproduce|echa)\b.*\b(serie|peli|pel[ií]cula|cap[ií]tulo|episodio|temporada|"
                      r"documental|programa|directo|canal|telediario|la 1|la 2|netflix|prime|rtve|pluto|disney|"
                      r"atres|movistar)", re.I)
# [[guardar_web: nombre | URL]] (sin URL: la web abierta ahora en la ventana de vídeos)
SAVE_WEB_TAG = re.compile(r"\[\[\s*guardar[ _]web\s*:\s*(.+?)\]\]", re.I)
VOICE_TAG = re.compile(r"\[\[\s*voz\s*:\s*(.+?)\]\]", re.I)
# correo: [[correo: para | asunto | texto | cuenta?]] y [[responder: ID | texto]] (ID como p123 / s45)
MAIL_SEND_TAG = re.compile(r"\[\[\s*correo\s*:\s*(.+?)\]\]", re.I | re.S)
MAIL_REPLY_TAG = re.compile(r"\[\[\s*responder\s*:\s*([ps]\d+)\s*\|\s*(.+?)\]\]", re.I | re.S)
MAIL_ID_RE = re.compile(r"\b([ps]\d{1,9})\b")
RATE_TAG = re.compile(r"\[\[\s*velocidad\s*:\s*([+-]?\d+)\s*%?\s*\]\]", re.I)


def set_voice(name: str | None) -> str:
    """Cambia la voz en español (sin nombre: la siguiente de la lista). Devuelve el nombre corto."""
    name = norm(name or "")
    if name in VOICE_NAMES:
        voice = VOICE_NAMES[name]
    else:
        order = list(dict.fromkeys(v for v in VOICE_NAMES.values()))
        cur = settings["voice_es"]
        voice = order[(order.index(cur) + 1) % len(order)] if cur in order else order[0]
    settings.update({"voice_es": voice, "tts_engine": "edge"})
    log.info("Voz cambiada a %s", voice)
    return voice_label(voice)


def voice_label(voice: str) -> str:
    """«en-US-BrianMultilingualNeural» → «Brian»."""
    return voice.split("-")[2].removesuffix("Neural").removesuffix("Multilingual")


def set_rate(rate: int) -> int:
    rate = max(-30, min(60, int(rate)))
    settings.update({"tts_rate": rate})
    log.info("Velocidad de voz: %+d%%", rate)
    return rate


def local_tools(text: str) -> str | None:
    """Temporizadores, lista de la compra y su propia voz: se resuelven al momento, sin pasar por Claude."""
    t = text.strip()
    if QUIET_ON_RE.search(t):
        settings.update({"quiet_mode": True})
        return "Vale, modo silencio: los avisos solo saldrán en el panel."
    if QUIET_OFF_RE.search(t):
        settings.update({"quiet_mode": False})
        return "Vale, ya te aviso en voz alta."
    if FASTER_RE.search(t):
        set_rate(settings["tts_rate"] + 15)
        return "Vale, hablo más rápido."
    if SLOWER_RE.search(t):
        set_rate(settings["tts_rate"] - 15)
        return "Vale, hablo más despacio."
    if NORMAL_RATE_RE.search(t):
        set_rate(15)
        return "Vale, a velocidad normal."
    m = VOICE_RE.search(norm(t))   # sin tildes: «Álvaro», «cámbiate»
    if m:
        return f"Hola, ahora soy {set_voice(m.group("name"))}. ¿Qué tal sueno?"
    if timers.CANCEL_RE.search(t):
        n = timers.cancel()
        return "Temporizador quitado." if n == 1 else f"Quitados {n} temporizadores." if n else "No había ninguno."
    if timers.LEFT_RE.search(t):
        act = timers.active()
        if not act:
            return "No hay ningún temporizador."
        return ". ".join(f"{'Al de ' + a['label'] if a['label'] else 'Le'} quedan "
                         f"{timers.say_duration(a['end'] - time.time())}" for a in act) + "."
    parsed = timers.parse(t)
    if parsed:
        seconds, label = parsed
        timers.add(seconds, label)
        return f"Vale, {timers.say_duration(seconds)}" + (f" para {label}." if label else ".")
    m = COMPRA_ADD_RE.match(t.rstrip())
    if m:
        compra.add(m.group(1))
        return "Apuntado."
    if COMPRA_LIST_RE.search(t):
        pending = [i["text"] for i in compra.items() if not i["done"]]
        return ("En la lista: " + ", ".join(pending) + ".") if pending else "La lista está vacía."
    return None


# preguntas sobre la cuenta del banco: se le pasan a Claude los datos de Revolut (solo lectura)
MORNING_RE = re.compile(r"resumen (matinal|de la ma[ñn]ana|del d[ií]a)|buenos d[ií]as,? pepito", re.I)
BANK_RE = re.compile(r"revolut|saldo|movimientos|en el banco|en la cuenta|he gastado|mis gastos|presupuesto|"
                     r"c[oó]mo voy (de|con el) dinero|deber[ií]a tener",
                     re.I)
BANK_FRESH_RE = re.compile(r"actualiza|ahora mismo|en (este momento|vivo)|refresca", re.I)


# preguntas sobre los gestos: se le pasa a Claude la tabla real (la que hay configurada ahora)
GESTURE_RE = re.compile(r"\bgestos?\b|con la mano|con las manos|la c[aá]mara|\bse[ñn]as\b|qu[eé] hago para", re.I)
GESTURE_NAMES = {
    "swipe_left": "deslizar la mano a la izquierda", "swipe_right": "deslizar la mano a la derecha",
    "swipe_up": "deslizar hacia arriba", "swipe_down": "deslizar hacia abajo",
    "open_palm": "mano abierta", "closed_fist": "puño", "thumb_up": "pulgar arriba", "thumb_down": "pulgar abajo",
    "victory": "uve (dos dedos)", "pointing_up": "índice arriba", "i_love_you": "cuernos con pulgar",
    "ok": "OK (pulgar e índice en círculo)", "shaka": "shaka (pulgar y meñique)", "rock": "cuernos (índice y meñique)",
    "three": "tres dedos", "shush": "silencio (índice en los labios)",
    "flick_right": "toque de índice a la derecha", "flick_left": "toque de índice a la izquierda",
    "circle_cw": "círculo con el dedo (horario)", "circle_ccw": "círculo con el dedo (antihorario)",
    "wag": "dedo que dice «no»", "wave": "saludar con la mano", "push": "empujar hacia la cámara",
    "both_palms": "dos manos abiertas", "both_thumbs_up": "dos pulgares arriba", "both_thumbs_down": "dos pulgares abajo",
    "both_fists": "dos puños", "hands_apart": "separar las manos", "hands_together": "juntar las manos",
}
ACTION_NAMES = {
    "next": "siguiente canción", "previous": "canción anterior", "toggle": "pausar / seguir la música",
    "volume_up": "subir volumen", "volume_down": "bajar volumen", "pepito": "ponerse a escuchar (como decir «Pepito»)",
    "stop": "cortar a Pepito (para todo)", "quiet": "callar a Pepito (deja de hablar pero sigue con la tarea)",
    "ambient": "modo reposo", "wake_toggle": "pausar/activar la palabra «Pepito»",
    "new_chat": "conversación nueva", "theme": "cambiar tema claro/oscuro",
}


def site_url(service: str) -> str | None:
    """«cuevana.biz», «https://x.tv/…» o el nombre de un servicio de Ajustes → la URL de su portada."""
    s = service.strip().strip("/")
    for v in settings["video_services"]:
        if v["name"].lower() == s.lower():
            return v["url"]
    if re.fullmatch(r"(https?://)?[\w-]+(\.[\w-]+)+(/\S*)?", s, re.I):
        return s if s.lower().startswith("http") else "https://" + s
    return None


def save_web(name: str, url: str | None, web) -> str:
    """Guarda una web como servicio de vídeo con ese nombre (Ajustes → Vídeo): luego «busca X en <nombre>»."""
    if not url:
        page = web._page()
        url = page.get("url") if page else None
        if not url or not url.startswith("http"):
            return "No hay ninguna web abierta para guardar."
    url = site_url(url) or url
    p = urlparse(url)
    root = f"{p.scheme}://{p.netloc}/"
    domain = ".".join(p.hostname.removeprefix("www.").split(".")[-2:]) if p.hostname else ""
    services = [v for v in settings["video_services"] if v["name"].lower() != name.lower()]
    services.append({"name": name, "url": root, "domains": [domain], "enabled": True})
    settings.update({"video_services": services})
    log.info("Web guardada: %s → %s", name, root)
    return f"Guardada «{name}» ({domain})."


def watch_context() -> str:
    on = [s for s in settings["video_services"] if s.get("enabled", True)]
    return ("[Servicios de vídeo activados (dato del sistema): " + "; ".join(f"{s['name']} ({', '.join(s['domains'])})"
            for s in on) + ". Para poner algo escribe al final [[ver: servicio | qué]] (p. ej. [[ver: RTVE | último "
            "telediario]], [[ver: RTVE | La Revuelta de anoche]], [[ver: RTVE | La 1 en directo]], [[ver: Pluto TV | canal "
            "de películas de acción]], [[ver: Atresplayer | El Hormiguero de anoche]]): Pepito lo busca en la propia web, "
            "lo abre y le da al play. Solo si tienes la URL exacta de la página del vídeo usa [[ver: URL]]; no pongas la "
            "portada. Los directos de RTVE piden cuenta; canales temáticos sin cuenta: Pluto TV. Netflix, Prime Video y "
            "Disney+: busca el título y pone el primer resultado. Cualquier otra web de vídeo también vale: "
            "[[ver: dominio.com | qué]] (p. ej. [[ver: dailymotion.com | resumen del Betis]]) busca en esa web, quita "
            f"anuncios y ventanas emergentes y pone el primer resultado; si {WHO} no dice la dirección y no la sabes "
            "seguro, pregúntasela.]")


UPDATE_PROMPT = (
    "Actualización automática (nadie espera respuesta en voz). 1) Busca con WebSearch los próximos partidos del Real "
    "Betis (fecha, hora, rival, competición) y corrige betis.yaml solo si algo ha cambiado o falta. 2) Mira tareas.yaml "
    "sin cambiar nada y fíjate en lo que vence hoy o mañana. No toques otros archivos. Contesta SOLO con las novedades "
    f"importantes para {WHO}, cada una en una línea que empiece por '- ' (máximo 3, muy cortas), o exactamente NADA.")
STATE_FILE = ROOT / "logs" / "auto_update.json"
YES_RE = __import__("re").compile(r"^(s[ií]|vale|venga|dale|claro|porfa|por favor|res[uú]melo|l[eé]emelo|ok|okey)\b", 2)


def claude_usage(plan: dict) -> dict:
    """Lo que queda del plan de Claude (ventana de 5 h), del último aviso de límites que manda el propio claude."""
    win = ((plan or {}).get("unifiedWindows") or {}).get("five_hour") or {}
    if "utilization" not in win:
        return {}
    resets = datetime.fromtimestamp(win["resetsAt"]).strftime("%H:%M") if win.get("resetsAt") else ""
    week = ((plan.get("unifiedWindows") or {}).get("seven_day") or {}).get("utilization")
    return {"left": max(0.0, 100 - win["utilization"] * 100), "resets": f"a las {resets}" if resets else "",
            "week_left": None if week is None else max(0.0, 100 - week * 100)}


def web_version() -> int:
    """Cambia cuando se toca el panel (html/js/css): el panel abierto se recarga solo."""
    return int(max(p.stat().st_mtime for p in WEB_DIR.iterdir() if p.is_file()))


def gesture_context(vision: "Vision | None") -> str:
    actions = settings["gesture_actions"]
    used = [f"- {GESTURE_NAMES.get(g, g)} → {ACTION_NAMES.get(a, a)}" for g, a in actions.items() if a and a != "none"]
    free = [GESTURE_NAMES.get(g, g) for g, a in actions.items() if not a or a == "none"]
    custom = vision.list_custom() if vision else []
    lines = [f"[Gestos configurados ahora mismo (dato del sistema, no lo dice {WHO}):"] + used
    for c in custom:
        a = actions.get(c["id"], "none")
        lines.append(f"- gesto propio «{c['name']}» → {ACTION_NAMES.get(a, 'nada')}")
    lines.append(f"- Libres (sin acción): {', '.join(free)}")
    lines.append("- Cámara " + ("encendida" if vision else "apagada")
                 + (". Solo cuentan mirando a la pantalla" if settings["gestures_require_look"] else "")
                 + ". Para cambiarlos o grabar uno propio: en el panel, Ajustes (la rueda, arriba a la derecha) → pestaña "
                 "Gestos: ahí se elige la acción de cada gesto y se graban gestos propios. El chip «Gestos» de arriba abre "
                 "la cámara para ver qué detecta. Tú no puedes cambiarlos.]")
    return "\n".join(lines)


def bank_context(text: str) -> str:
    """Bloque con saldo y últimos movimientos para añadir a la pregunta (vacío si no hay banco conectado)."""
    data = bank.summary(force=bool(BANK_FRESH_RE.search(text)))
    if not data.get("ok") and not data.get("accounts"):
        return ""
    when = datetime.fromtimestamp(data.get("fetched_at", time.time())).strftime("%d/%m %H:%M")
    lines = [f"[Datos de Revolut (solo lectura), consultados el {when}"
             + (", puede que no estén al día" if data.get("stale") else "") + ":"]
    for a in data.get("accounts", []):
        b = a.get("balance") or {}
        lines.append(f"- Cuenta {a.get('masked')}: saldo {b.get('amount')} {b.get('currency')}")
        for t in a.get("transactions", [])[:10]:
            lines.append(f"  {t['date']}  {t['amount']:+.2f} {t['currency']}  {t['description']}"
                         + (" (pendiente)" if t.get("pending") else ""))
    for a in data.get("accounts", []):
        sp = a.get("spending") or {}
        if sp:
            cats = ", ".join(f"{k} {v:.0f} €" for k, v in sp.get("categorias", [])[:6])
            lines.append(f"- Gasto con tarjeta este mes ({sp['mes']}, día {sp['dia']} de {sp['dias_mes']}): "
                         f"{sp['gastado']:.0f} € ({sp['media_diaria']:.0f} €/día, a este ritmo {sp['proyeccion']:.0f} € "
                         f"a fin de mes; el mes pasado {sp['mes_anterior']:.0f} €). Por categorías: {cats}")
    if data.get("consent_days") is not None:
        lines.append(f"- El permiso del banco caduca en {data['consent_days']} días]")
    else:
        lines[-1] += "]"
    return "\n".join(lines)


class Pepito:
    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[WebSocket] = set()
        self.state = "idle"
        self.player = audio.Player()
        self.mic = audio.Mic()
        self.stt = Transcriber()
        self.brain = Brain()
        self.estado = datos.EstadoWriter()
        self.vision: Vision | None = None
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
        self._offer: tuple[float, str] | None = None   # «¿quieres que te lo resuma?»: (momento, lo que se haría)
        self.notices: list[dict] = []
        self._notice_lock = asyncio.Lock()
        self._video_status: dict = {"playing": False}
        self.history: list[dict] = []
        self._turn: asyncio.Task | None = None
        self._sentences: asyncio.Queue = asyncio.Queue()
        self._speak_lang = "es"
        self._muted_until = 0.0
        self._outputs: str | None = None
        self._pending = ""
        self._raw = ""        # texto de Claude tal cual, con las etiquetas [[musica: …]]
        self._shown = ""      # lo que ya se ha mostrado/hablado (sin etiquetas)
        self._spoke = False   # ¿ya ha dicho algo en este turno?
        self._turn_t = 0.0    # cuándo empezó el turno (0 = ya ha hablado)
        self._gen = 0         # cada turno tiene su número: el audio de un turno interrumpido se descarta
        self._quiet_gen = -1   # turno al que se ha mandado callar (gesto de silencio): sigue, pero sin voz
        self._enroll_left = 0  # muestras de «Pepito» que faltan por grabar
        self.mic_mute = audio.WindowsMicMute()
        self.spotify = Spotify(settings["ui_port"])
        self._duck_lock = asyncio.Lock()
        self._no_duck = False   # durante una orden de música no se baja el volumen
        self._followup = False  # ¿la escucha actual es la de después de responder?
        self._tasks: set[asyncio.Task] = set()

    # ---------- arranque ----------
    async def start(self):
        self.loop = asyncio.get_running_loop()
        self.loop.set_exception_handler(_quiet_resets)
        keep_awake()
        timers.load()   # los temporizadores sobreviven a un reinicio
        self.devices = Devices()
        self.open_devices()
        await asyncio.gather(
            asyncio.to_thread(self.stt.load, settings["stt_model"]),
            asyncio.to_thread(self.brain.start),
        )
        self.listener = Listener(self.mic, self.stt, self._threadsafe(self._on_wake),
                                 self._threadsafe(self._on_command), self._threadsafe(self._on_timeout))
        self.barge = BargeIn(self.mic, self.stt, self._threadsafe(self._on_barge))
        self.spawn(asyncio.to_thread(tts.preload_piper))
        self.vision = Vision(on_gesture=self._threadsafe(self._on_gesture),
                             on_presence=self._threadsafe(self._on_presence))
        self.vision.start()
        self.wa.start()
        self.spawn(self._speaker())
        self.spawn(self._monitor())
        log.info("Pepito listo")

    def open_devices(self):
        self.player.close()
        self.mic.close()
        audio.refresh_devices()
        for dev, key in ((self.player, "speaker_device"), (self.mic, "mic_device")):
            try:
                dev.open(settings[key])
            except Exception as e:
                log.error("No puedo abrir %s: %s", key, e)
                dev.ok = False

    def _threadsafe(self, coro_fn):
        """Para que los hilos (el oído) puedan lanzar corrutinas en el bucle principal."""
        def call(*args):
            self.loop.call_soon_threadsafe(lambda: self.spawn(coro_fn(*args)))
        return call

    def spawn(self, coro) -> asyncio.Task:
        """Lanza una tarea suelta guardando la referencia (si no, el recolector puede llevársela a medias) y
        dejando sus errores en el registro (si no, solo salen como «Task exception was never retrieved»)."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task):
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("Error en una tarea de fondo", exc_info=task.exception())

    # ---------- comunicación con el panel ----------
    async def broadcast(self, msg: dict):
        for ws in list(self.clients):
            try:
                # con límite: un panel colgado que no lee no puede parar el bucle de vigilancia (y el micro)
                await asyncio.wait_for(ws.send_json(msg), 5)
            except Exception:
                if ws in self.clients:
                    self.clients.discard(ws)
                    self.spawn(self._drop(ws))

    async def _drop(self, ws: WebSocket):
        """Cierra un panel que no responde: si sigue vivo, se reconecta solo."""
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
                tts.warm()   # la conexión de la voz, lista para cuando toque contestar
            if self.vision:
                self.vision.yield_cpu = state == "transcribing"
            await self.broadcast({"type": "state", "state": state})
            if self.spotify.connected or self.video.playing() or self._video_status.get("source") == "web":
                self.spawn(self._duck(state != "idle" and not self._no_duck))

    async def _duck(self, down: bool):
        """Baja la música mientras Pepito escucha o habla, y la devuelve al terminar."""
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
                log.exception("Error bajando la música")

    # ---------- entradas ----------
    async def press(self):
        """Botón del panel."""
        if self.state == "listening":
            self.listener.finish()
            return
        await self.interrupt()
        await self._listen()

    async def send_text(self, text: str):
        await self.interrupt()
        self._start_turn(self._handle(text.strip(), guess_lang(text), "texto"))

    async def _video_command(self, text: str) -> str | None:
        """«pon en youtube X» / «quita el vídeo» / «pausa el vídeo» / «adelanta 30 segundos»… (con mpv)."""
        t = text.strip()
        player = await asyncio.to_thread(self._current_player)
        if player:
            m = youtube.SEEK_RE.search(t)
            simple = {youtube.CLOSE_RE: ("stop", None, "Quitado."), youtube.PAUSE_RE: ("pause", None, "Pausado."),
                      youtube.RESUME_RE: ("resume", None, "Sigo."), youtube.BIG_RE: ("fullscreen", 1, "Ahí lo tienes."),
                      youtube.SMALL_RE: ("fullscreen", 0, "Vale.")}
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
                return "Hecho."
        # webs de vídeo gratis con su propia búsqueda; si no encuentra nada, sigue Claude
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
            return "No he encontrado ningún vídeo."
        await self.play_video(results[0]["id"], results[0]["title"])
        await self.broadcast({"type": "video_results", "results": results, "query": m.group(1)})
        return f"Pongo «{results[0]['title']}»."

    async def notify(self, text: str, offer: str | None = None, kind: str = "info"):
        """Aviso que Charlie ha pedido (correo nuevo, novedades de la actualización). En silencio solo sale en el panel."""
        item = {"text": text, "kind": kind, "time": time.time()}
        self.notices = ([item] + self.notices)[:20]
        await self.broadcast({"type": "notice", **item})
        log.info("Aviso (%s): %s", kind, text)
        if settings["quiet_mode"]:
            return
        if self.state != "idle":   # está hablando o escuchando: se dice en cuanto acabe, no se pierde
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
        async with self._notice_lock:   # varios avisos seguidos: uno detrás de otro
            if self.state != "idle":       # mientras esperaba turno le ha hablado (o contesta al anterior)
                self.spawn(self._notify_later(text, offer))
                return
            self._offer = (time.time(), offer) if offer else None
            self.player.play(audio.chime(self.player.rate), self.player.rate, mutes_mic=False)
            self._speak_lang = "es"
            await self._say(text)
            await self._wait_speech()
            if offer:
                await self._listen_followup(self._gen)

    async def check_mail(self):
        new = await asyncio.to_thread(self.mail.check)
        await self.broadcast({"type": "mail", **self.mail.summary()})
        if len(new) >= 3:
            # muchos de golpe (vuelta de suspensión, primera conexión): un solo aviso en vez de una retahíla
            whos = list(dict.fromkeys(m["from"].split("<")[0].strip() or m["addr"] for m in new))
            more = f" y {len(whos) - 3} más" if len(whos) > 3 else ""
            self.spawn(self.notify(f"Tienes {len(new)} correos nuevos: de {', '.join(whos[:3])}{more}.", kind="mail"))
            return
        for m in new:
            who = m["from"].split("<")[0].strip() or m["addr"]
            where = "" if len(self.mail.accounts) < 2 else f" (a la {m['account']})"
            # el aviso va aparte: la comprobación acaba ya y no se amontona con la siguiente mientras habla
            self.spawn(self.notify(f"Nuevo correo de {who}{where}: {m['subject']}. ¿Quieres que te lo resuma?",
                                   offer=f"Resúmeme en corto el correo {m['id']}.", kind="mail"))

    async def check_whatsapp(self):
        """WhatsApp (solo lectura): refresca el panel y avisa de lo nuevo, salvo de los chats silenciados (grupos, por defecto)."""
        new = await asyncio.to_thread(self.wa.check)
        await self.broadcast({"type": "whatsapp", **self.wa.summary()})
        media = [m for m in new if m["media"] in ("audio", "image", "sticker")]
        if media:   # WhatsApp borra los audios y fotos de sus servidores a las ~2 semanas: se guardan ya
            self.spawn(self._keep_media(media))
        by_chat: dict[str, list[str]] = {}
        for m in new:
            if not m["muted"]:
                by_chat.setdefault(m["chat"], []).append((f"{m['from']}: " if m["group"] else "") + m["text"])
        if len(by_chat) > 2:
            chats = list(by_chat)
            more = f" y {len(chats) - 3} más" if len(chats) > 3 else ""
            self.spawn(self.notify(f"Tienes WhatsApps nuevos de {', '.join(chats[:3])}{more}.", kind="whatsapp"))
            return
        for chat, texts in by_chat.items():
            text = texts[-1] if len(texts[-1]) <= 140 else texts[-1][:137] + "…"
            more = f" (y {len(texts) - 1} más)" if len(texts) > 1 else ""
            self.spawn(self.notify(f"WhatsApp de {chat}{more}: {text}", kind="whatsapp"))

    async def _keep_media(self, media: list[dict]):
        for m in media:
            try:
                await asyncio.to_thread(self.wa.media_file, m["jid"], m["id"])
            except Exception as e:
                log.info("WhatsApp: no pude guardar el %s %s: %s", m["media"], m["id"], e)

    async def _mail_action(self, fn, *args):
        """Enviar o responder (Charlie no quiere confirmaciones): se hace y se avisa en el panel."""
        try:
            said = await asyncio.to_thread(fn, *args)
            await self.broadcast({"type": "notice", "text": "✉️ " + said})
            self.spawn(self.check_mail())
        except Exception as e:
            log.warning("Correo: %s", e)
            msg = f"No he podido enviar el correo: {e}"
            await self.broadcast({"type": "error", "text": msg})
            await self._say(msg)

    async def auto_update(self):
        """Cada X horas: Claude repasa Betis y tareas (en una consulta aparte); se avisa solo si hay algo."""
        usage = claude_usage(self.brain.plan)
        if usage and usage["left"] < 25:
            log.info("Actualización automática aplazada: queda poco plan de Claude (%.0f%%)", usage["left"])
            return False
        news = []
        w = await asyncio.to_thread(weather.forecast)
        today = (w.get("days") or [{}])[0] if w.get("ok") else {}
        if (today.get("rain") or 0) >= 60:
            news.append(f"hoy puede llover en {w['place']} ({today['rain']}%)")
        try:
            out = await asyncio.to_thread(one_shot, UPDATE_PROMPT)
        except Exception as e:
            # sin red o el portátil se suspendió a medias: no se marca como hecha, se reintenta en 10 min
            log.warning("Actualización automática: %s", e)
            return False
        news += [l[2:].strip() for l in out.splitlines() if l.startswith("- ")][:3]
        log.info("Actualización automática: %s", news or "nada nuevo")
        STATE_FILE.write_text(json.dumps({"last_update": time.time()}), encoding="utf-8")
        if news:
            await self.notify("Novedades: " + "; ".join(news) + ".", kind="update")
        return True

    async def morning_context(self) -> str:
        """Lo que hace falta para el resumen matinal (botón 🌅 o «resumen matinal»); Claude decide qué contar."""
        now = datetime.now()
        parts = []
        if self.mail.configured:
            mails = await asyncio.to_thread(self.mail.recent, 12)
            parts.append("Correos de las últimas 12 h: " + ("; ".join(
                f"{m['id']} ({m['account']}{', sin leer' if not m['read'] else ''}) de {m['from']}: «{m['subject']}»"
                for m in mails) or "ninguno"))
        w = await asyncio.to_thread(weather.forecast)
        if w.get("ok"):
            d = (w.get("days") or [{}])[0]
            parts.append(f"Tiempo hoy en {w['place']}: ahora {w['temp']}° y {w['text']}; máx {d.get('max')}°, "
                         f"mín {d.get('min')}°, lluvia {d.get('rain', 0)}%")
        data = await asyncio.to_thread(datos.summary)
        today = data["today"]
        parts.append(f"Hoy es {today['day']} ({today['mode']}). Lo que le queda hoy: " + ("; ".join(
            f"{i.get('hora', '')} {i.get('que', '')}".strip() for i in today["items"]) or "nada"))
        parts.append("Tareas pendientes (todas; tú eliges las relevantes para hoy o estos días): " + "; ".join(
            f"{t['tarea']} [{t['prioridad']}{', ' + t['cuando'] if t['cuando'] else ''}{', ' + t['estado'] if t['estado'] else ''}]"
            for t in data["tasks"]))
        if data["betis"]:
            parts.append("Próximos del Betis: " + "; ".join(
                f"{m.get('partido', '')} {m['cuando']} {m.get('hora', '')}" for m in data["betis"][:2]))
        hoy = [a for a in avisos.upcoming(now) if a["cuando"].startswith(now.strftime("%Y-%m-%d"))]
        parts.append("Avisos de hoy: " + ("; ".join(f"{a['cuando'][11:]} {a.get('texto', '')}" for a in hoy) or "ninguno"))
        return ("[Resumen matinal (datos del sistema). Haz un resumen hablado, natural y corto (unas 8 frases, sin listas "
                "largas ni leer IDs): 1) los correos que importan (ignora publicidad y notificaciones automáticas); "
                "2) el tiempo de hoy; 3) busca con WebSearch si hoy hay algún partido de fútbol bueno (LaLiga, Champions, "
                "Europa League, selección o el Betis) con hora y dónde verlo; 4) lo que le toca hoy; 5) solo las tareas "
                "relevantes; 6) los avisos de hoy.\n" + "\n".join(parts) + "]")

    def live_context(self) -> str:
        """Lo que se ve y suena ahora (dato del sistema), para que Claude no tenga que preguntarlo."""
        parts = []
        sp = self.spotify.summary() if self.spotify.connected else {}
        if sp.get("title"):
            parts.append(f"música: «{sp['title']}» de {sp['artist']} en {sp.get('device') or '?'}"
                         + ("" if sp.get("playing") else " (en pausa)"))
        else:
            parts.append("música: nada")
        v = self._video_status
        if v.get("playing"):
            parts.append(f"vídeo: «{v.get('title', '')}»" + (" (en pausa)" if v.get("paused") else ""))
        w = weather.forecast()   # con caché de 15 min
        if w.get("ok"):
            parts.append(f"tiempo en {w['place']}: {w['temp']}° ({w['text'].lower()}, sensación {w['feels']}°)")
        act = timers.active()
        if act:
            parts.append("temporizadores: " + ", ".join(f"{a['label'] or 'sin nombre'} {timers.say_duration(a['end'] - time.time())}"
                                                     for a in act))
        try:
            lay = json.loads(LAYOUT_FILE.read_text(encoding="utf-8"))
            parts.append("panel (widgets visibles): " + ", ".join(i["id"] for i in lay if not i.get("hidden")))
        except (OSError, ValueError):
            pass
        if self.mail.configured:
            unread = self.mail.unread[:6]
            parts.append("correos sin leer: " + ("; ".join(f"[{m['id']}] {m['from']} → {m['account']}: «{m['subject']}»"
                                                           for m in unread) if unread else "ninguno"))
        if self.wa.state == "connected" and self.wa.chats:
            parts.append("WhatsApp (últimos chats, solo lectura): " + "; ".join(
                f"{c['name']}{' (grupo)' if c['group'] else ''}: {'yo' if c['mine'] else (c['from'] or c['name'])} «{c['text'][:80]}»"
                for c in self.wa.chats[:6]))
        parts.append(f"voz {voice_label(settings['voice_es'])} {int(settings['tts_rate']):+d}%, "
                     f"tema {settings['theme']}, cámara {'sí' if self.vision else 'no'}")
        return "[Ahora mismo (dato del sistema): " + " · ".join(parts) + "]"

    def _current_player(self):
        if self.video.playing():
            return self.video
        return self.web if self.web.active() else None

    async def watch(self, url: str) -> str | None:
        """Abre una web de vídeo en la ventana controlada. Devuelve el motivo si no se puede."""
        try:
            await asyncio.to_thread(self.video.stop)
            try:
                await asyncio.to_thread(self.spotify.pause)
            except Exception:
                pass
            await asyncio.to_thread(self.web.open, url)
        except RuntimeError as e:
            log.info("No abro %s: %s", url, e)
            await self.broadcast({"type": "error", "text": str(e)})
            return str(e)
        self.spawn(self._video_refresh(delay=4))
        self.spawn(self._watch_wall(url))
        return None

    async def _watch_wall(self, url: str):
        """Si la web pide cuenta (RTVE en directo, Netflix sin sesión…), se lo dice a Charlie: entrar lo hace él."""
        for _ in range(16):
            await asyncio.sleep(2)
            if self.web.wall:
                name = next((s["name"] for s in settings["video_services"]
                             if any(self.web.wall.endswith(d) for d in s["domains"])), "esa web")
                msg = f"{name} pide iniciar sesión para verlo. Entra tú en la ventana y luego me lo vuelves a pedir."
                await self.broadcast({"type": "error", "text": msg})
                await self._say(msg)
                return

    async def watch_find(self, service: str, query: str) -> tuple[bool, str]:
        """Busca «query» en ese servicio (video_sites) y pone lo primero. (ok, frase para decir)."""
        if video_sites.service_key(service) is None:   # cualquier otra web: se busca en la propia página
            url = site_url(service)
            if not url:
                return False, f"No sé cuál es la web de {service}; dime su dirección."
            self.spawn(self._search_web(url, service, query))
            return True, f"Busco «{query}» en {service} y lo pongo."
        try:
            results = await asyncio.to_thread(video_sites.find, service, query)
        except RuntimeError as e:
            return False, str(e)
        if not results:
            return False, f"No he encontrado «{query}» en {service}."
        best = results[0]
        log.info("Ver %r en %s → %s", query, service, best["url"])
        if best.get("id"):   # YouTube: con mpv, sin anuncios
            await self.play_video(best["id"], best["title"])
        else:
            reason = await self.watch(best["url"])
            if reason:
                return False, reason
        if best["kind"] == "búsqueda":   # Netflix, Prime, Disney+: se abre la búsqueda y se pone el primer resultado
            self.spawn(self._play_first(video_sites.service_key(service), service, query))
            return True, f"Busco «{query}» en {service} y lo pongo."
        day = f" del {best['date'][8:10]}/{best['date'][5:7]}" if best.get("date") else ""
        return True, f"Pongo «{best['title']}»{day}."

    async def _search_web(self, url: str, name: str, query: str):
        """Web sin receta: abre la portada, busca como una persona y pone el primer resultado que no sea anuncio."""
        reason = await self.watch(url)
        if reason:
            await self._say(reason)
            return
        found = await asyncio.to_thread(self.web.search_site, url, query)
        if not found and not self.web.wall:   # como mínimo, con el buscador («site:web búsqueda»)
            found = await asyncio.to_thread(self.web.search_engine, url, query)
        if found:
            log.info("Primer resultado de %r en %s → %s", query, name, found)
            await self.watch(found)
        elif not self.web.wall:
            msg = f"No he encontrado «{query}» en {name}. Te dejo la web abierta."
            await self.broadcast({"type": "error", "text": msg})
            await self._say(msg)

    async def _play_first(self, key: str, service: str, query: str):
        url = await asyncio.to_thread(self.web.pick_result, key)
        if url:
            log.info("Primer resultado de %r en %s → %s", query, service, url)
            await self.watch(url)
        elif not self.web.wall:
            msg = f"No he encontrado «{query}» en {service}. Te dejo la búsqueda abierta."
            await self.broadcast({"type": "error", "text": msg})
            await self._say(msg)

    async def play_video(self, video_id: str, title: str, rect: dict | None = None):
        try:   # que no se pisen la música y el vídeo
            await asyncio.to_thread(self.spotify.pause)
        except Exception:
            pass
        await asyncio.to_thread(self.web.close)
        await asyncio.to_thread(self.video.play, video_id, title, rect)
        self.spawn(self._video_refresh(delay=3))

    async def _video_refresh(self, delay: float = 0.3):
        await asyncio.sleep(delay)
        # en un hilo: mirar si hay web de vídeo es una petición HTTP que, sin vídeo, tarda ~1 s en fallar (Windows)
        player = await asyncio.to_thread(self._current_player)
        st = await asyncio.to_thread(player.status) if player else {"playing": False}
        if st != self._video_status:
            self._video_status = st
            await self.broadcast({"type": "video", **st})

    async def _on_barge(self, text: str, just_stop: bool):
        """Le has hablado mientras hablaba: se corta todo (la tarea también) y te atiende."""
        if self.state != "speaking" and not self.player.busy():
            return
        log.info("Interrumpido por voz: %r", text)
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
        if self.mic_mute.muted:
            await self.broadcast({"type": "error", "text": "El micro está silenciado en Windows. Actívalo desde Estado."})
        self._followup = False
        self.player.play(audio.chime(self.player.rate), self.player.rate, mutes_mic=False)
        self.listener.activate()
        await self.set_state("listening")

    async def _listen_followup(self, gen: int):
        """Tras responder, deja el micro abierto unos segundos para contestar sin decir «Pepito»."""
        if gen != self._gen or self.state != "idle" or self._enroll_left or not settings["followup_seconds"]:
            return
        self._followup = True
        # el micro sigue silenciado un instante tras hablar (eco): se cuenta desde que se abre
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
                                  "message": "No te he oído; grabación parada."})
        if self.state == "listening":
            await self.set_state("idle")

    # ---------- muestras de «Pepito» ----------
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
                                  "message": "¡Listo! Ahora reentreno el detector con tu voz (unos minutos)."})
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
        self._turn_t = time.monotonic()   # para medir cuánto tarda en empezar a hablar
        self._turn = self.spawn(self._guarded(coro))

    async def _guarded(self, coro):
        """Un fallo inesperado en un turno no deja el panel atascado en «pensando»."""
        try:
            await coro
        except Exception:
            log.exception("Error en el turno")
            await self.broadcast({"type": "error", "text": "Algo ha fallado; está en el registro."})
            await self.set_state("idle")

    # ---------- turnos ----------
    async def _voice_turn(self, clip: np.ndarray, followup: bool = False, spec=None):
        await self.set_state("transcribing")
        t = time.monotonic()
        text = None
        if spec is not None:   # ya se empezó a transcribir en la pausa (ver Listener)
            try:
                text, lang = await asyncio.wrap_future(spec)
                self.spawn(asyncio.to_thread(save_clip, normalize(clip), f"{lang}: {text}"))
            except Exception:
                log.exception("La transcripción adelantada falló; la repito")
                text = None
        if text is None:
            text, lang = await asyncio.to_thread(self.stt.transcribe_command, clip, self._speak_lang)
        log.info("Transcrito en %.2fs%s (%s): %s", time.monotonic() - t, " (adelantada)" if spec else "", lang, text)
        text = strip_wake(text)
        last = next((h["text"] for h in reversed(self.history) if h["role"] == "assistant"), "")
        asked = last.rstrip().endswith("?")
        if not text or (followup and not asked and is_ack(text)):
            # «ah vale», «okey»… después de una respuesta: no hace falta contestar
            # (salvo que Pepito acabara de preguntar algo: entonces «sí» o «vale» es la respuesta)
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
        local = route(text, lang) or (datos.route(text) if lang == "es" else None)
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
                ask += (f"\n\n[Correo {mid} (cuenta {m['account']}) · De: {m['from']} · Asunto: {m['subject']} · "
                        f"{m['date']}]\n{m['text']}\n[Fin del correo {mid}]")
            except Exception as e:
                log.warning("No pude leer el correo %s: %s", mid, e)
        if BANK_RE.search(text):
            try:
                extra = await asyncio.to_thread(bank_context, text)
            except Exception:
                log.exception("No pude leer el banco")
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
                # para | asunto | texto | cuenta (opcional: principal o secundaria)
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
            log.info("Claude pide música: %r → %s", command, intent)
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
                    if not str(e).startswith("No encuentro"):
                        raise
                    name = await asyncio.to_thread(self.spotify.play_query, value)   # no es playlist: canción
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
            # poner música: sin respuesta hablada, la música ya es la respuesta (en el chat sí queda escrito)
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
                log.exception("Error leyendo Spotify")
        await self.broadcast({"type": "spotify", **self.spotify.summary()})

    def _on_delta(self, chunk: str):
        """Va acumulando texto y manda a hablar cada frase completa en cuanto llega."""
        self._raw += chunk
        visible = ANY_TAG.sub("", self._raw)
        cut = visible.find("[[")   # etiqueta a medio llegar: se guarda hasta que se cierre
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
            self.spawn(self.broadcast({"type": "activity", "text": "Buscando en internet…"}))

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
                                log.info("Empieza a hablar a los %.2f s", time.monotonic() - self._turn_t)
                                self._turn_t = 0.0
                            if self.barge:
                                self.barge.said(clean)
                            self.player.play(pcm, rate)
                    finally:
                        await voice.aclose()   # cortada a medias: suelta ya la conexión de la voz
            except Exception:
                log.exception("Error sintetizando voz")
            finally:
                self._sentences.task_done()

    async def _monitor(self):
        """Silencia el micro mientras suena Pepito (con una cola para el eco del Bluetooth)."""
        while True:
            try:
                await self._monitor_loop()
            except Exception:
                # si este bucle muriera, el micro se quedaría silenciado para siempre
                log.exception("Error en el bucle de vigilancia; lo reanudo")
                await asyncio.sleep(1)

    async def _monitor_loop(self):
        last_status = last_maintain = time.monotonic()
        last_estado = last_aviso = last_vision = 0.0
        last_bank = -1e9
        last_timer = last_video = last_web_check = 0.0
        last_mail = last_wa = -1e9
        last_update_try = time.monotonic() - 540   # la primera, al minuto de arrancar
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
                    # se ha conectado o desconectado un altavoz (Bluetooth): reabrir para no hablarle al vacío
                    log.info("Cambian los altavoces (%s); reabro el audio", outs)
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
                # el saldo se renueva solo cada 6 h (lo decide la caché de bank.summary: ~4 consultas al día)
                last_bank = now
                self.spawn(asyncio.to_thread(bank.summary))
                self._maybe_nightly_retrain()
            t = time.monotonic()
            await asyncio.sleep(0.08)
            lag = time.monotonic() - t - 0.08
            if lag > 120:   # el portátil estuvo suspendido, no es un bloqueo; la red tarda en volver
                log.info("Vuelvo de una suspensión de %.0f min", lag / 60)
                last_update_try = time.monotonic()
                last_mail = time.monotonic() - 40   # el correo, a los 20 s
            elif lag > 0.5:   # algo bloqueó el bucle principal (una llamada lenta sin to_thread): que quede rastro
                log.warning("El bucle principal se ha quedado parado %.1f s", lag)

    # ---------- gestos (cámara) ----------
    async def _on_gesture(self, gesture: str, action: str):
        log.info("Gesto %s → %s", gesture, action)
        await self.broadcast({"type": "gesture", "gesture": gesture, "action": action})
        sp = self.spotify
        try:
            player = await asyncio.to_thread(self._current_player) \
                if action in ("next", "previous", "toggle", "volume_up", "volume_down") else None
            if player:   # con un vídeo puesto, los gestos mandan sobre el vídeo
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
                await asyncio.to_thread(sp.change_volume, 5 if action == "volume_up" else -5)   # pasos suaves: los gestos se repiten
            elif action == "pepito" and self.state == "idle":
                await self._listen()
            elif action == "stop":
                await self.interrupt()
            elif action == "quiet":
                # se calla pero no corta lo que está haciendo (poner música, buscar algo…)
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
            log.exception("Error con el gesto %s", gesture)
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

    # ---------- reentrenar «Pepito» ----------
    def _maybe_nightly_retrain(self):
        t = datetime.now()
        if t.hour != 4 or self._retrained_day == t.date() or self.state != "idle":
            return
        self._retrained_day = t.date()
        model = MODELS_DIR / "pepito_wake.npz"
        real = list((MODELS_DIR / "wake_train").glob("*_real/*.wav"))
        if model.exists() and real and max(f.stat().st_mtime for f in real) > model.stat().st_mtime:
            self.spawn(self.retrain_wake())

    async def retrain_wake(self):
        """Reentrena el detector con lo último (incluidas activaciones reales) en segundo plano, con prioridad baja."""
        if self._retraining:
            return
        self._retraining = True
        await self.broadcast({"type": "wake_train", "running": True})
        log.info("Reentrenando el detector de «Pepito»")
        try:
            proc = await asyncio.create_subprocess_exec(
                str(ROOT / ".venv" / "Scripts" / "python.exe"), str(ROOT / "tools" / "train_wake.py"),
                cwd=str(ROOT), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                creationflags=0x00004000 | 0x08000000)   # BELOW_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW
            out, _ = await proc.communicate()
            (ROOT / "logs" / "train_wake.log").write_bytes(out)
            ok = proc.returncode == 0
            if ok:
                self.listener.reload_detector()
            summary = [l for l in out.decode("utf-8", "replace").splitlines() if l.startswith("umbral 0.85")]
            log.info("Reentrenado (%s): %s", "ok" if ok else "fallo", summary)
            await self.broadcast({"type": "wake_train", "running": False, "ok": ok,
                                  "summary": summary[0] if summary else ""})
        finally:
            self._retraining = False

    # ---------- avisos ----------
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
        log.info("Temporizador terminado: %s", label or timers.say_duration(t["seconds"]))
        await self.broadcast({"type": "timer_done", "timer": t})
        await self.broadcast({"type": "timers", "timers": timers.active()})
        for _ in range(3):   # que se oiga aunque haya música
            self.player.play(audio.chime(self.player.rate), self.player.rate, mutes_mic=False)
            await asyncio.sleep(0.5)
        self._speak_lang = "es"
        await self._say(f"¡Tiempo! Ya está {label}." if label else "¡Tiempo! Se acabó el temporizador.")

    async def _check_avisos(self):
        try:
            fired = await asyncio.to_thread(avisos.due, datetime.now())
        except Exception:
            log.exception("Error mirando los avisos")
            return
        for a in fired:
            # espera a que Pepito acabe lo que esté haciendo (máx. 2 min) para no pisarle
            for _ in range(240):
                if self.state == "idle":
                    break
                await asyncio.sleep(0.5)
            log.info("Aviso: %s", a.get("texto"))
            await self.broadcast({"type": "aviso", "texto": a.get("texto", "")})
            await self._add("assistant", f"Aviso: {a.get('texto', '')}", local=True)
            self.player.play(audio.chime(self.player.rate), self.player.rate, mutes_mic=False)
            self._speak_lang = "es"
            await self._say(f"Oye, te recuerdo: {a.get('texto', '')}.")
            await self._wait_speech()
        if fired:
            await self.broadcast_avisos()

    async def _refresh_data(self):
        """Regenera estado.md si cambió algo y avisa a los widgets."""
        try:
            changed = await asyncio.to_thread(self.estado.refresh)
            if changed and self.clients:
                await self.broadcast({"type": "data", "data": await asyncio.to_thread(datos.summary)})
        except Exception:
            log.exception("Error actualizando estado.md")


def keep_awake():
    """Que el portátil no se duerma ni apague la pantalla mientras Pepito esté vivo (se deshace al salir)."""
    import ctypes
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x00000001, 0x00000002
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
    except Exception:
        log.warning("No pude evitar que el portátil se duerma")


pepito = Pepito()


@asynccontextmanager
def _quiet_resets(loop, context):
    """Windows avisa con un traceback cada vez que el navegador corta una conexión (WinError 10054): es normal."""
    if isinstance(context.get("exception"), ConnectionResetError) and "connection_lost" in str(context.get("handle", "")):
        return
    loop.default_exception_handler(context)


async def lifespan(_app):
    await pepito.start()
    yield
    pepito.brain.stop()
    pepito.wa.stop()
    if pepito.vision:
        pepito.vision.stop()
    pepito.player.close()
    pepito.mic.close()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.middleware("http")
async def same_origin_only(request, call_next):
    """Las órdenes (POST…) solo desde el panel: el navegador manda «Origin» en las peticiones entre webs,
    y sin esto una web cualquiera podría lanzar, a ciegas, reentrenos, actualizaciones…"""
    origin = request.headers.get("origin")
    local = not origin or urlparse(origin).hostname in ("127.0.0.1", "localhost")
    if request.method not in ("GET", "HEAD", "OPTIONS") and not local:
        log.warning("Petición %s %s rechazada desde %s", request.method, request.url.path, origin)
        return Response(status_code=403)
    return await call_next(request)


@app.middleware("http")
async def no_cache(request, call_next):
    """Que Edge no se quede con una versión vieja del panel."""
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
        await asyncio.to_thread(pepito.open_devices)
    if "stt_model" in changed:
        await asyncio.to_thread(pepito.stt.load, settings["stt_model"])
    if "data_dir" in changed:
        await asyncio.to_thread(pepito.brain.start)
        await pepito._refresh_data()
    if "claude_model" in changed and pepito.state == "idle":
        await asyncio.to_thread(pepito.brain.maintain)
    await pepito.broadcast(pepito.status())
    await pepito.broadcast({"type": "settings", "settings": settings.all()})
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
    """El mismo icono que el de la bandeja (lo crea tools/tray.py)."""
    ico = ROOT / "logs" / "pepito.ico"
    return FileResponse(ico, media_type="image/x-icon") if ico.exists() else Response(status_code=204)


@app.get("/api/mail")
async def get_mail():
    return pepito.mail.summary()


@app.get("/api/whatsapp")
async def get_whatsapp():
    return pepito.wa.summary()


@app.get("/api/whatsapp/chat")
async def whatsapp_chat(jid: str):
    return await asyncio.to_thread(pepito.wa.messages, jid)


@app.get("/api/whatsapp/media")
async def whatsapp_media(jid: str, id: str):
    try:
        path = await asyncio.to_thread(pepito.wa.media_file, jid, id)
    except Exception as e:
        gone = any(k in str(e) for k in ("403", "404", "410", "incomplete media"))
        log.info("WhatsApp: no pude bajar %s: %s", id, e)
        return Response("WhatsApp ya no lo tiene" if gone else "No se pudo descargar", status_code=410 if gone else 502)
    kind = {".ogg": "audio/ogg", ".opus": "audio/ogg", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".mp4": "video/mp4"}.get(path.suffix.lower())
    return FileResponse(path, media_type=kind, headers={"Cache-Control": "private, max-age=86400"})


@app.post("/api/whatsapp/mote")
async def whatsapp_mote(body: dict):
    """Mote de un contacto (vacío = quitarlo): Pepito lo dice así en avisos, panel y respuestas."""
    await asyncio.to_thread(pepito.wa.set_mote, str(body.get("name", "")), str(body.get("mote", "")))
    await pepito.check_whatsapp()
    return {"ok": True}


@app.post("/api/whatsapp/check")
async def whatsapp_check():
    """Botón «Recargar» del panel: mira ya si hay mensajes nuevos."""
    await pepito.check_whatsapp()
    return pepito.wa.summary()


@app.post("/api/whatsapp/mute")
async def whatsapp_mute(body: dict):
    """Silenciar o no un chat (que avise o no). Los grupos vienen silenciados."""
    await asyncio.to_thread(pepito.wa.set_muted, str(body.get("jid", "")), str(body.get("name", "")), bool(body.get("muted")))
    await pepito.broadcast({"type": "whatsapp", **pepito.wa.summary()})
    return {"ok": True}


@app.get("/api/whatsapp/qr.svg")
async def whatsapp_qr():
    """El QR para vincular WhatsApp (Ajustes → Dispositivos vinculados), como imagen para el panel."""
    import io
    import qrcode
    import qrcode.image.svg
    code = pepito.wa.qr
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
        return await asyncio.to_thread(pepito.mail.open, id)
    except Exception as e:
        log.info("Correo: no pude abrir %s: %s", id, e)
        return Response(f"No pude abrir el correo: {e}", status_code=502)


@app.get("/api/mail/view")
async def mail_view(id: str):
    """El cuerpo del correo para el iframe del panel: sin scripts ni formularios (CSP + sandbox)."""
    if not MAIL_ID.match(id):
        return Response(status_code=400)
    try:
        page = await asyncio.to_thread(pepito.mail.html, id)
    except Exception as e:
        page = f"<p style='font:15px system-ui;color:#b91c1c'>No pude abrir el correo: {e}</p>"
    return HTMLResponse(page, headers={"Content-Security-Policy": MAIL_CSP, "Cache-Control": "no-store"})


@app.get("/api/mail/part")
async def mail_part(id: str, n: int, dl: int = 0):
    if not MAIL_ID.match(id):
        return Response(status_code=400)
    try:
        data, kind, name = await asyncio.to_thread(pepito.mail.part, id, n)
    except Exception:
        return Response(status_code=404)
    from urllib.parse import quote
    how = "attachment" if dl else "inline"
    # lo que el navegador ejecutaría (html, svg…) se baja siempre, no se abre aquí
    if not dl and not kind.startswith(("image/", "audio/", "video/")) and kind != "application/pdf" or kind == "image/svg+xml":
        how = "attachment"
    return Response(data, media_type=kind, headers={"Content-Disposition": f"{how}; filename*=UTF-8''{quote(name)}",
                                                   "X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"})


@app.post("/api/mail/send")
async def send_mail(body: dict):
    """Enviar (to, subject, text, account) o responder (reply_to = ID, text) desde el panel."""
    try:
        if body.get("reply_to"):
            said = await asyncio.to_thread(pepito.mail.reply, str(body["reply_to"]), str(body.get("text", "")))
        else:
            said = await asyncio.to_thread(pepito.mail.send, str(body["to"]), str(body.get("subject", "")),
                                           str(body.get("text", "")), body.get("account"))
    except Exception as e:
        return {"ok": False, "error": str(e)}
    pepito.spawn(pepito.check_mail())
    return {"ok": True, "message": said}


@app.post("/api/mail/login")
async def mail_login(body: dict):
    """Conectar una cuenta desde el panel: Charlie pega su contraseña de aplicación y se prueba en el momento."""
    err = await asyncio.to_thread(pepito.mail.connect, str(body.get("slot", "")), str(body.get("user", "")),
                                  str(body.get("password", "")))
    if err:
        return {"ok": False, "error": err}
    await pepito.check_mail()
    return {"ok": True, **pepito.mail.summary()}


@app.post("/api/mail/apppasswords")
async def mail_apppasswords(body: dict):
    """Desactivado en Cortex: no abre páginas de inicio de sesión. La contraseña de aplicación se crea a mano."""
    return {"ok": False, "error": "Crea la contraseña de aplicación a mano en tu cuenta de Google."}


@app.post("/api/mail/check")
async def mail_check():
    await pepito.check_mail()
    return pepito.mail.summary()


@app.get("/api/notices")
async def get_notices():
    return {"notices": pepito.notices, "quiet": settings["quiet_mode"]}


@app.post("/api/quiet")
async def set_quiet(body: dict):
    settings.update({"quiet_mode": bool(body.get("on"))})
    await pepito.broadcast({"type": "settings", "settings": settings.all()})
    return {"quiet": settings["quiet_mode"]}


@app.post("/api/update")
async def run_update():
    pepito.spawn(pepito.auto_update())
    return {"ok": True}


@app.get("/api/devices")
async def get_devices():
    """Aparatos de casa: los de Spotify Connect (se les puede mandar la música) y los vistos en la red."""
    sp = pepito.spotify
    music = []
    if sp.connected:
        try:
            for d in await asyncio.to_thread(sp.devices):
                music.append({"id": d["id"], "name": "Este portátil" if sp._is_laptop(d["name"]) else d["name"],
                              "type": d.get("type", ""), "active": bool(d.get("is_active")),
                              "volume": d.get("volume_percent")})
        except SpotifyError:
            pass
    return {"music": music, "lan": pepito.devices.list() if pepito.devices else [],
            "scanned_at": pepito.devices.scanned_at if pepito.devices else 0}


@app.post("/api/devices/music")
async def music_to_device(body: dict):
    """Pasa la música de Spotify a ese aparato (por id de Spotify Connect)."""
    sp = pepito.spotify
    try:
        await asyncio.to_thread(sp.move_to, str(body["id"]))
    except (SpotifyError, KeyError) as e:
        return {"ok": False, "error": str(e) or "Falta el aparato"}
    pepito.spawn(pepito._spotify_refresh(delay=1))
    return {"ok": True}


@app.post("/api/devices/scan")
async def scan_devices():
    if pepito.devices:
        await asyncio.to_thread(pepito.devices.scan)
    return await get_devices()


@app.get("/api/video")
async def video_status():
    player = await asyncio.to_thread(pepito._current_player)
    return await asyncio.to_thread(player.status) if player else {"playing": False}


@app.get("/api/video/services")
async def video_services():
    return {"services": [{"name": s["name"], "url": s["url"]} for s in settings["video_services"] if s.get("enabled", True)]}


@app.get("/api/web/accounts")
async def web_accounts():
    return webaccounts.accounts()   # sin contraseñas


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
            await pepito.play_video(str(body["id"]), str(body.get("title", ""))[:200], body.get("rect"))
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}
    elif action == "rect" and isinstance(body.get("rect"), dict):
        await asyncio.to_thread(pepito.video.set_rect, body["rect"])
        if pepito.web.docked and await asyncio.to_thread(pepito.web.active):
            await asyncio.to_thread(pepito.web.dock, body["rect"])
        else:
            pepito.web.rect = body["rect"]
        return {"ok": True}
    elif action == "dock":
        await asyncio.to_thread(pepito.web.dock, body.get("rect") if isinstance(body.get("rect"), dict) else None)
    elif action == "undock":
        await asyncio.to_thread(pepito.web.undock)
    elif action == "hide":
        await asyncio.to_thread(pepito.web.hide)
        return {"ok": True}
    elif action == "open" and body.get("url"):
        if isinstance(body.get("rect"), dict):
            pepito.web.rect = body["rect"]
        reason = await pepito.watch(str(body["url"]))
        if reason:
            return {"ok": False, "error": reason}
    elif action == "find" and body.get("service") and body.get("query"):   # buscar en un servicio o en cualquier web
        ok, said = await pepito.watch_find(str(body["service"]), str(body["query"]))
        return {"ok": ok, "text": said}
    elif action in ("toggle", "seek", "volume", "fullscreen", "stop", "pause", "resume"):
        player = await asyncio.to_thread(pepito._current_player)
        if player:
            await asyncio.to_thread(player.command, action, body.get("value"))
    pepito.spawn(pepito._video_refresh())
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
            return {"ok": False, "error": "Duración no válida"}
        timers.add(seconds, str(body.get("label", ""))[:40])
    await pepito.broadcast({"type": "timers", "timers": timers.active()})
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
        return {"ok": False, "error": f"No pude guardar la lista: {e}"}
    await pepito.broadcast({"type": "compra", "items": items})
    return {"ok": True, "items": items}


@app.post("/api/tasks")
async def edit_task(body: dict):
    """Tareas a mano desde el panel: add, edit, done o delete (por su posición «i» en tareas.yaml). Sin confirmación."""
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
                return "Escribe la tarea"
            item = {"tarea": Q(text), "prioridad": fields.get("prioridad") or "media", "estado": "pendiente"}
            if fields.get("cuando"):
                item["cuando"] = Q(fields["cuando"])
            pend.insert(0, item)   # arriba (y así no se descoloca el hueco antes de «recurrentes»)
        else:
            i = body.get("i")
            if not isinstance(i, int) or not 0 <= i < len(pend):
                return "No encuentro esa tarea"
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
                return "Acción desconocida"
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
    log.info("Tarea desde el panel: %s", action)
    await pepito._refresh_data()
    return {"ok": True, "data": await asyncio.to_thread(datos.summary)}


@app.get("/api/bank")
async def get_bank():
    """Saldo de Revolut para el panel (de la caché: el banco solo deja unas 4 consultas al día sin ti delante)."""
    data = await asyncio.to_thread(bank.summary)
    if not data.get("accounts"):
        return {"ok": False}
    return {"ok": True, "consent_days": data.get("consent_days"), "fetched_at": data.get("fetched_at"),
            "stale": bool(data.get("stale")),
            "accounts": [{"masked": a.get("masked"), "balance": a.get("balance"), "spending": a.get("spending"),
                          "recent": (a.get("transactions") or [])[:12]} for a in data["accounts"]]}


# archivos de datos que se pueden editar desde el panel (los avisos tienen su propio formulario)
EDITABLE = {"tareas": "tareas.yaml", "comidas": "comidas.yaml", "gym": "gym.yaml", "dinero": "dinero.yaml",
            "fechas": "fechas.yaml", "betis": "betis.yaml", "horario": "horario.yaml", "compra": "compra.md"}


@app.get("/api/file/{name}")
async def read_file(name: str):
    if name not in EDITABLE:
        return {"ok": False, "error": "Ese archivo no se puede editar"}
    path = datos.folder() / EDITABLE[name]
    text = await asyncio.to_thread(path.read_text, encoding="utf-8") if path.exists() else ""
    return {"ok": True, "name": name, "file": EDITABLE[name], "text": text}


@app.post("/api/file/{name}")
async def write_file(name: str, body: dict):
    if name not in EDITABLE:
        return {"ok": False, "error": "Ese archivo no se puede editar"}
    text = str(body.get("text", ""))
    file = EDITABLE[name]
    if file.endswith(".yaml"):
        import yaml
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            where = f" (línea {mark.line + 1})" if mark else ""
            return {"ok": False, "error": f"El formato no es válido{where}: {getattr(e, 'problem', None) or e}"}
        if data is not None and not isinstance(data, dict):
            return {"ok": False, "error": "El archivo debe tener la forma «clave: valor»"}
    path = datos.folder() / file
    # copia de la versión anterior por si acaso (las 20 últimas, en la carpeta de código)
    backup = ROOT / "logs" / "backups"
    backup.mkdir(parents=True, exist_ok=True)
    if path.exists():
        (backup / f"{path.stem}-{time.strftime('%Y%m%d-%H%M%S')}{path.suffix}").write_text(
            path.read_text(encoding="utf-8"), encoding="utf-8")
        for old in sorted(backup.glob(f"{path.stem}-*"))[:-20]:
            old.unlink(missing_ok=True)
    await asyncio.to_thread(path.write_text, text, encoding="utf-8")
    log.info("Editado desde el panel: %s", file)
    pepito.spawn(pepito._refresh_data())
    return {"ok": True}


@app.get("/api/camera.mjpg")
async def camera_stream():
    """Vídeo de la cámara con lo que detecta (solo para probar): MJPEG mientras alguien lo mire."""
    from fastapi.responses import StreamingResponse
    vision = pepito.vision

    async def frames():
        vision.preview_clients += 1
        try:
            last = None
            while True:
                jpg = vision.frame_jpeg() if vision else None
                if jpg and jpg is not last:
                    last = jpg
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
                await asyncio.sleep(0.06)
        finally:
            vision.preview_clients = max(0, vision.preview_clients - 1)

    return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store"})


@app.post("/api/gestures/record")
async def record_gesture(body: dict):
    name = str(body.get("name", "")).strip()
    if not pepito.vision or not await asyncio.to_thread(pepito.vision.record_custom, name):
        return {"error": "No puedo grabar ahora: ¿cámara activada y nombre sin «:»?"}
    return {"ok": True}


@app.post("/api/gestures/delete")
async def delete_gesture(body: dict):
    ok = bool(pepito.vision and await asyncio.to_thread(pepito.vision.delete_custom, str(body.get("name", ""))))
    await pepito._send_vision(force=True)
    return {"ok": ok}


@app.post("/api/wake/retrain")
async def wake_retrain():
    pepito.spawn(pepito.retrain_wake())
    return {"ok": True}


@app.get("/api/avisos")
async def get_avisos():
    return await asyncio.to_thread(avisos.upcoming, datetime.now())


@app.post("/api/avisos")
async def add_aviso(body: dict):
    when = avisos.parse_when(body.get("cuando"))
    if not when or not str(body.get("texto", "")).strip():
        return {"error": "Falta la fecha o el texto del aviso."}
    try:
        await asyncio.to_thread(avisos.add, when, str(body["texto"]), str(body.get("repetir", "")))
    except ValueError as e:
        return {"error": f"No lo guardo: {e}."}
    await pepito.broadcast_avisos()
    return {"ok": True}


@app.post("/api/avisos/delete")
async def delete_aviso(body: dict):
    ok = await asyncio.to_thread(avisos.delete, str(body.get("id", "")))
    await pepito.broadcast_avisos()
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
        return {"error": "No llego al buscador de ciudades, ¿hay internet?"}
    if not found:
        return {"error": f"No encuentro «{name}»."}
    settings.update({"weather_place": {"name": found["name"], "lat": found["latitude"], "lon": found["longitude"]}})
    await pepito.broadcast({"type": "settings", "settings": settings.all()})
    return await asyncio.to_thread(weather.forecast)


@app.post("/api/voice/test")
async def test_voice(body: dict):
    lang = body.get("lang", "es")
    text = {"es": "¡Hola! Soy Pepito. Así sueno ahora mismo.", "en": "Hi! I'm Pepito. This is how I sound."}[lang]
    pepito._speak_lang = lang
    pepito._queue_sentence(text)
    return {"ok": True}


def _page(message: str) -> HTMLResponse:
    return HTMLResponse(f"<p style='font:18px system-ui;padding:40px'>{message} <a href='/'>Volver</a></p>")


@app.get("/spotify/login")
async def spotify_login():
    try:
        return RedirectResponse(pepito.spotify.login_url())
    except SpotifyError as e:
        return _page(f"{e}.")


@app.get("/spotify/callback")
async def spotify_callback(code: str = "", state: str = "", error: str = ""):
    if error:
        return _page(f"Spotify dijo: {error}.")
    try:
        await asyncio.to_thread(pepito.spotify.finish_login, code, state)
    except SpotifyError as e:
        return _page(f"{e}.")
    pepito.spawn(pepito._spotify_refresh())
    return RedirectResponse("/")


@app.post("/api/spotify")
async def spotify_command(body: dict):
    sp = pepito.spotify
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
    pepito.spawn(pepito._spotify_refresh(delay=0.4))
    return {"ok": True}


@app.get("/api/layout")
async def get_layout():
    return json.loads(LAYOUT_FILE.read_text(encoding="utf-8")) if LAYOUT_FILE.exists() else []


@app.post("/api/layout")
async def save_layout(items: list[dict]):
    # un panel con una versión vieja no conoce los widgets nuevos: los que no mande se conservan
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
    await asyncio.to_thread(pepito.open_devices)
    await pepito.broadcast(pepito.status())
    return await get_settings()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    # Los websockets no tienen la protección CORS: sin esto, cualquier web abierta en el navegador podría
    # conectarse, leer la conversación y darle órdenes (mandar correos…). Solo el panel (o scripts locales sin Origin).
    origin = ws.headers.get("origin")
    if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
        log.warning("Websocket rechazado desde %s", origin)
        await ws.close(code=1008)
        return
    await ws.accept()
    pepito.clients.add(ws)
    await ws.send_json({"type": "hello", "state": pepito.state, "history": pepito.history,
                        "settings": settings.all(), "web": web_version()})
    await ws.send_json(pepito.status())
    await ws.send_json({"type": "spotify", **pepito.spotify.summary()})
    await ws.send_json({"type": "whatsapp", **pepito.wa.summary()})
    await ws.send_json({"type": "mail", **pepito.mail.summary()})
    if pepito.vision:
        await ws.send_json({"type": "vision", **pepito.vision.state()})
    try:
        while True:
            msg = await ws.receive_json()
            action = msg.get("action")
            if action == "press":
                await pepito.press()
            elif action == "stop":
                await pepito.interrupt()
            elif action == "text" and msg.get("text", "").strip():
                await pepito.send_text(msg["text"])
            elif action == "enroll":
                await pepito.enroll()
            elif action == "enroll_clear":
                await asyncio.to_thread(pepito.listener.print.clear_samples)
                await pepito.broadcast(pepito.status())
            elif action == "unmute_mic":
                pepito.mic_mute.unmute()
                await asyncio.sleep(0.5)
                await pepito.broadcast(pepito.status())
            elif action == "wake_toggle":
                settings.update({"wake_enabled": not settings["wake_enabled"]})
                await pepito.broadcast(pepito.status())
            elif action == "new_conversation":
                await pepito.interrupt()
                await asyncio.to_thread(pepito.brain.new_conversation)
                pepito.history = []
                await pepito.broadcast({"type": "cleared"})
                await pepito.broadcast(pepito.status())
    except WebSocketDisconnect:
        pass
    finally:
        pepito.clients.discard(ws)
