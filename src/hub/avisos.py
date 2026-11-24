"""Scheduled reminders (phase 4): only the ones the user asks for; they live in avisos.yaml (Drive) so they show on the phone.

Claude creates them with a [[aviso: YYYY-MM-DD HH:MM | text | repeat]] tag that the orchestrator
strips from the text and stores here. Repeats: diario, laborables, findes, semanal, mensual (or none).
"""

import logging
import re
import threading
import uuid
from datetime import datetime, timedelta

import yaml

from .config import settings
from .datos import DAYS_EN, DAYS_ES, MONTHS_EN, MONTHS_ES, folder

log = logging.getLogger("avisos")

HEADER = ("# Scheduled reminders. Only the ones the user schedules or asks to schedule: Cortex invents none.\n"
          "# Managed by Cortex (also from the panel). cuando: YYYY-MM-DD HH:MM · repetir: diario, laborables,\n"
          "# findes, semanal, mensual or empty.\n\n")
REPEATS = {"", "diario", "laborables", "findes", "semanal", "mensual"}
TAG = re.compile(r"\[\[\s*aviso\s*:\s*(.+?)\]\]", re.I | re.S)
DELETE_TAG = re.compile(r"\[\[\s*borrar[ _]aviso\s*:\s*(.+?)\]\]", re.I | re.S)

_lock = threading.Lock()


def _path():
    return folder() / "avisos.yaml"


def load(strict: bool = False) -> list[dict]:
    """strict=True when reading before saving: if the file is broken (hand-edited) it is better to fail than
    to overwrite it and lose the reminders it held."""
    try:
        data = yaml.safe_load(_path().read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return []
    except yaml.YAMLError as e:
        log.error("avisos.yaml is not valid YAML: %s", e)
        if strict:
            raise ValueError("avisos.yaml is malformed; fix it before changing reminders") from e
        return []
    if not isinstance(data, dict):
        if strict:
            raise ValueError("avisos.yaml does not have the form 'avisos: ...'")
        return []
    return [a for a in data.get("avisos") or [] if isinstance(a, dict)]


def save(avisos: list[dict]) -> None:
    avisos = sorted(avisos, key=lambda a: str(a.get("cuando", "")))
    _path().write_text(HEADER + yaml.safe_dump({"avisos": avisos}, allow_unicode=True, sort_keys=False),
                       encoding="utf-8")


def parse_when(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(str(value).strip()[:16].replace("T", " "), "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def add(when: datetime, text: str, repeat: str = "") -> dict:
    repeat = repeat.strip().lower()
    if repeat not in REPEATS:
        repeat = ""
    item = {"id": uuid.uuid4().hex[:6], "cuando": when.strftime("%Y-%m-%d %H:%M"), "texto": text.strip(),
            "repetir": repeat, "activo": True}
    with _lock:
        avisos = load(strict=True)
        avisos.append(item)
        save(avisos)
    log.info("New reminder: %s", item)
    return item


def delete(aviso_id: str) -> bool:
    with _lock:
        try:
            avisos = load(strict=True)
        except ValueError as e:
            log.warning("Not deleting reminder %s: %s", aviso_id, e)
            return False
        left = [a for a in avisos if str(a.get("id")) != str(aviso_id)]
        if len(left) == len(avisos):
            return False
        save(left)
    log.info("Reminder deleted: %s", aviso_id)
    return True


def from_tag(body: str) -> dict | None:
    """'2026-10-24 18:00 | hang the laundry | semanal' -> saved reminder."""
    parts = [p.strip() for p in body.split("|")]
    when = parse_when(parts[0]) if parts else None
    if not when or len(parts) < 2 or not parts[1]:
        log.warning("Invalid reminder tag: %r", body)
        return None
    try:
        return add(when, parts[1], parts[2] if len(parts) > 2 else "")
    except ValueError as e:
        log.warning("Not saving reminder %r: %s", body, e)
        return None


def _next(when: datetime, repeat: str, day: int | None = None) -> datetime | None:
    """day: the original day of the month of a monthly reminder (the 31st falls on the 30th or 28th, then goes back to the 31st)."""
    if repeat == "diario":
        return when + timedelta(days=1)
    if repeat == "semanal":
        return when + timedelta(days=7)
    if repeat in ("laborables", "findes"):
        d = when + timedelta(days=1)
        while (d.weekday() < 5) != (repeat == "laborables"):
            d += timedelta(days=1)
        return d
    if repeat == "mensual":
        month = when.month % 12 + 1
        year = when.year + (when.month == 12)
        for day in (day or when.day, 30, 29, 28):
            try:
                return when.replace(year=year, month=month, day=day)
            except ValueError:
                continue
    return None


def in_quiet_hours(now: datetime) -> bool:
    start, end = settings["quiet_start"], settings["quiet_end"]
    if not start or not end or start == end:
        return False
    t = now.strftime("%H:%M")
    return start <= t < end if start < end else t >= start or t < end


def due(now: datetime) -> list[dict]:
    """Reminders due now. One-off ones are marked done; repeating ones move to their next time."""
    if in_quiet_hours(now):
        return []
    fired = []
    with _lock:
        avisos = load()
        changed = False
        for a in avisos:
            when = parse_when(a.get("cuando"))
            if not a.get("activo", True) or not when or when > now:
                continue
            if now - when > timedelta(hours=12):
                # long overdue (laptop was off): not announced at once, but it still moves to its next time
                log.info("Overdue reminder, not announcing it: %s", a)
            else:
                fired.append(dict(a))
            day = a.get("dia") or when.day
            nxt = _next(when, a.get("repetir") or "", day)
            while nxt and nxt <= now:
                nxt = _next(nxt, a.get("repetir") or "", day)
            if nxt:
                a["cuando"] = nxt.strftime("%Y-%m-%d %H:%M")
                if a.get("repetir") == "mensual" and nxt.day != day:
                    a["dia"] = day
                else:
                    a.pop("dia", None)
            else:
                a["activo"] = False
            changed = True
        if changed:
            save([a for a in avisos if a.get("activo", True) or a.get("repetir")])
    return fired


def say_when(when: datetime, now: datetime, en: bool = True) -> str:
    today = now.date()
    if en:
        if when.date() == today:
            day = "today"
        elif when.date() == today + timedelta(days=1):
            day = "tomorrow"
        elif 0 < (when.date() - today).days < 7:
            day = f"on {DAYS_EN[when.weekday()]}"
        else:
            day = f"on {MONTHS_EN[when.month - 1]} {when.day}"
        return f"{day} at {when:%H:%M}"
    if when.date() == today:
        day = "hoy"
    elif when.date() == today + timedelta(days=1):
        day = "mañana"
    elif 0 < (when.date() - today).days < 7:
        day = f"el {DAYS_ES[when.weekday()]}"
    else:
        day = f"el {when.day} de {MONTHS_ES[when.month - 1]}"
    return f"{day} a las {when:%H:%M}"


def upcoming(now: datetime, limit: int = 20) -> list[dict]:
    out = []
    for a in load():
        when = parse_when(a.get("cuando"))
        if a.get("activo", True) and when:
            out.append({**a, "cuando": when.strftime("%Y-%m-%d %H:%M"), "di": say_when(when, now)})
    return sorted(out, key=lambda a: a["cuando"])[:limit]
