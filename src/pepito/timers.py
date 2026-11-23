"""Temporizadores de cocina («Pepito, pon un temporizador de 10 minutos para la pasta»).

Se guardan en logs/timers.json para sobrevivir a un reinicio. El orquestador llama a due() cada segundo.
"""

import json
import logging
import re
import threading
import time
import uuid

from .config import ROOT

log = logging.getLogger("timers")

FILE = ROOT / "logs" / "timers.json"
_lock = threading.Lock()
_timers: list[dict] = []

NUMBERS = {
    "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8,
    "nueve": 9, "diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14, "quince": 15, "dieciséis": 16,
    "dieciseis": 16, "diecisiete": 17, "dieciocho": 18, "diecinueve": 19, "veinte": 20, "veinticinco": 25,
    "treinta": 30, "cuarenta": 40, "cuarenta y cinco": 45, "cincuenta": 50, "sesenta": 60, "noventa": 90,
}
_NUM = r"(\d+(?:[.,]\d+)?|" + "|".join(sorted(map(re.escape, NUMBERS), key=len, reverse=True)) + ")"
_UNIT = r"(segundos?|minutos?|min|horas?|h)"
SET_RE = re.compile(r"(temporizador|timer|cuenta atr[aá]s|av[ií]same en|alarma de)\b.*?"
                    r"(?:" + _NUM + r"\s*" + _UNIT + r"(\s+y\s+medi[ao])?|(media hora)|(un cuarto de hora)|"
                    r"(hora y media))(?:\s+(?:para|de|del|con)\s+(?:el |la |los |las )?(.+?))?[.!?]*$", re.I)
CANCEL_RE = re.compile(r"(cancela|quita|para|borra|apaga)\w*\s+(el |los |todos los )?(temporizador|timer)", re.I)
LEFT_RE = re.compile(r"cu[aá]nto (le )?queda.*(temporizador|timer)|(temporizador|timer).*cu[aá]nto queda", re.I)


def _num(word: str) -> float:
    word = word.lower().replace(",", ".")
    return float(word) if word[0].isdigit() else float(NUMBERS[word])


def parse(text: str) -> tuple[int, str] | None:
    """«pon un temporizador de 10 minutos para la pasta» → (600, "pasta") (el artículo se quita)."""
    m = SET_RE.search(text)
    if not m:
        return None
    if m.group(5):
        seconds = 1800
    elif m.group(6):
        seconds = 900
    elif m.group(7):
        seconds = 5400
    else:
        n = _num(m.group(2))
        unit = m.group(3).lower()
        mult = 1 if unit.startswith("s") else 3600 if unit.startswith("h") else 60
        seconds = n * mult + (mult / 2 if m.group(4) else 0)
    label = (m.group(8) or "").strip(" .,")
    return (int(seconds), label) if 0 < seconds <= 24 * 3600 else None


def _save():
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(json.dumps(_timers, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning("No pude guardar los temporizadores: %s", e)


def load():
    global _timers
    try:
        _timers = [t for t in json.loads(FILE.read_text(encoding="utf-8")) if t["end"] > time.time() - 60]
    except (OSError, ValueError, KeyError):
        _timers = []


def add(seconds: int, label: str = "") -> dict:
    t = {"id": uuid.uuid4().hex[:8], "label": label, "seconds": seconds, "end": time.time() + seconds}
    with _lock:
        _timers.append(t)
        _save()
    log.info("Temporizador de %d s (%s)", seconds, label or "sin nombre")
    return t


def cancel(timer_id: str | None = None) -> int:
    """Quita uno (por id) o todos. Devuelve cuántos quitó."""
    with _lock:
        before = len(_timers)
        _timers[:] = [t for t in _timers if timer_id and t["id"] != timer_id]
        _save()
        return before - len(_timers)


def active() -> list[dict]:
    with _lock:
        return sorted(_timers, key=lambda t: t["end"])


def due() -> list[dict]:
    """Los que acaban de terminar (y los quita)."""
    now = time.time()
    with _lock:
        done = [t for t in _timers if t["end"] <= now]
        if done:
            _timers[:] = [t for t in _timers if t["end"] > now]
            _save()
    return done


def say_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    parts = []
    if h:
        parts.append(f"{h} hora{'s' if h > 1 else ''}")
    if m:
        parts.append(f"{m} minuto{'s' if m > 1 else ''}")
    if s and not h:
        parts.append(f"{s} segundo{'s' if s > 1 else ''}")
    return " y ".join(parts) or "nada"
