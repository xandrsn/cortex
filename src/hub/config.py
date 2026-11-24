"""Paths and persistent Cortex settings (config.yaml in the code folder, no secrets)."""

import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]   # exe: next to the .exe
WEB_DIR = ROOT / "web" / "hub"
MODELS_DIR = ROOT / "models"
LOGS_DIR = ROOT / "logs"
CONFIG_FILE = ROOT / "config.yaml"

load_dotenv(ROOT / ".env")   # secrets (Spotify...): only in the code folder, never in Drive

DEFAULTS = {
    "user_name": "",            # what Cortex calls the user in texts; empty = "the user"
    "data_dir": str(ROOT / "datos-local"),
    "claude_model": "haiku",
    "session_idle_minutes": 30,
    "mic_device": None,          # device name; None = Windows default
    "speaker_device": None,
    "stt_model": "parakeet",
    "wake_enabled": True,
    "wake_sensitivity": 2,       # 1 low · 2 medium · 3 high
    "followup_seconds": 6,       # mic stays open after replying (0 = no)
    "tts_engine": "edge",        # edge (with Piper as fallback) | piper
    "tts_rate": 15,              # % speed over normal
    "barge_in": True,
    "quiet_mode": False,          # quiet mode: mail and news notices are not spoken aloud
    "auto_update_hours": 6,       # automatic update (Betis, tasks...) if Claude plan allows; 0 = never
    # video sites Cortex can open and control (the user signs in once in that window)
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
    # sites that are never opened even if enabled above (filled in by the user; empty by default)
    "blocked_sites": [],
    "voice_es": "es-ES-XimenaNeural",
    "voice_en": "en-US-AvaMultilingualNeural",
    "piper_es": "es_ES-davefx-medium",
    "piper_en": "en_US-lessac-medium",
    "notice_language": "en",
    "theme": "light",            # light | dark | auto
    "spotify_device": "",        # "" = this laptop (desktop Spotify); otherwise the Connect device name
    "ui_port": 8765,
    # First run: the installer writes the choices to config.yaml and the dashboard wizard finishes setup
    "setup_done": False,
    "setup_widgets": [],             # cards chosen at install time ([] = defaults)
    "setup_accounts": ["claude", "spotify", "email", "whatsapp"],
    "whatsapp_mode": "read",     # read (read only) | write (read and write from the dashboard)
    # Dashboard
    "accent": "#0b8f55",
    "text_scale": 1,
    "ambient_minutes": 10,           # big clock after X min of inactivity (0 = never)
    "weather_place": {"name": "Madrid", "lat": 40.4168, "lon": -3.7038},
    # Notices: quiet hours (HH:MM; empty = none). Anything due inside waits until they end.
    "quiet_start": "",
    "quiet_end": "",
    # Gestures: handled by Cortex Sense (another program). These four are shown and changed from the Hub dashboard
    # but stored in Sense; the values here only show when Sense is not running.
    "sense_url": "http://127.0.0.1:8766",
    "gestures_enabled": False,
    "gestures_require_look": True,
    "window_gestures": False,
    "gesture_actions": {},
}


SENSE_KEYS = ("gestures_enabled", "gestures_require_look", "window_gestures", "gesture_actions")


class Settings:
    def __init__(self):
        self.sense = None   # SenseLink, set by app.py: SENSE_KEYS settings are read from and written to Cortex Sense
        self._lock = threading.Lock()
        self._data = dict(DEFAULTS)
        if CONFIG_FILE.exists():
            saved = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
            for key, value in saved.items():
                # dicts (e.g. gesture_actions) are merged so new keys show up
                if isinstance(value, dict) and isinstance(DEFAULTS.get(key), dict):
                    value = {**DEFAULTS[key], **value}
                self._data[key] = value
        if os.environ.get("CORTEX_USER_NAME"):   # the environment / .env overrides config.yaml
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
                    logging.getLogger("hub").warning("Sense did not accept %s: %s", remote, err)
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
