"""Datos de Charlie en la carpeta de Drive: lectura, respuestas locales, widgets y estado.md.

Los ficheros los edita Charlie a mano o Claude; aquí solo se leen. estado.md se regenera solo cuando
cambia algún fichero (o el día), para que el Claude del móvil, que lee Drive, sepa cómo va todo.
"""

import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from .config import settings

log = logging.getLogger("datos")

DAYS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
DAYS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
DAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTHS_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
             "noviembre", "diciembre"]
SOURCES = ["horario.yaml", "gym.yaml", "comidas.yaml", "dinero.yaml", "betis.yaml", "tareas.yaml", "fechas.yaml",
           "avisos.yaml", "dudas.md", "quitado.md"]
PRIORITY = {"alta": 0, "media": 1, "baja": 2}


def folder() -> Path:
    return Path(settings["data_dir"])


def load(name: str) -> dict:
    path = folder() / name
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    except yaml.YAMLError as e:
        log.error("%s no es YAML válido: %s", name, e)
        return {}


def _as_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def say_date(d: date, today: date) -> str:
    if d == today:
        return "hoy"
    if d == today + timedelta(days=1):
        return "mañana"
    if 0 < (d - today).days < 7:
        return f"el {DAYS_ES[d.weekday()]}"
    return f"el {DAYS_ES[d.weekday()]} {d.day} de {MONTHS_ES[d.month - 1]}"


# ---------- horario ----------
def work_mode(d: date) -> str | None:
    horario = load("horario.yaml")
    for change in horario.get("cambios") or []:
        if _as_date(change.get("fecha")) == d:
            return change.get("trabajo")
    return (horario.get("dias", {}).get(DAYS[d.weekday()]) or {}).get("trabajo")


def day_plan(d: date) -> list[dict]:
    """Lista ordenada de {hora, fin?, que} para ese día."""
    horario = load("horario.yaml")
    key = DAYS[d.weekday()]
    day = horario.get("dias", {}).get(key) or {}
    items = []
    if d.weekday() < 5:
        mode = work_mode(d)
        for it in horario.get("entre_semana") or []:
            it = dict(it)
            if it["que"].startswith("Trayecto"):
                it["que"] = "Trayecto a la oficina (30 min)" if mode == "oficina" else "Margen (teletrabajo)"
            if it["que"] == "Curro":
                it["que"] = f"Curro ({mode})" if mode else "Curro"
            if it["que"] == "Gym":
                gym = (load("gym.yaml").get("dias") or {}).get(key)
                if gym:
                    it["que"] = f"Gym: {gym}"
            items.append(it)
    items += [dict(it) for it in day.get("extra") or []]
    for change in horario.get("cambios") or []:
        if _as_date(change.get("fecha")) == d and change.get("nota"):
            items.append({"hora": "", "que": change["nota"]})

    def sort_key(it):
        h = str(it.get("hora", ""))
        return h if re.fullmatch(r"\d\d:\d\d", h) else {"mañana": "10:00", "mediodía": "13:00"}.get(h, "99:99")
    return sorted(items, key=sort_key)


def remaining(d: datetime) -> list[dict]:
    now = d.strftime("%H:%M")
    out = []
    for it in day_plan(d.date()):
        h, end = str(it.get("hora", "")), str(it.get("fin", ""))
        if not re.fullmatch(r"\d\d:\d\d", h) or h >= now or (end and end > now):
            out.append(it)
    return out


def gym_of(d: date) -> str | None:
    return (load("gym.yaml").get("dias") or {}).get(DAYS[d.weekday()])


# ---------- tareas, Betis, fechas ----------
def tasks(limit: int | None = None) -> list[dict]:
    data = load("tareas.yaml")
    # «i» es su posición en tareas.yaml: el panel la usa para editar o quitar esa tarea
    items = [{**t, "i": i} for i, t in enumerate(data.get("pendientes") or [])
             if isinstance(t, dict) and str(t.get("estado", "")).lower() != "hecha"]

    def key(t):
        d = _as_date(t.get("fecha"))
        return (PRIORITY.get(str(t.get("prioridad", "media")), 1), d or date.max)
    items.sort(key=key)
    return items[:limit] if limit else items


def next_matches(today: date, n: int = 3) -> list[dict]:
    matches = [m for m in load("betis.yaml").get("partidos") or [] if (_as_date(m.get("fecha")) or date.min) >= today]
    return sorted(matches, key=lambda m: _as_date(m["fecha"]))[:n]


def upcoming_dates(today: date, days: int = 30) -> list[tuple[date, str]]:
    data = load("fechas.yaml")
    out = []
    for f in data.get("fechas") or []:
        start, end = _as_date(f.get("fecha")), _as_date(f.get("hasta"))
        if start and today <= (end or start) and start <= today + timedelta(days=days):
            out.append((start, f["que"]))
    for r in data.get("recurrentes") or []:
        if "dia_mes" in r:
            for month_offset in (0, 1):
                y, m = today.year + (today.month + month_offset - 1) // 12, (today.month + month_offset - 1) % 12 + 1
                try:
                    d = date(y, m, int(r["dia_mes"]))
                except ValueError:
                    continue
                if today <= d <= today + timedelta(days=days):
                    out.append((d, r["que"]))
        elif "anual" in r:
            m, dd = map(int, str(r["anual"]).split("-"))
            for y in (today.year, today.year + 1):
                d = date(y, m, dd)
                if today <= d <= today + timedelta(days=days):
                    out.append((d, r["que"]))
    seen, unique = set(), []
    for d, que in sorted(out):
        if (d, que) not in seen:
            seen.add((d, que))
            unique.append((d, que))
    return unique


def open_doubts() -> list[str]:
    path = folder() / "dudas.md"
    if not path.exists():
        return []
    rows = [l for l in path.read_text(encoding="utf-8").splitlines() if l.startswith("|")][2:]
    return [r.split("|")[1].strip() for r in rows if r.count("|") >= 3]


# ---------- respuestas locales (sin Claude) ----------
def norm_text(s: str) -> str:
    from .spotify import norm
    return norm(s)


def _join(parts: list[str], en: bool = False) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + (" and " if en else " y ") + parts[-1]


def _item(it: dict) -> str:
    h = str(it.get("hora", ""))
    que = it["que"].split(":")[0] if it["que"].startswith("Rutina") else it["que"]
    que = que.split(". ")[0]
    if re.fullmatch(r"\d\d:\d\d", h):
        return f"a las {h} {que[0].lower() + que[1:]}"
    return f"{h} {que[0].lower() + que[1:]}".strip()


def answer(intent: str, now: datetime) -> str:
    today = now.date()
    if intent == "hoy":
        items = remaining(now)[:5]
        if not items:
            return "Hoy ya no te queda nada en el horario."
        return "Te queda " + _join([_item(i) for i in items]) + "."
    if intent == "manana":
        d = today + timedelta(days=1)
        items = [i for i in day_plan(d) if not i["que"].startswith(("Rutina", "Andando", "Desayuno", "Merienda",
                                                                    "Dormir", "Comida"))][:5]
        mode = work_mode(d)
        head = f"Mañana {DAYS_ES[d.weekday()]}" + (f", {mode}" if mode else "")
        return head + (": " + _join([_item(i) for i in items]) + "." if items else ", sin nada especial.")
    if intent == "gym":
        g = gym_of(today)
        if g:
            return f"Hoy toca {g[0].lower() + g[1:]}."
        g = gym_of(today + timedelta(days=1))
        return "Hoy no hay gym." + (f" Mañana toca {g[0].lower() + g[1:]}." if g else "")
    if intent == "tareas":
        items = tasks(5)
        if not items:
            return "No tienes tareas pendientes."
        return f"Tienes {len(tasks())} pendientes. Las gordas: " + _join([t["tarea"].split(":")[0] for t in items]) + "."
    if intent == "betis":
        m = next_matches(today, 1)
        if not m:
            return "No tengo más partidos del Betis apuntados."
        m = m[0]
        if "confirmar" in norm_text(f"{m['partido']} {m.get('hora', '')}"):
            return ""   # faltan datos: que lo busque Claude en internet
        hora = str(m.get("hora", ""))
        when = say_date(_as_date(m["fecha"]), today)
        hora_txt = f" a las {hora}" if re.fullmatch(r"~?\d\d:\d\d", hora) else f", hora {hora.lower()}"
        return f"{when[0].upper() + when[1:]}, {m['partido']}{hora_txt.replace('~', 'sobre las ').replace('a las sobre', 'sobre')}."
    return ""


INTENTS = [
    (r"(que (me )?(toca|queda|tengo|hay)( que hacer)? (hoy|ahora|esta tarde|esta noche|lo que queda de dia)|"
     r"que me queda (hoy|por hacer)|que toca ahora|como va el dia|que hay hoy)", "hoy"),
    (r"(que (me )?(toca|tengo|hay)( que hacer)? manana|que hay manana|como es manana)", "manana"),
    (r"(que (me )?toca (hoy )?(en el |de )?gym( hoy)?|que entreno hoy|que toca entrenar( hoy)?|gym de hoy|que hago en el gym)",
     "gym"),
    (r"(que tareas tengo|mis tareas|lee(me)? (las |mis )?tareas|tareas pendientes|que tengo pendiente)", "tareas"),
    (r"(cuando juega el betis|(cual es el |cuando es el )?proximo partido( del betis)?|a que hora juega el betis|"
     r"cuando es el partido del betis)", "betis"),
]


def route(text: str) -> str | None:
    from .spotify import norm
    t = norm(text)
    t = re.sub(r"^(oye |hey |venga )?(cortex )?", "", t)
    t = re.sub(r" ?(por favor|porfa|cortex)$", "", t).strip()
    for pattern, intent in INTENTS:
        if re.fullmatch(pattern, t):
            try:
                return answer(intent, datetime.now()) or None
            except Exception:
                log.exception("Error respondiendo %s en local", intent)
                return None
    return None


# ---------- widgets ----------
def summary() -> dict:
    now = datetime.now()
    today = now.date()
    comidas = load("comidas.yaml")
    dinero = load("dinero.yaml")
    menu = comidas.get("menu") or {}
    return {
        "ok": folder().exists(),
        "today": {"items": remaining(now), "mode": work_mode(today), "day": DAYS_ES[today.weekday()]},
        "gym": {"today": gym_of(today), "tomorrow": gym_of(today + timedelta(days=1)),
                "notes": (load("gym.yaml").get("notas") or []),
                "week": [{"dia": DAYS_ES[DAYS.index(k)], "que": v, "hoy": k == DAYS[today.weekday()]}
                         for k, v in (load("gym.yaml").get("dias") or {}).items() if k in DAYS]},
        "tasks": [{"i": t["i"], "tarea": t["tarea"], "prioridad": t.get("prioridad", "media"), "notas": str(t.get("notas") or ""),
                   "cuando": t.get("cuando") or (str(t["fecha"]) if t.get("fecha") else ""),
                   "estado": str(t.get("estado", ""))} for t in tasks()],
        "betis": [{**m, "fecha": str(m["fecha"]), "cuando": say_date(_as_date(m["fecha"]), today)}
                  for m in next_matches(today, 4)],
        "menu": [{"dia": DAYS_ES[DAYS.index(k)], "plato": v.get("plato"), "cubre": v.get("cubre"),
                  "hoy": DAYS.index(k) == today.weekday()} for k, v in menu.items() if k in DAYS],
        "money": {"queda": dinero.get("queda_aprox"), "colchon": (dinero.get("colchon") or {}).get("ahorrado"),
                  "gastos": dinero.get("gastos_fijos") or {}, "ingresos": dinero.get("ingresos") or {}},
        "dates": [{"fecha": str(d), "cuando": say_date(d, today), "que": q} for d, q in upcoming_dates(today, 21)],
    }


# ---------- estado.md ----------
def build_estado(now: datetime) -> str:
    today = now.date()
    tomorrow = today + timedelta(days=1)
    L = [
        f"# Estado de {settings['user_name'] or 'el usuario'}",
        "",
        "> Resumen automático que mantiene Cortex (el asistente de casa) para el Claude del móvil. **No lo edites:**",
        "> se regenera solo. Los datos de verdad están en los otros ficheros de esta carpeta (ver CLAUDE.md).",
        "",
        f"Actualizado: {DAYS_ES[now.weekday()]} {now:%d/%m/%Y %H:%M} (hora de Madrid)",
        "",
        f"## Hoy, {DAYS_ES[today.weekday()]} {today:%d/%m}" + (f" · {work_mode(today)}" if work_mode(today) else ""),
        "",
    ]
    for it in day_plan(today):
        L.append(f"- {it.get('hora', '')}{'–' + it['fin'] if it.get('fin') else ''} {it['que']}".replace("-  ", "- "))
    L += ["", f"## Mañana, {DAYS_ES[tomorrow.weekday()]} {tomorrow:%d/%m}" +
          (f" · {work_mode(tomorrow)}" if work_mode(tomorrow) else ""), ""]
    extras = [i for i in day_plan(tomorrow) if i["que"].startswith(("Gym", "Curro", "Lavadora", "Tender", "Limpieza",
                                                                     "Compra", "Llamada", "Batch", "Levantarse"))]
    L += [f"- {i.get('hora', '')} {i['que']}" for i in extras] or ["- Nada especial"]

    L += ["", "## Tareas pendientes", "", "| Tarea | Cuándo | Prioridad | Estado |", "|---|---|---|---|"]
    for t in tasks():
        cuando = t.get("cuando") or (str(t["fecha"]) if t.get("fecha") else "")
        L.append(f"| {t['tarea']} | {cuando} | {t.get('prioridad', '')} | {t.get('estado', '')} |")

    L += ["", "## Próximas fechas (3 semanas)", ""]
    L += [f"- {d:%d/%m} ({DAYS_ES[d.weekday()]}): {q}" for d, q in upcoming_dates(today, 21)] or ["- Nada"]

    L += ["", "## Betis", "", "| Fecha | Partido | Hora | Nota |", "|---|---|---|---|"]
    for m in next_matches(today, 4):
        d = _as_date(m["fecha"])
        L.append(f"| {DAYS_ES[d.weekday()][:3]} {d:%d/%m} | {m['partido']} | {m.get('hora', '')} | {m.get('nota', '')} |")
    L.append("")
    L.append("Días de partido: camiseta del Betis. Horarios: confirmar en la web oficial.")

    avisos = [a for a in load("avisos.yaml").get("avisos") or [] if a.get("activo", True)]
    if avisos:
        L += ["", "## Avisos programados", ""]
        L += [f"- {a.get('cuando', '')}: {a.get('texto', '')}" for a in avisos[:10]]

    dinero = load("dinero.yaml")
    if dinero:
        L += ["", "## Dinero", "",
              f"- Queda al mes: ~{dinero.get('queda_aprox', '?')} €",
              f"- Colchón: {(dinero.get('colchon') or {}).get('ahorrado', '?')} € "
              f"({(dinero.get('colchon') or {}).get('regla', '')})",
              f"- Inversión: {(dinero.get('inversion') or {}).get('regla', '')}"]

    comidas = load("comidas.yaml")
    if comidas:
        L += ["", "## Comidas", "",
              f"- Objetivo: ~{(comidas.get('objetivo') or {}).get('kcal', '?')} kcal y "
              f"{(comidas.get('objetivo') or {}).get('proteina_g', '?')} g de proteína",
              f"- No come: {', '.join(comidas.get('no_como') or [])}"]
        for k, v in (comidas.get("menu") or {}).items():
            if k in DAYS:
                L.append(f"- Cocina el {DAYS_ES[DAYS.index(k)]}: {v.get('plato')} ({v.get('cubre')})")

    gym = load("gym.yaml")
    if gym:
        L += ["", "## Gym", ""]
        L += [f"- {DAYS_ES[DAYS.index(k)].capitalize()}: {v}" for k, v in (gym.get("dias") or {}).items() if k in DAYS]
        L += [f"- {n}" for n in gym.get("notas") or []]

    doubts = open_doubts()
    if doubts:
        L += ["", "## Dudas abiertas (ver dudas.md)", ""] + [f"- {d}" for d in doubts]

    quitado = folder() / "quitado.md"
    if quitado.exists():
        items = [l[2:] for l in quitado.read_text(encoding="utf-8").splitlines() if l.startswith("- ")]
        L += ["", "## No volver a sacar", "", "; ".join(items) + "."]
    return "\n".join(L) + "\n"


class EstadoWriter:
    """Regenera estado.md cuando cambia algún fichero de datos o el día."""

    def __init__(self):
        self._signature = None

    def _sig(self) -> tuple:
        mtimes = []
        for name in SOURCES:
            p = folder() / name
            mtimes.append(p.stat().st_mtime if p.exists() else 0)
        return (str(folder()), date.today(), tuple(mtimes))

    def refresh(self, force: bool = False) -> bool:
        if not folder().exists():
            return False
        sig = self._sig()
        if sig == self._signature and not force:
            return False
        content = build_estado(datetime.now())
        path = folder() / "estado.md"
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        strip = lambda s: re.sub(r"^Actualizado: .*$", "", s, flags=re.M)
        if strip(old) != strip(content):
            path.write_text(content, encoding="utf-8")
            log.info("estado.md actualizado")
        self._signature = sig
        return True
