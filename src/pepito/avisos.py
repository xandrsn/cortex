"""Avisos programados (fase 4): solo los que Charlie pide; viven en avisos.yaml (Drive) para que se vean desde el móvil.

Claude los crea con una etiqueta [[aviso: AAAA-MM-DD HH:MM | texto | repetición]] que el orquestador
quita del texto y guarda aquí. Repeticiones: diario, laborables, findes, semanal, mensual (o nada).
"""

import logging
import re
import threading
import uuid
from datetime import datetime, timedelta

import yaml

from .config import settings
from .datos import DAYS_ES, MONTHS_ES, folder

log = logging.getLogger("avisos")

HEADER = ("# Avisos programados. Solo los que Charlie programe o pida programar: Pepito no se inventa ninguno.\n"
          "# Los gestiona Pepito (también desde el panel). cuando: AAAA-MM-DD HH:MM · repetir: diario, laborables,\n"
          "# findes, semanal, mensual o vacío.\n\n")
REPEATS = {"", "diario", "laborables", "findes", "semanal", "mensual"}
TAG = re.compile(r"\[\[\s*aviso\s*:\s*(.+?)\]\]", re.I | re.S)
DELETE_TAG = re.compile(r"\[\[\s*borrar[ _]aviso\s*:\s*(.+?)\]\]", re.I | re.S)

_lock = threading.Lock()


def _path():
    return folder() / "avisos.yaml"


def load(strict: bool = False) -> list[dict]:
    """strict=True para leer antes de guardar: si el archivo está roto (editado a mano) mejor fallar que
    sobrescribirlo y perder los avisos que hubiera."""
    try:
        data = yaml.safe_load(_path().read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return []
    except yaml.YAMLError as e:
        log.error("avisos.yaml no es YAML válido: %s", e)
        if strict:
            raise ValueError("avisos.yaml está mal escrito; arréglalo antes de cambiar avisos") from e
        return []
    if not isinstance(data, dict):
        if strict:
            raise ValueError("avisos.yaml no tiene la forma «avisos: …»")
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
    log.info("Aviso nuevo: %s", item)
    return item


def delete(aviso_id: str) -> bool:
    with _lock:
        try:
            avisos = load(strict=True)
        except ValueError as e:
            log.warning("No borro el aviso %s: %s", aviso_id, e)
            return False
        left = [a for a in avisos if str(a.get("id")) != str(aviso_id)]
        if len(left) == len(avisos):
            return False
        save(left)
    log.info("Aviso borrado: %s", aviso_id)
    return True


def from_tag(body: str) -> dict | None:
    """«2026-10-24 18:00 | tender la ropa | semanal» → aviso guardado."""
    parts = [p.strip() for p in body.split("|")]
    when = parse_when(parts[0]) if parts else None
    if not when or len(parts) < 2 or not parts[1]:
        log.warning("Etiqueta de aviso no válida: %r", body)
        return None
    try:
        return add(when, parts[1], parts[2] if len(parts) > 2 else "")
    except ValueError as e:
        log.warning("No guardo el aviso %r: %s", body, e)
        return None


def _next(when: datetime, repeat: str, day: int | None = None) -> datetime | None:
    """day: el día del mes original de un aviso mensual (el 31 cae el 30 o el 28, pero luego vuelve al 31)."""
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
    """Avisos que tocan ya. Los únicos se marcan como hechos; los repetidos pasan a la siguiente vez."""
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
                # muy atrasado (portátil apagado): no se suelta de golpe, pero sí pasa a la siguiente vez
                log.info("Aviso atrasado, no lo digo: %s", a)
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


def say_when(when: datetime, now: datetime) -> str:
    today = now.date()
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
