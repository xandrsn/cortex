"""Revolut balance and transactions via Enable Banking (PSD2, READ ONLY).

Only query endpoints are used: GET /aspsps, POST /auth, POST /sessions, GET /sessions/{id},
GET /accounts/{uid}/balances and GET /accounts/{uid}/transactions. No payments.

Secrets (never in Drive): EB_APP_ID and EB_KEY_PATH (the <app id>.pem) in .env; EB_REDIRECT_URL optional;
the session in secrets/bank_session.json.
Banks only allow ~4 queries a day without the user present, so results are cached for 6 h and, even if a
refresh is requested, the bank is never queried more than once every 10 min.

Manual use (from the program folder's src directory):  python -m hub.bank auth | summary [--force] | status
(Spanish aliases still work: resumen [--forzar] | estado)
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
CACHE_FILE = SECRETS / "bank_cache.json"     # bank data too: kept out of Drive
DEFAULT_KEY = SECRETS / "enablebanking.pem"
DEFAULT_REDIRECT = "https://localhost:8765/bank/callback"   # the one registered in the Enable Banking app

CACHE_SECONDS = 6 * 3600
MIN_REFRESH_SECONDS = 10 * 60
# Preferred balance types (ISO 20022): available -> booked -> whatever exists
BALANCE_ORDER = ["CLAV", "ITAV", "XPCD", "CLBD", "ITBD", "OPAV", "OPBD"]

# Allow-list of calls: anything else (e.g. /payments) is rejected
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
    """Error with a message ready to show to the user."""


# ---------- credentials and calls ----------

def _app_id() -> str:
    app_id = os.environ.get("EB_APP_ID", "").strip()
    if not app_id:
        raise BankError("EB_APP_ID is missing from .env (the Enable Banking application ID).")
    return app_id


def _key() -> bytes:
    path = Path(os.environ.get("EB_KEY_PATH", "").strip() or DEFAULT_KEY)
    if not path.exists():   # e.g. "\a" in a double-quoted .env eats the backslash: try <app id>.pem
        path = SECRETS / f"{_app_id()}.pem"
    if not path.exists():
        raise BankError(f"Cannot find the Enable Banking private key at {path}.")
    return path.read_bytes()


def _jwt() -> str:
    """RS256 JWT signed with the app key; reused until 5 min before it expires (1 h)."""
    now = int(time.time())
    if _token.get("exp", 0) - now > 300:
        return _token["jwt"]
    body = {"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": now, "exp": now + 3600}
    try:
        token = jwt.encode(body, _key(), algorithm="RS256", headers={"kid": _app_id()})
    except BankError:
        raise
    except Exception as e:
        raise BankError(f"The private key cannot be used to sign: {e}") from e
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
        raise BankError(f"Call not allowed (read-only): {method} {path}")
    try:
        r = httpx.request(method, API + path, timeout=30,
                          headers={"Authorization": f"Bearer {_jwt()}"}, **kw)
    except httpx.HTTPError as e:
        raise BankError(f"Could not connect to Enable Banking: {e}") from e
    if r.status_code < 300:
        return r.json()
    detail = _error_text(r)
    log.warning("%s %s → %s %s", method, path, r.status_code, detail)
    if "EXPIRED_SESSION" in detail or "CLOSED_SESSION" in detail or "REVOKED" in detail:
        raise BankError("The permission with the bank has expired or been revoked. It must be authorized again.")
    if r.status_code == 429:
        raise BankError("The bank will not allow more queries right now (daily limit). Try again in a few hours.")
    if r.status_code == 401:
        raise BankError("Enable Banking does not accept the credentials (check EB_APP_ID and the .pem key).")
    if r.status_code == 403:
        raise BankError("Enable Banking denies access (is the app activated and the account linked?).")
    raise BankError(f"Enable Banking error ({r.status_code}): {detail}")


# ---------- state files ----------

def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("Could not read %s: %s", path.name, e)
        return {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------- authorization ----------

def _max_consent_seconds(aspsp_name: str, country: str) -> int:
    """Maximum consent validity for that bank (180 days for Revolut)."""
    for a in _call("GET", "/aspsps", params={"country": country}).get("aspsps", []):
        if a.get("name", "").lower() == aspsp_name.lower():
            return int(a.get("maximum_consent_validity") or 90 * 86400)
    raise BankError(f"Enable Banking does not have '{aspsp_name}' in country {country}.")


def start_auth(aspsp_name: str = "Revolut", country: str = "ES", redirect_url: str | None = None) -> str:
    """Ask Enable Banking for the consent link and return it. Saves the 'state' to check it later."""
    redirect_url = redirect_url or os.environ.get("EB_REDIRECT_URL", "").strip() or DEFAULT_REDIRECT
    seconds = _max_consent_seconds(aspsp_name, country) - 3600   # margin for clock skew
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
    log.info("Consent requested for %s (%s)", aspsp_name, country)
    return d["url"]


def _mask(account: dict) -> str:
    ids = account.get("account_id") or {}
    iban = ids.get("iban") or (ids.get("other") or {}).get("identification") or ""
    return f"···{iban[-4:]}" if iban else ""


def finish_auth(code: str) -> dict:
    """Take the 'code' (or the whole URL the bank sent you to), create the session and save it."""
    code = code.strip()
    state = None
    if code.startswith("http"):
        q = parse_qs(urlparse(code).query)
        if "error" in q:
            raise BankError(f"The bank did not grant permission: {q.get('error_description', q['error'])[0]}")
        state = (q.get("state") or [None])[0]
        code = (q.get("code") or [""])[0]
    if not code:
        raise BankError("I cannot see the 'code' in what you pasted.")
    data = _load(SESSION_FILE)
    pending = data.get("pending") or {}
    if state and pending.get("state") and state != pending["state"]:
        raise BankError("The 'state' does not match the request; start the authorization again.")
    s = _call("POST", "/sessions", json={"code": code})
    accounts = [{"uid": a.get("uid"), "name": a.get("name") or a.get("product") or "Account",
                 "currency": a.get("currency"), "masked": _mask(a)}
                for a in s.get("accounts", []) if a.get("uid")]
    session = {"session_id": s["session_id"], "valid_until": (s.get("access") or {}).get("valid_until"),
               "aspsp": s.get("aspsp") or {"name": pending.get("aspsp"), "country": pending.get("country")},
               "accounts": accounts, "created": time.time()}
    _save(SESSION_FILE, {"session": session})
    CACHE_FILE.unlink(missing_ok=True)
    log.info("Bank session created with %d accounts", len(accounts))
    return session


def _session() -> dict:
    s = _load(SESSION_FILE).get("session")
    if not s:
        raise BankError("No bank is connected yet. Run: python -m hub.bank auth")
    return s


def consent_expires_in_days() -> int | None:
    """Days left on the bank consent (None if there is no session). Negative = expired."""
    s = _load(SESSION_FILE).get("session") or {}
    if not s.get("valid_until"):
        return None
    until = datetime.fromisoformat(s["valid_until"].replace("Z", "+00:00"))
    return (until - datetime.now(timezone.utc)).days


def session_status() -> str:
    """Session status according to Enable Banking (AUTHORIZED, EXPIRED...). Costs one call: use sparingly."""
    return _call("GET", f"/sessions/{_session()['session_id']}").get("status", "?")


# ---------- balance and transactions ----------

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
    """Transactions since the 1st of last month (to compare months)."""
    first = date.today().replace(day=1)
    params = {"date_from": (first - timedelta(days=1)).replace(day=1).isoformat()}
    rows: list[dict] = []
    for _ in range(8):                       # few pages: each one counts as a call
        d = _call("GET", f"/accounts/{uid}/transactions", params=params)
        rows += [_tx_row(t) for t in d.get("transactions", [])]
        if not d.get("continuation_key"):
            break
        params["continuation_key"] = d["continuation_key"]
    rows.sort(key=lambda r: r["date"] or "", reverse=True)
    return rows


# ---------- spending ----------
# Categories by keywords in the description (first match wins). Anything unmatched goes to "otros".
# The category names are Spanish data values shown in the panel; keep them in sync with the UI.
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
# Not spending: money moved to your own vaults, investments or currency exchanges
NOT_SPENDING = re.compile(r"to investment|exchanged to|to savings|to pocket|vault|hucha|to eur|to usd", re.I)
_CAT_RE = [(name, re.compile(rx, re.I)) for name, rx in CATEGORIES]


def category(description: str) -> str:
    for name, rx in _CAT_RE:
        if rx.search(description or ""):
            return name
    return "otros"


def spending(rows: list[dict], today: date | None = None) -> dict:
    """Month spending (charges only), daily average, end-of-month projection, previous month and categories."""
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
                         "balance": _balance(a["uid"]), "transactions": rows})   # this month and the previous one
    return {"ok": True, "accounts": accounts, "fetched_at": time.time(),
            "consent_days": consent_expires_in_days()}


def summary(force: bool = False, n: int = 10) -> dict:
    """Balance per account and latest n transactions. Cached 6 h; with force, refreshes if at least 10 min have passed."""
    with _lock:
        cache = _load(CACHE_FILE)
        age = time.time() - cache.get("fetched_at", 0)
        if cache and (age < MIN_REFRESH_SECONDS or (age < CACHE_SECONDS and not force)):
            return _with_spending(cache)
        try:
            data = _fetch(n)
        except BankError as e:
            log.warning("Bank: %s", e)
            if cache:
                return _with_spending({**cache, "stale": True, "error": str(e)})
            return {"ok": False, "error": str(e)}
        _save(CACHE_FILE, data)
        return _with_spending(data)


def _with_spending(data: dict) -> dict:
    """Spending is computed on read (categories can change without spending bank queries)."""
    for a in data.get("accounts", []):
        a["spending"] = spending(a.get("transactions") or [])
    return data


# ---------- command line ----------

def _print_summary(d: dict) -> None:
    if not d.get("ok"):
        print("Error:", d.get("error"))
        return
    when = datetime.fromtimestamp(d["fetched_at"]).strftime("%d/%m %H:%M")
    print(f"Data from {when}" + (" (stale: " + d["error"] + ")" if d.get("stale") else ""))
    for a in d["accounts"]:
        b = a["balance"]
        saldo = f"{b['amount']:.2f} {b['currency']}" if b else "no balance"
        print(f"\n{a['name']} {a['masked']} [{a['currency']}]: {saldo}")
        for t in a["transactions"]:
            print(f"  {t['date']}  {t['amount']:>10.2f} {t['currency']}  {t['description']}")
    if d.get("consent_days") is not None:
        print(f"\nThe permission expires in {d['consent_days']} days.")


def _cli(args: list[str]) -> int:
    cmd = args[0] if args else ""
    try:
        if cmd == "auth":
            country = args[1] if len(args) > 1 else "ES"
            print("Open this link, sign in to Revolut and grant permission:\n\n" + start_auth("Revolut", country) + "\n")
            pasted = input("Paste the full URL you were taken to (even if the page does not load): ")
            s = finish_auth(pasted)
            print(f"Done: {len(s['accounts'])} accounts connected. Expires in {consent_expires_in_days()} days.")
        elif cmd in ("resumen", "summary"):
            _print_summary(summary(force="--forzar" in args or "--force" in args))
        elif cmd in ("estado", "status"):
            print("Session status:", session_status(), "· expires in", consent_expires_in_days(), "days")
        else:
            print("Usage: python -m hub.bank auth [COUNTRY] | summary [--force] | status  (Spanish aliases: resumen, --forzar, estado)")
            return 2
    except BankError as e:
        print("Error:", e)
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(_cli(sys.argv[1:]))
