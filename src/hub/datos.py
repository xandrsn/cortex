"""The user's data in the Drive folder: reading, local answers, widgets and estado.md.

The files are edited by hand or by Claude; here they are only read. estado.md is regenerated on its own when
any file (or the day) changes, so the mobile Claude, which reads Drive, knows how everything stands.
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
MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
             "November", "December"]
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
        log.error("%s is not valid YAML: %s", name, e)
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


def say_date(d: date, today: date, en: bool = True) -> str:
    if en:
        if d == today:
            return "today"
        if d == today + timedelta(days=1):
            return "tomorrow"
        if 0 < (d - today).days < 7:
            return f"on {DAYS_EN[d.weekday()]}"
        return f"on {DAYS_EN[d.weekday()]}, {MONTHS_EN[d.month - 1]} {d.day}"
    if d == today:
        return "hoy"
    if d == today + timedelta(days=1):
        return "mañana"
    if 0 < (d - today).days < 7:
        return f"el {DAYS_ES[d.weekday()]}"
    return f"el {DAYS_ES[d.weekday()]} {d.day} de {MONTHS_ES[d.month - 1]}"


# ---------- schedule ----------
def work_mode(d: date) -> str | None:
    horario = load("horario.yaml")
    for change in horario.get("cambios") or []:
        if _as_date(change.get("fecha")) == d:
            return change.get("trabajo")
    return (horario.get("dias", {}).get(DAYS[d.weekday()]) or {}).get("trabajo")


def day_plan(d: date) -> list[dict]:
    """Ordered list of {hora, fin?, que} for that day."""
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


# ---------- tasks, Betis, dates ----------
def tasks(limit: int | None = None) -> list[dict]:
    data = load("tareas.yaml")
    # "i" is its position in tareas.yaml: the panel uses it to edit or remove that task
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


# ---------- local answers (without Claude) ----------
def norm_text(s: str) -> str:
    from .spotify import norm
    return norm(s)


def _join(parts: list[str], en: bool = False) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + (" and " if en else " y ") + parts[-1]


def _item(it: dict, en: bool = False) -> str:
    h = str(it.get("hora", ""))
    que = it["que"].split(":")[0] if it["que"].startswith("Rutina") else it["que"]
    que = que.split(". ")[0]
    if en:
        return f"{que[0].lower() + que[1:]} at {h}" if re.fullmatch(r"\d\d:\d\d", h) else f"{que[0].lower() + que[1:]} {h}".strip()
    if re.fullmatch(r"\d\d:\d\d", h):
        return f"a las {h} {que[0].lower() + que[1:]}"
    return f"{h} {que[0].lower() + que[1:]}".strip()


def answer(intent: str, now: datetime, en: bool = False) -> str:
    """Spoken answer for a schedule question, in English or Spanish (`en`)."""
    today = now.date()
    if intent == "hoy":
        items = remaining(now)[:5]
        if not items:
            return "Nothing left on your schedule today." if en else "Hoy ya no te queda nada en el horario."
        if en:
            return "Left today: " + _join([_item(i, True) for i in items], True) + "."
        return "Te queda " + _join([_item(i) for i in items]) + "."
    if intent == "manana":
        d = today + timedelta(days=1)
        items = [i for i in day_plan(d) if not i["que"].startswith(("Rutina", "Andando", "Desayuno", "Merienda",
                                                                    "Dormir", "Comida"))][:5]
        mode = work_mode(d)
        if en:
            head = f"Tomorrow, {DAYS_EN[d.weekday()]}" + (f", {mode}" if mode else "")
            return head + (": " + _join([_item(i, True) for i in items], True) + "." if items else ", nothing special.")
        head = f"Mañana {DAYS_ES[d.weekday()]}" + (f", {mode}" if mode else "")
        return head + (": " + _join([_item(i) for i in items]) + "." if items else ", sin nada especial.")
    if intent == "gym":
        g = gym_of(today)
        if g:
            return f"Today it's {g[0].lower() + g[1:]}." if en else f"Hoy toca {g[0].lower() + g[1:]}."
        g = gym_of(today + timedelta(days=1))
        if en:
            return "No gym today." + (f" Tomorrow it's {g[0].lower() + g[1:]}." if g else "")
        return "Hoy no hay gym." + (f" Mañana toca {g[0].lower() + g[1:]}." if g else "")
    if intent == "tareas":
        items = tasks(5)
        if not items:
            return "You have no pending tasks." if en else "No tienes tareas pendientes."
        names = _join([t["tarea"].split(":")[0] for t in items], en)
        if en:
            return f"You have {len(tasks())} pending. The big ones: " + names + "."
        return f"Tienes {len(tasks())} pendientes. Las gordas: " + names + "."
    if intent == "betis":
        m = next_matches(today, 1)
        if not m:
            return "I have no more Betis matches noted." if en else "No tengo más partidos del Betis apuntados."
        m = m[0]
        if "confirmar" in norm_text(f"{m['partido']} {m.get('hora', '')}"):
            return ""   # data missing: let Claude look it up online
        hora = str(m.get("hora", ""))
        when = say_date(_as_date(m["fecha"]), today, en)
        if en:
            hora_txt = f" at {hora}" if re.fullmatch(r"~?\d\d:\d\d", hora) else f", time {hora.lower()}"
            return f"{when[0].upper() + when[1:]}, {m['partido']}{hora_txt.replace('~', 'around ').replace('at around', 'around')}."
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
# English equivalents (text goes through spotify.norm first: lowercase, no punctuation)
INTENTS_EN = [
    (r"(what (do i have|is left|s left|s on|is on) (today|for today|left today)|what s left|how is the day going|"
     r"what is on today)", "hoy"),
    (r"(what (do i have|is|s) (on )?tomorrow|what is on tomorrow|how is tomorrow)", "manana"),
    (r"(what is (my )?(gym|workout)( today)?|what s (my )?(gym|workout)( today)?|what do i train today|"
     r"what is my workout)", "gym"),
    (r"(what tasks do i have|my tasks|read (me )?(my )?tasks|pending tasks|what is pending)", "tareas"),
    (r"(when does betis play|when is the next betis (game|match)|next betis (game|match)|when is the next (game|match))",
     "betis"),
]


def route(text: str) -> str | None:
    """Answer a schedule question locally; replies in the language of the phrase that matched."""
    from .spotify import norm
    t = norm(text)
    t = re.sub(r"^(oye |hey |venga )?(cortex )?", "", t)
    t = re.sub(r" ?(por favor|porfa|please|cortex)$", "", t).strip()
    for patterns, en in ((INTENTS, False), (INTENTS_EN, True)):
        for pattern, intent in patterns:
            if re.fullmatch(pattern, t):
                try:
                    return answer(intent, datetime.now(), en) or None
                except Exception:
                    log.exception("Error answering %s locally", intent)
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
        "today": {"items": remaining(now), "mode": work_mode(today), "day": DAYS_EN[today.weekday()]},
        "gym": {"today": gym_of(today), "tomorrow": gym_of(today + timedelta(days=1)),
                "notes": (load("gym.yaml").get("notas") or []),
                "week": [{"dia": DAYS_EN[DAYS.index(k)], "que": v, "hoy": k == DAYS[today.weekday()]}
                         for k, v in (load("gym.yaml").get("dias") or {}).items() if k in DAYS]},
        "tasks": [{"i": t["i"], "tarea": t["tarea"], "prioridad": t.get("prioridad", "media"), "notas": str(t.get("notas") or ""),
                   "cuando": t.get("cuando") or (str(t["fecha"]) if t.get("fecha") else ""),
                   "estado": str(t.get("estado", ""))} for t in tasks()],
        "betis": [{**m, "fecha": str(m["fecha"]), "cuando": say_date(_as_date(m["fecha"]), today)}
                  for m in next_matches(today, 4)],
        "menu": [{"dia": DAYS_EN[DAYS.index(k)], "plato": v.get("plato"), "cubre": v.get("cubre"),
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
        f"# Status of {settings['user_name'] or 'the user'}",
        "",
        "> Automatic summary kept by Cortex (the home assistant) for the mobile Claude. **Do not edit it:**",
        "> it regenerates itself. The real data lives in the other files of this folder (see CLAUDE.md).",
        "",
        f"Updated: {DAYS_EN[now.weekday()]} {now:%d/%m/%Y %H:%M} (Madrid time)",
        "",
        f"## Today, {DAYS_EN[today.weekday()]} {today:%d/%m}" + (f" · {work_mode(today)}" if work_mode(today) else ""),
        "",
    ]
    for it in day_plan(today):
        L.append(f"- {it.get('hora', '')}{'–' + it['fin'] if it.get('fin') else ''} {it['que']}".replace("-  ", "- "))
    L += ["", f"## Tomorrow, {DAYS_EN[tomorrow.weekday()]} {tomorrow:%d/%m}" +
          (f" · {work_mode(tomorrow)}" if work_mode(tomorrow) else ""), ""]
    extras = [i for i in day_plan(tomorrow) if i["que"].startswith(("Gym", "Curro", "Lavadora", "Tender", "Limpieza",
                                                                     "Compra", "Llamada", "Batch", "Levantarse"))]
    L += [f"- {i.get('hora', '')} {i['que']}" for i in extras] or ["- Nothing special"]

    L += ["", "## Pending tasks", "", "| Task | When | Priority | Status |", "|---|---|---|---|"]
    for t in tasks():
        cuando = t.get("cuando") or (str(t["fecha"]) if t.get("fecha") else "")
        L.append(f"| {t['tarea']} | {cuando} | {t.get('prioridad', '')} | {t.get('estado', '')} |")

    L += ["", "## Upcoming dates (3 weeks)", ""]
    L += [f"- {d:%d/%m} ({DAYS_EN[d.weekday()]}): {q}" for d, q in upcoming_dates(today, 21)] or ["- Nothing"]

    L += ["", "## Betis", "", "| Date | Match | Time | Note |", "|---|---|---|---|"]
    for m in next_matches(today, 4):
        d = _as_date(m["fecha"])
        L.append(f"| {DAYS_EN[d.weekday()][:3]} {d:%d/%m} | {m['partido']} | {m.get('hora', '')} | {m.get('nota', '')} |")
    L.append("")
    L.append("Match days: Betis shirt. Times: confirm on the official website.")

    avisos = [a for a in load("avisos.yaml").get("avisos") or [] if a.get("activo", True)]
    if avisos:
        L += ["", "## Scheduled reminders", ""]
        L += [f"- {a.get('cuando', '')}: {a.get('texto', '')}" for a in avisos[:10]]

    dinero = load("dinero.yaml")
    if dinero:
        L += ["", "## Money", "",
              f"- Left per month: ~{dinero.get('queda_aprox', '?')} €",
              f"- Cushion: {(dinero.get('colchon') or {}).get('ahorrado', '?')} € "
              f"({(dinero.get('colchon') or {}).get('regla', '')})",
              f"- Investing: {(dinero.get('inversion') or {}).get('regla', '')}"]

    comidas = load("comidas.yaml")
    if comidas:
        L += ["", "## Meals", "",
              f"- Target: ~{(comidas.get('objetivo') or {}).get('kcal', '?')} kcal and "
              f"{(comidas.get('objetivo') or {}).get('proteina_g', '?')} g of protein",
              f"- Does not eat: {', '.join(comidas.get('no_como') or [])}"]
        for k, v in (comidas.get("menu") or {}).items():
            if k in DAYS:
                L.append(f"- Cooks on {DAYS_EN[DAYS.index(k)]}: {v.get('plato')} (covers {v.get('cubre')})")

    gym = load("gym.yaml")
    if gym:
        L += ["", "## Gym", ""]
        L += [f"- {DAYS_EN[DAYS.index(k)]}: {v}" for k, v in (gym.get("dias") or {}).items() if k in DAYS]
        L += [f"- {n}" for n in gym.get("notas") or []]

    doubts = open_doubts()
    if doubts:
        L += ["", "## Open questions (see dudas.md)", ""] + [f"- {d}" for d in doubts]

    quitado = folder() / "quitado.md"
    if quitado.exists():
        items = [l[2:] for l in quitado.read_text(encoding="utf-8").splitlines() if l.startswith("- ")]
        L += ["", "## Do not bring up again", "", "; ".join(items) + "."]
    return "\n".join(L) + "\n"


class EstadoWriter:
    """Regenerates estado.md when a data file or the day changes."""

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
        strip = lambda s: re.sub(r"^Updated: .*$", "", s, flags=re.M)
        if strip(old) != strip(content):
            path.write_text(content, encoding="utf-8")
            log.info("estado.md updated")
        self._signature = sig
        return True
