"""Rutas y ajustes persistentes de Cortex (config.yaml en la carpeta de código, sin secretos)."""

import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]   # exe: junto al .exe
WEB_DIR = ROOT / "web" / "hub"
MODELS_DIR = ROOT / "models"
LOGS_DIR = ROOT / "logs"
CONFIG_FILE = ROOT / "config.yaml"

load_dotenv(ROOT / ".env")   # secretos (Spotify…): solo en la carpeta de código, nunca en Drive

DEFAULTS = {
    "user_name": "",            # cómo llama Cortex al usuario en los textos; vacío = "el usuario"
    "data_dir": str(ROOT / "datos-local"),
    "claude_model": "haiku",
    "session_idle_minutes": 30,
    "mic_device": None,          # nombre del dispositivo; None = predeterminado de Windows
    "speaker_device": None,
    "stt_model": "parakeet",
    "wake_enabled": True,
    "wake_sensitivity": 2,       # 1 baja · 2 media · 3 alta
    "followup_seconds": 6,       # micro abierto tras responder (0 = no)
    "tts_engine": "edge",        # edge (con Piper de respaldo) | piper
    "tts_rate": 15,              # % de velocidad sobre la normal
    "barge_in": True,
    "quiet_mode": False,          # modo silencio: los avisos de correo y novedades no se dicen en voz alta
    "auto_update_hours": 6,       # actualización automática (Betis, tareas…) si queda plan de Claude; 0 = nunca
    # webs de vídeo que Cortex puede abrir y controlar (Charlie inicia sesión una vez en esa ventana)
    "video_services": [
        {"name": "YouTube", "url": "https://www.youtube.com", "domains": ["youtube.com", "youtu.be"], "enabled": True},
        {"name": "RTVE Play", "url": "https://www.rtve.es/play/", "domains": ["rtve.es"], "enabled": True},
        {"name": "Pluto TV", "url": "https://pluto.tv/es/", "domains": ["pluto.tv"], "enabled": True},
        {"name": "Atresplayer", "url": "https://www.atresplayer.com", "domains": ["atresplayer.com"], "enabled": True},
        {"name": "Netflix", "url": "https://www.netflix.com/browse", "domains": ["netflix.com"], "enabled": True},
        {"name": "Prime Video", "url": "https://www.primevideo.com", "domains": ["primevideo.com", "amazon.es"],
         "enabled": True},
        {"name": "Disney+", "url": "https://www.disneyplus.com", "domains": ["disneyplus.com"], "enabled": True},
        {"name": "Movistar Plus+", "url": "https://ver.movistarplus.es", "domains": ["movistarplus.es"], "enabled": False},
    ],
    # webs que nunca se abren aunque se activen arriba (la rellena Charlie si quiere; vacía de serie)
    "blocked_sites": [],
    "voice_es": "es-ES-XimenaNeural",
    "voice_en": "en-US-AvaMultilingualNeural",
    "piper_es": "es_ES-davefx-medium",
    "piper_en": "en_US-lessac-medium",
    "notice_language": "es",
    "theme": "light",            # light | dark | auto
    "spotify_device": "",        # "" = este portátil (Spotify de escritorio); si no, nombre del dispositivo Connect
    "ui_port": 8765,
    # Primera vez: el instalador deja en config.yaml lo que se eligió y el asistente del panel lo termina
    "setup_done": False,
    "setup_widgets": [],             # tarjetas elegidas al instalar ([] = las de serie)
    "setup_accounts": ["claude", "spotify", "email", "whatsapp"],
    "whatsapp_mode": "read",     # read (solo leer) | write (leer y escribir desde el panel)
    # Panel
    "accent": "#0b8f55",
    "text_scale": 1,
    "ambient_minutes": 10,           # reloj grande tras X min sin usarlo (0 = nunca)
    "weather_place": {"name": "Madrid", "lat": 40.4168, "lon": -3.7038},
    # Avisos: horas de silencio (HH:MM; vacío = sin silencio). Lo que toque dentro espera al final.
    "quiet_start": "",
    "quiet_end": "",
    # Gestos: los lleva Cortex Sense (otro programa). Estos cuatro se ven y se cambian desde el panel de Hub,
    # pero se guardan en Sense; los valores de aquí solo se ven si Sense no está en marcha.
    "sense_url": "http://127.0.0.1:8766",
    "gestures_enabled": False,
    "gestures_require_look": True,
    "window_gestures": False,
    "gesture_actions": {},
}


SENSE_KEYS = ("gestures_enabled", "gestures_require_look", "window_gestures", "gesture_actions")


class Settings:
    def __init__(self):
        self.sense = None   # SenseLink, lo pone app.py: los ajustes de SENSE_KEYS van y vienen de Cortex Sense
        self._lock = threading.Lock()
        self._data = dict(DEFAULTS)
        if CONFIG_FILE.exists():
            saved = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
            for key, value in saved.items():
                # los diccionarios (p. ej. gesture_actions) se mezclan: así aparecen las claves nuevas
                if isinstance(value, dict) and isinstance(DEFAULTS.get(key), dict):
                    value = {**DEFAULTS[key], **value}
                self._data[key] = value
        if os.environ.get("CORTEX_USER_NAME"):   # el entorno / .env manda sobre config.yaml
            self._data["user_name"] = os.environ["CORTEX_USER_NAME"]

    def _remote(self) -> dict:
        live = self.sense.settings if self.sense and self.sense.connected else {}
        return {k: live[k] for k in SENSE_KEYS if k in live}

    def __getitem__(self, key):
        if key in SENSE_KEYS:
            return self._remote().get(key, self._data[key])
        return self._data[key]

    def all(self) -> dict:
        return {**self._data, **self._remote()}

    def update(self, changes: dict) -> None:
        remote = {k: v for k, v in changes.items() if k in SENSE_KEYS}
        if remote:
            if self.sense and self.sense.connected:
                if err := self.sense.update_settings(remote):
                    logging.getLogger("hub").warning("Sense no aceptó %s: %s", remote, err)
            changes = {k: v for k, v in changes.items() if k not in SENSE_KEYS}
            if not changes:
                return
        with self._lock:
            self._data.update({k: v for k, v in changes.items() if k in DEFAULTS})
            CONFIG_FILE.write_text(yaml.safe_dump(self._data, allow_unicode=True, sort_keys=False), encoding="utf-8")


settings = Settings()


def setup_logging() -> None:
    LOGS_DIR.mkdir(exist_ok=True)
    handler = RotatingFileHandler(LOGS_DIR / "cortex.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[handler, console])
    logging.getLogger("httpx").setLevel(logging.WARNING)
