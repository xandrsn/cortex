"""Saldo y movimientos de Revolut vía Enable Banking (PSD2, SOLO LECTURA).

Solo se usan endpoints de consulta: GET /aspsps, POST /auth, POST /sessions, GET /sessions/{id},
GET /accounts/{uid}/balances y GET /accounts/{uid}/transactions. Nada de pagos.

Secretos (nunca en Drive): EB_APP_ID y EB_KEY_PATH (el <app id>.pem) en el .env; EB_REDIRECT_URL opcional;
la sesión en secrets/bank_session.json.
Los bancos solo permiten ~4 consultas al día sin el usuario delante, así que se cachea 6 h y, aunque se
pida refrescar, nunca se consulta más de una vez cada 10 min.

Uso a mano (desde C:la carpeta del programa\\src):  python -m pepito.bank auth | resumen [--forzar] | estado
"""

import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import jwt

from .config import ROOT

log = logging.getLogger("bank")

API = "https://api.enablebanking.com"
SECRETS = ROOT / "secrets"
SESSION_FILE = SECRETS / "bank_session.json"
CACHE_FILE = SECRETS / "bank_cache.json"     # también son datos bancarios: fuera de Drive
DEFAULT_KEY = SECRETS / "enablebanking.pem"
DEFAULT_REDIRECT = "https://localhost:8765/bank/callback"   # la registrada en la app de Enable Banking

CACHE_SECONDS = 6 * 3600
MIN_REFRESH_SECONDS = 10 * 60
# Orden de preferencia de tipos de saldo (ISO 20022): disponible → contable → el que haya
BALANCE_ORDER = ["CLAV", "ITAV", "XPCD", "CLBD", "ITBD", "OPAV", "OPBD"]

# Lista blanca de llamadas permitidas: cualquier otra cosa (p. ej. /payments) se rechaza
_ALLOWED = [
    ("GET", re.compile(r"^/aspsps$")),
    ("POST", re.compile(r"^/auth$")),
    ("POST", re.compile(r"^/sessions$")),
    ("GET", re.compile(r"^/sessions/[\w-]+$")),
    ("GET", re.compile(r"^/accounts/[\w-]+/balances$")),
    ("GET", re.compile(r"^/accounts/[\w-]+/transactions$")),
]

_lock = threading.Lock()
_token: dict = {}


class BankError(Exception):
    """Error con mensaje en español, listo para decírselo a Charlie."""


# ---------- credenciales y llamadas ----------

def _app_id() -> str:
    app_id = os.environ.get("EB_APP_ID", "").strip()
    if not app_id:
        raise BankError("Falta el EB_APP_ID en el .env (el ID de la aplicación de Enable Banking).")
    return app_id


def _key() -> bytes:
    path = Path(os.environ.get("EB_KEY_PATH", "").strip() or DEFAULT_KEY)
    if not path.exists():   # p. ej. "\a" en un .env entre comillas dobles se come la barra: probar <app id>.pem
        path = SECRETS / f"{_app_id()}.pem"
    if not path.exists():
        raise BankError(f"No encuentro la clave privada de Enable Banking en {path}.")
    return path.read_bytes()


def _jwt() -> str:
    """JWT RS256 firmado con la clave de la app; se reutiliza hasta 5 min antes de caducar (1 h)."""
    now = int(time.time())
    if _token.get("exp", 0) - now > 300:
        return _token["jwt"]
    body = {"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": now, "exp": now + 3600}
    try:
        token = jwt.encode(body, _key(), algorithm="RS256", headers={"kid": _app_id()})
    except BankError:
        raise
    except Exception as e:
        raise BankError(f"La clave privada no vale para firmar: {e}") from e
    _token.update(jwt=token, exp=body["exp"])
    return token


def _error_text(r: httpx.Response) -> str:
    try:
        d = r.json()
        return f"{d.get('error') or d.get('code') or ''} {d.get('message') or d.get('detail') or ''}".strip()
    except Exception:
        return r.text[:200]


def _call(method: str, path: str, **kw) -> dict:
    if not any(m == method and rx.match(path) for m, rx in _ALLOWED):
        raise BankError(f"Llamada no permitida (solo lectura): {method} {path}")
    try:
        r = httpx.request(method, API + path, timeout=30,
                          headers={"Authorization": f"Bearer {_jwt()}"}, **kw)
    except httpx.HTTPError as e:
        raise BankError(f"No pude conectar con Enable Banking: {e}") from e
    if r.status_code < 300:
        return r.json()
    detail = _error_text(r)
    log.warning("%s %s → %s %s", method, path, r.status_code, detail)
    if "EXPIRED_SESSION" in detail or "CLOSED_SESSION" in detail or "REVOKED" in detail:
        raise BankError("El permiso con el banco ha caducado o se ha revocado. Hay que volver a autorizar.")
    if r.status_code == 429:
        raise BankError("El banco no deja consultar más ahora mismo (límite diario). Prueba dentro de unas horas.")
    if r.status_code == 401:
        raise BankError("Enable Banking no acepta las credenciales (revisa EB_APP_ID y la clave .pem).")
    if r.status_code == 403:
        raise BankError("Enable Banking deniega el acceso (¿la app está activada y la cuenta vinculada?).")
    raise BankError(f"Error de Enable Banking ({r.status_code}): {detail}")


# ---------- ficheros de estado ----------

def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("No pude leer %s: %s", path.name, e)
        return {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------- autorización ----------

def _max_consent_seconds(aspsp_name: str, country: str) -> int:
    """Validez máxima del permiso para ese banco (180 días en Revolut)."""
    for a in _call("GET", "/aspsps", params={"country": country}).get("aspsps", []):
        if a.get("name", "").lower() == aspsp_name.lower():
            return int(a.get("maximum_consent_validity") or 90 * 86400)
    raise BankError(f"Enable Banking no tiene «{aspsp_name}» en el país {country}.")


def start_auth(aspsp_name: str = "Revolut", country: str = "ES", redirect_url: str | None = None) -> str:
    """Pide a Enable Banking el enlace de consentimiento y lo devuelve. Guarda el 'state' para comprobarlo luego."""
    redirect_url = redirect_url or os.environ.get("EB_REDIRECT_URL", "").strip() or DEFAULT_REDIRECT
    seconds = _max_consent_seconds(aspsp_name, country) - 3600   # margen por relojes desajustados
    valid_until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    state = str(uuid.uuid4())
    d = _call("POST", "/auth", json={
        "access": {"valid_until": valid_until.isoformat()},
        "aspsp": {"name": aspsp_name, "country": country},
        "state": state,
        "redirect_url": redirect_url,
        "psu_type": "personal",
    })
    data = _load(SESSION_FILE)
    data["pending"] = {"state": state, "aspsp": aspsp_name, "country": country, "at": time.time()}
    _save(SESSION_FILE, data)
    log.info("Consentimiento pedido para %s (%s)", aspsp_name, country)
    return d["url"]


def _mask(account: dict) -> str:
    ids = account.get("account_id") or {}
    iban = ids.get("iban") or (ids.get("other") or {}).get("identification") or ""
    return f"···{iban[-4:]}" if iban else ""


def finish_auth(code: str) -> dict:
    """Recibe el 'code' (o la URL entera a la que te mandó el banco), crea la sesión y la guarda."""
    code = code.strip()
    state = None
    if code.startswith("http"):
        q = parse_qs(urlparse(code).query)
        if "error" in q:
            raise BankError(f"El banco no dio permiso: {q.get('error_description', q['error'])[0]}")
        state = (q.get("state") or [None])[0]
        code = (q.get("code") or [""])[0]
    if not code:
        raise BankError("No veo el 'code' en lo que has pegado.")
    data = _load(SESSION_FILE)
    pending = data.get("pending") or {}
    if state and pending.get("state") and state != pending["state"]:
        raise BankError("El 'state' no coincide con la petición; vuelve a empezar la autorización.")
    s = _call("POST", "/sessions", json={"code": code})
    accounts = [{"uid": a.get("uid"), "name": a.get("name") or a.get("product") or "Cuenta",
                 "currency": a.get("currency"), "masked": _mask(a)}
                for a in s.get("accounts", []) if a.get("uid")]
    session = {"session_id": s["session_id"], "valid_until": (s.get("access") or {}).get("valid_until"),
               "aspsp": s.get("aspsp") or {"name": pending.get("aspsp"), "country": pending.get("country")},
               "accounts": accounts, "created": time.time()}
    _save(SESSION_FILE, {"session": session})
    CACHE_FILE.unlink(missing_ok=True)
    log.info("Sesión bancaria creada con %d cuentas", len(accounts))
    return session


def _session() -> dict:
    s = _load(SESSION_FILE).get("session")
    if not s:
        raise BankError("Aún no hay ningún banco conectado. Ejecuta: python -m pepito.bank auth")
    return s


def consent_expires_in_days() -> int | None:
    """Días que le quedan al permiso del banco (None si no hay sesión). Negativo = caducado."""
    s = _load(SESSION_FILE).get("session") or {}
    if not s.get("valid_until"):
        return None
    until = datetime.fromisoformat(s["valid_until"].replace("Z", "+00:00"))
    return (until - datetime.now(timezone.utc)).days


def session_status() -> str:
    """Estado de la sesión según Enable Banking (AUTHORIZED, EXPIRED…). Gasta una llamada: usar poco."""
    return _call("GET", f"/sessions/{_session()['session_id']}").get("status", "?")


# ---------- saldo y movimientos ----------

def _balance(uid: str) -> dict:
    balances = _call("GET", f"/accounts/{uid}/balances").get("balances", [])
    if not balances:
        return {}
    rank = {t: i for i, t in enumerate(BALANCE_ORDER)}
    b = min(balances, key=lambda x: rank.get(x.get("balance_type"), 99))
    amt = b.get("balance_amount") or {}
    return {"amount": float(amt.get("amount", 0)), "currency": amt.get("currency"), "type": b.get("balance_type")}


def _tx_row(t: dict) -> dict:
    amt = t.get("transaction_amount") or {}
    value = float(amt.get("amount", 0))
    if t.get("credit_debit_indicator") == "DBIT":
        value = -abs(value)
    other = (t.get("creditor") if value < 0 else t.get("debtor")) or {}
    desc = " ".join(t.get("remittance_information") or []).strip() or other.get("name") or t.get("note") or ""
    return {"date": t.get("booking_date") or t.get("value_date") or t.get("transaction_date"),
            "amount": value, "currency": amt.get("currency"), "description": desc[:80],
            "pending": t.get("status") == "PDNG"}


def _transactions(uid: str) -> list[dict]:
    """Movimientos desde el día 1 del mes pasado (para comparar meses)."""
    first = date.today().replace(day=1)
    params = {"date_from": (first - timedelta(days=1)).replace(day=1).isoformat()}
    rows: list[dict] = []
    for _ in range(8):                       # pocas páginas: cada una cuenta como llamada
        d = _call("GET", f"/accounts/{uid}/transactions", params=params)
        rows += [_tx_row(t) for t in d.get("transactions", [])]
        if not d.get("continuation_key"):
            break
        params["continuation_key"] = d["continuation_key"]
    rows.sort(key=lambda r: r["date"] or "", reverse=True)
    return rows


# ---------- gastos ----------
# Categorías por palabras del concepto (lo primero que case). Lo que no case va a «otros».
CATEGORIES = [
    ("súper", r"mercadona|carrefour|carref|lidl|\bdia\b|alcampo|aldi|ahorramas|eroski|supermercado|hipercor|supeco|"
              r"el jamon|consum\b|covirán|coviran"),
    ("transporte", r"crtm|metro|renfe|\bave\b|cabify|uber(?! ?eats)|bolt|\bemt\b|bicimad|blablacar|gasolin|repsol|"
                   r"cepsa|taxi|tussam"),
    ("comer fuera", r"restaurante|restuarante|\brest\b|\bbar\b|caf[eé]|burger|mcdonald|kfc|telepizza|domino|glovo|"
                    r"just ?eat|uber ?eats|brown brothers|taberna|tasca|tasquita|cervecer|pizzer|sushi|kebab|ramen|"
                    r"hosteler|bistro|\bventa\b|china city|dominguita|carbajo"),
    ("bizum", r"bizum"),
    ("compras", r"amazon|aliexpress|zara|lefties|decathlon|primark|ikea|mediamarkt|shein|temu|el corte ingl"),
    ("suscripciones", r"spotify|netflix|claude|anthropic|apple\.com|google|youtube|hbo|disney|prime video|icloud"),
    ("tabaco", r"estanco|expend\b|expend "),
    ("apuestas", r"pokerstars|bet365|codere|sportium|luckia|bwin|casino"),
    ("efectivo", r"atm|cash withdrawal|cajero"),
    ("ocio", r"cine|steam|playstation|ticketmaster|entradas|gimnasio|basic-fit"),
]
# No son gastos: dinero que se mueve a tus propios huchas, inversiones o cambios de divisa
NOT_SPENDING = re.compile(r"to investment|exchanged to|to savings|to pocket|vault|hucha|to eur|to usd", re.I)
_CAT_RE = [(name, re.compile(rx, re.I)) for name, rx in CATEGORIES]


def category(description: str) -> str:
    for name, rx in _CAT_RE:
        if rx.search(description or ""):
            return name
    return "otros"


def spending(rows: list[dict], today: date | None = None) -> dict:
    """Gasto del mes (solo cargos), media diaria, proyección a fin de mes, mes anterior y categorías."""
    today = today or date.today()
    month = today.strftime("%Y-%m")
    prev = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    out = [r for r in rows if r["amount"] < 0 and not NOT_SPENDING.search(r["description"] or "")]
    this = [r for r in out if (r["date"] or "").startswith(month)]
    spent = round(-sum(r["amount"] for r in this), 2)
    days = ((today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).day
    per_day = spent / today.day
    cats: dict[str, float] = {}
    for r in this:
        c = category(r["description"])
        cats[c] = cats.get(c, 0.0) - r["amount"]
    return {"mes": month, "gastado": spent, "media_diaria": round(per_day, 2),
            "proyeccion": round(per_day * days, 2), "dias_mes": days, "dia": today.day,
            "mes_anterior": round(-sum(r["amount"] for r in out if (r["date"] or "").startswith(prev)), 2),
            "categorias": sorted(([k, round(v, 2)] for k, v in cats.items()), key=lambda kv: -kv[1])}


def _fetch(n: int) -> dict:
    s = _session()
    accounts = []
    for a in s["accounts"]:
        rows = _transactions(a["uid"])
        accounts.append({"name": a["name"], "masked": a["masked"], "currency": a["currency"],
                         "balance": _balance(a["uid"]), "transactions": rows})   # este mes y el anterior
    return {"ok": True, "accounts": accounts, "fetched_at": time.time(),
            "consent_days": consent_expires_in_days()}


def summary(force: bool = False, n: int = 10) -> dict:
    """Saldo por cuenta y últimos n movimientos. Cache 6 h; con force, refresca si han pasado ≥10 min."""
    with _lock:
        cache = _load(CACHE_FILE)
        age = time.time() - cache.get("fetched_at", 0)
        if cache and (age < MIN_REFRESH_SECONDS or (age < CACHE_SECONDS and not force)):
            return _with_spending(cache)
        try:
            data = _fetch(n)
        except BankError as e:
            log.warning("Banco: %s", e)
            if cache:
                return _with_spending({**cache, "stale": True, "error": str(e)})
            return {"ok": False, "error": str(e)}
        _save(CACHE_FILE, data)
        return _with_spending(data)


def _with_spending(data: dict) -> dict:
    """Los gastos se calculan al leer (las categorías pueden cambiar sin gastar consultas al banco)."""
    for a in data.get("accounts", []):
        a["spending"] = spending(a.get("transactions") or [])
    return data


# ---------- línea de comandos ----------

def _print_summary(d: dict) -> None:
    if not d.get("ok"):
        print("Error:", d.get("error"))
        return
    when = datetime.fromtimestamp(d["fetched_at"]).strftime("%d/%m %H:%M")
    print(f"Datos de las {when}" + (" (antiguos: " + d["error"] + ")" if d.get("stale") else ""))
    for a in d["accounts"]:
        b = a["balance"]
        saldo = f"{b['amount']:.2f} {b['currency']}" if b else "sin saldo"
        print(f"\n{a['name']} {a['masked']} [{a['currency']}]: {saldo}")
        for t in a["transactions"]:
            print(f"  {t['date']}  {t['amount']:>10.2f} {t['currency']}  {t['description']}")
    if d.get("consent_days") is not None:
        print(f"\nEl permiso caduca en {d['consent_days']} días.")


def _cli(args: list[str]) -> int:
    cmd = args[0] if args else ""
    try:
        if cmd == "auth":
            country = args[1] if len(args) > 1 else "ES"
            print("Abre este enlace, entra en Revolut y da permiso:\n\n" + start_auth("Revolut", country) + "\n")
            pasted = input("Pega aquí la URL completa a la que te ha llevado (aunque la página no cargue): ")
            s = finish_auth(pasted)
            print(f"Listo: {len(s['accounts'])} cuentas conectadas. Caduca en {consent_expires_in_days()} días.")
        elif cmd == "resumen":
            _print_summary(summary(force="--forzar" in args))
        elif cmd == "estado":
            print("Estado de la sesión:", session_status(), "· caduca en", consent_expires_in_days(), "días")
        else:
            print("Uso: python -m pepito.bank auth [PAÍS] | resumen [--forzar] | estado")
            return 2
    except BankError as e:
        print("Error:", e)
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(_cli(sys.argv[1:]))
