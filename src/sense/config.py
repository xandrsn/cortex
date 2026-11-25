"""Cortex Sense settings: sense.yaml next to the code (or next to the .exe)."""

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

import yaml

ROOT = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
WEB_DIR = ROOT / "web" / "sense"
MODELS_DIR = ROOT / "models"
LOGS_DIR = ROOT / "logs"
CONFIG_FILE = ROOT / "sense.yaml"

# Hub actions: if Hub is connected it runs them; otherwise Sense uses the equivalent media key.
HUB_ACTIONS = ["next", "previous", "toggle", "volume_up", "volume_down", "listen", "stop", "quiet",
               "ambient", "wake_toggle", "new_chat", "theme"]

DEFAULTS = {
    "ui_port": 8766,
    "setup_done": False,             # the setup wizard (camera + calibration) has been completed
    "camera_index": 0,
    "gestures_enabled": True,
    "window_gestures": True,         # move windows with a pinch (pygame overlay; on restart)
    "gestures_require_look": True,   # gestures only count while looking at the screen
    "look_max_angle": 22,            # max yaw/pitch degrees to count as "looking"
    "look_pitch_offset": 0,          # degrees: pitch centre (+ = down); set by calibration
    "gesture_min_score": 0.6,
    "gesture_hold_seconds": 0.35,
    "gesture_cooldown": 1.0,
    "swipe_min_distance": 0.22,      # fraction of the frame; calibration scales it to your hand size
    "custom_gesture_threshold": 0.22,  # max mean distance (in palm widths) to accept a custom gesture
    "pinch_grab": 40,                # px (window space) to grab a window; calibrated
    "pinch_release": 60,
    "calibration": {},               # last measurement: light, fps, hand size, dominant hand
    # Gesture → action (see commands.py): "window:…", "media:…", "keys:…", "run:…", "script:…", a HUB_ACTIONS entry or "none".
    # The defaults work without Cortex Hub; music ones go to Hub only when it is running.
    "gesture_actions": {
        "swipe_left": "next", "swipe_right": "previous", "open_palm": "toggle",
        "thumb_up": "volume_up", "thumb_down": "volume_down", "pointing_up": "none",
        "victory": "none", "closed_fist": "none", "i_love_you": "none",
        "swipe_up": "window:maximize", "swipe_down": "window:minimize",
        "flick_right": "next", "flick_left": "previous", "circle_cw": "next", "circle_ccw": "previous",
        "wag": "none", "wave": "none", "push": "window:desktop",
        "ok": "none", "shaka": "none", "rock": "none", "three": "none", "shush": "media:mute",
        "both_palms": "window:minimize_all", "both_thumbs_up": "none", "both_thumbs_down": "none", "both_fists": "none",
        "hands_apart": "volume_up", "hands_together": "volume_down",
    },
}


class Settings:
    def __init__(self, path: Path = CONFIG_FILE):
        self.path = path
        self._lock = threading.Lock()
        self._data = dict(DEFAULTS)
        if path.exists():
            saved = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for key, value in saved.items():
                if isinstance(value, dict) and isinstance(DEFAULTS.get(key), dict):
                    value = {**DEFAULTS[key], **value}   # so new keys show up
                self._data[key] = value

    def __getitem__(self, key):
        return self._data[key]

    def all(self) -> dict:
        return dict(self._data)

    def update(self, changes: dict) -> dict:
        with self._lock:
            self._data.update({k: v for k, v in changes.items() if k in DEFAULTS})
            self.path.write_text(yaml.safe_dump(self._data, allow_unicode=True, sort_keys=False), encoding="utf-8")
            return dict(self._data)


settings = Settings()


def setup_logging() -> None:
    LOGS_DIR.mkdir(exist_ok=True)
    handler = RotatingFileHandler(LOGS_DIR / "sense.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[handler, console])
