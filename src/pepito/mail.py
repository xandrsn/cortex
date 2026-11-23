"""Correo de Gmail (dos cuentas: principal y secundaria): avisa de los nuevos, los lee, envía y responde.

Por IMAP (leer) y SMTP (enviar) con una «contraseña de aplicación» por cuenta, en .env (nunca en Drive):
  GMAIL_USER / GMAIL_APP_PASSWORD          → principal (desde aquí se envía si no se dice otra)
  GMAIL_USER_2 / GMAIL_APP_PASSWORD_2      → secundaria
Se crean en myaccount.google.com → Seguridad → Contraseñas de aplicaciones (hace falta la verificación en dos
pasos). El buzón se abre en solo lectura y con PEEK: mirar no marca nada como leído.
"""

import email
import email.header
import email.message
import email.utils
import imaplib
import json
import logging
import os
import re
import smtplib
import threading
import time

from .config import ROOT

log = logging.getLogger("mail")

SEEN_FILE = ROOT / "logs" / "mail_seen.json"
HOST = "imap.gmail.com"
SMTP_HOST = "smtp.gmail.com"


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = []
    for text, charset in email.header.decode_header(value):
        if isinstance(text, bytes):
            try:
                text = text.decode(charset or "utf-8", errors="replace")
            except LookupError:
                text = text.decode("utf-8", errors="replace")
        parts.append(text)
    return " ".join("".join(parts).split())


def _text_of(msg: email.message.Message) -> str:
    """Texto plano del correo (o el HTML sin etiquetas si no hay otro)."""
    plain, html = None, None
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        try:
            body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if part.get_content_type() == "text/plain" and plain is None:
            plain = body
        elif part.get_content_type() == "text/html" and html is None:
            html = body
    if plain is None and html:
        plain = re.sub(r"<(style|script).*?</\1>", " ", html, flags=re.S | re.I)
        plain = re.sub(r"<[^>]+>", " ", plain)
    return re.sub(r"\s+\n", "\n", re.sub(r"[ \t]+", " ", plain or "")).strip()


def _payload(part: email.message.Message) -> bytes:
    try:
        return part.get_payload(decode=True) or b""
    except Exception:
        return b""


def _parts(msg: email.message.Message) -> list[email.message.Message]:
    """Las partes «hoja» del correo, en orden: su índice es el n de /api/mail/part."""
    return [p for p in (msg.walk() if msg.is_multipart() else [msg]) if p.get_content_maintype() != "multipart"]


def _filename(part: email.message.Message) -> str:
    return _decode(part.get_filename() or "") or ""


class Account:
    def __init__(self, key: str, label: str, user: str, password: str):
        self.key, self.label, self.user, self._password = key, label, user, password

    def imap(self) -> imaplib.IMAP4_SSL:
        box = imaplib.IMAP4_SSL(HOST, timeout=20)
        box.login(self.user, self._password)
        box.select("INBOX", readonly=True)
        return box

    def send(self, msg: email.message.EmailMessage) -> None:
        with smtplib.SMTP_SSL(SMTP_HOST, 465, timeout=30) as s:
            s.login(self.user, self._password)
            s.send_message(msg)


SLOTS = (("p", "principal", ""), ("s", "secundaria", "_2"))
ENV_FILE = ROOT / ".env"


def test_login(user: str, password: str) -> str | None:
    """Prueba usuario + contraseña de aplicación contra Gmail. None si entra; si no, el motivo en castellano."""
    try:
        box = imaplib.IMAP4_SSL(HOST, timeout=20)
        try:
            box.login(user, password)
        finally:
            try:
                box.logout()
            except Exception:
                pass
    except imaplib.IMAP4.error as e:
        msg = str(e)
        if "Application-specific password required" in msg:
            return "Google pide una contraseña de aplicación: esa es la contraseña normal de la cuenta."
        if "Invalid credentials" in msg or "AUTHENTICATIONFAILED" in msg:
            return "Google dice que el correo o la contraseña no son correctos."
        return f"Google no deja entrar: {msg[:120]}"
    except OSError:
        return "No hay conexión con Gmail."
    return None


def save_env(values: dict[str, str]) -> None:
    """Escribe/actualiza claves en .env (en la carpeta de código) sin tocar el resto de líneas."""
    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    left = dict(values)
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in left:
            lines[i] = f"{key}={left.pop(key)}"
    lines += [f"{k}={v}" for k, v in left.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ.update(values)


class Mail:
    def __init__(self):
        self.accounts: list[Account] = []
        self.load()
        self._lock = threading.Lock()
        self._headers: dict[str, dict] = {}   # id → cabecera ya leída (no se vuelve a pedir cada minuto)
        self._opened: dict[str, tuple] = {}   # id → (cuenta, mensaje): el último par abierto en el panel (fotos, adjuntos)
        self.unread: list[dict] = []
        self.error = ""
        self.checked_at = 0.0
        try:
            self._seen = set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            self._seen = None   # primera vez: lo que ya hay no se anuncia

    def load(self):
        """(Re)lee las cuentas del entorno: así una cuenta conectada desde el panel vale sin reiniciar."""
        self.accounts = []
        for key, label, suffix in SLOTS:
            user = os.environ.get(f"GMAIL_USER{suffix}", "").strip()
            pw = os.environ.get(f"GMAIL_APP_PASSWORD{suffix}", "").replace(" ", "").strip()
            if user and pw:
                self.accounts.append(Account(key, label, user, pw))

    def connect(self, slot: str, user: str, password: str) -> str | None:
        """Desde el panel: prueba la cuenta y, si entra, la guarda en .env. Devuelve el error o None."""
        suffix = next((s for k, _, s in SLOTS if k == slot), None)
        user, password = user.strip(), "".join(password.split())
        if suffix is None or "@" not in user or not password:
            return "Falta el correo o la contraseña."
        err = test_login(user, password)
        if err:
            if len(password) != 16 or not password.isalpha():
                err += " Las contraseñas de aplicación son 16 letras (abcd efgh ijkl mnop)."
            return err
        save_env({f"GMAIL_USER{suffix}": user, f"GMAIL_APP_PASSWORD{suffix}": password})
        self.load()
        log.info("Cuenta de correo conectada desde el panel: %s", user)
        return None

    @property
    def configured(self) -> bool:
        return bool(self.accounts)

    def _account(self, key: str | None) -> Account:
        if not self.accounts:
            raise RuntimeError("El correo no está configurado (faltan las contraseñas de aplicación en .env)")
        k = (key or "").lower()
        for a in self.accounts:   # primero la letra exacta («s» también está dentro de «charlieandersen»)
            if k == a.key:
                return a
        for a in self.accounts:
            if k in (a.key, a.label, a.user.lower()) or (k and k in a.user.lower()):
                return a
        return self.accounts[0]

    # ---------- leer ----------
    def check(self) -> list[dict]:
        """Actualiza la lista de no leídos (de todas las cuentas) y devuelve los nuevos desde la última vez."""
        if not self.configured:
            return []
        items, errors = [], []
        for acc in self.accounts:
            try:
                box = acc.imap()
                try:
                    _, data = box.uid("search", None, "UNSEEN")
                    uids = [u.decode() for u in (data[0] or b"").split()[-15:]]
                    # las cabeceras ya vistas se reutilizan; las nuevas se piden todas en una sola orden
                    missing = [u for u in uids if f"{acc.key}{u}" not in self._headers]
                    if missing:
                        _, msg = box.uid("fetch", ",".join(missing), "(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                        for part in msg:
                            m = isinstance(part, tuple) and re.search(rb"UID (\d+)", part[0])
                            if not m:
                                continue
                            uid, h = m.group(1).decode(), email.message_from_bytes(part[1])
                            name, addr = email.utils.parseaddr(_decode(h.get("From")))
                            try:
                                when = email.utils.parsedate_to_datetime(h.get("Date")).timestamp()
                            except (TypeError, ValueError):
                                when = 0
                            self._headers[f"{acc.key}{uid}"] = {
                                "id": f"{acc.key}{uid}", "uid": uid, "account": acc.label, "to": acc.user,
                                "from": name or addr, "addr": addr,
                                "subject": _decode(h.get("Subject")) or "(sin asunto)", "time": when}
                    items += [self._headers[f"{acc.key}{u}"] for u in uids if f"{acc.key}{u}" in self._headers]
                finally:
                    try:
                        box.logout()
                    except Exception:
                        pass
            except (imaplib.IMAP4.error, OSError) as e:
                # IMAP4.abort (conexión cortada, p. ej. al volver de suspensión) también es IMAP4.error: no es la contraseña
                bad_login = isinstance(e, imaplib.IMAP4.error) and not isinstance(e, imaplib.IMAP4.abort)
                errors.append(f"{acc.user}: " + ("revisa la contraseña de aplicación" if bad_login else "sin conexión"))
                log.warning("Correo %s: %s", acc.user, e)
                # mientras tanto se quedan los que ya se sabían de esa cuenta (no desaparecen del panel)
                with self._lock:
                    items += [i for i in self.unread if i["id"].startswith(acc.key)]
        self.error = " · ".join(errors)
        items.sort(key=lambda i: i["time"], reverse=True)
        live = {i["id"] for i in items}
        self._headers = {k: v for k, v in self._headers.items() if k in live}   # leídos o borrados: fuera
        with self._lock:
            self.unread = items
            self.checked_at = time.time()
            first = self._seen is None
            seen = self._seen or set()
            new = [] if first else [i for i in items if i["id"] not in seen]
            # solo hace falta recordar los que siguen sin leer (los leídos ya no vuelven a la lista)
            self._seen = live if not errors else seen | live
            try:
                SEEN_FILE.write_text(json.dumps(sorted(self._seen)), encoding="utf-8")
            except OSError:
                pass
        return new

    def recent(self, hours: float = 12, limit: int = 25) -> list[dict]:
        """Correos recibidos en las últimas `hours` horas (leídos o no), de todas las cuentas; lo más nuevo primero."""
        since = time.time() - hours * 3600
        day = time.strftime("%d-%b-%Y", time.localtime(since))   # IMAP solo filtra por día; la hora se mira después
        out = []
        for acc in self.accounts:
            try:
                box = acc.imap()
                try:
                    _, data = box.uid("search", None, f"(SINCE {day})")
                    uids = (data[0] or b"").split()[-limit:]
                    if not uids:
                        continue
                    _, msg = box.uid("fetch", b",".join(uids), "(UID FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                    for part in msg:
                        m = isinstance(part, tuple) and re.search(rb"UID (\d+)", part[0])
                        if not m:
                            continue
                        h = email.message_from_bytes(part[1])
                        try:
                            when = email.utils.parsedate_to_datetime(h.get("Date")).timestamp()
                        except (TypeError, ValueError):
                            continue
                        if when < since:
                            continue
                        name, addr = email.utils.parseaddr(_decode(h.get("From")))
                        out.append({"id": f"{acc.key}{m.group(1).decode()}", "account": acc.label, "from": name or addr,
                                    "subject": _decode(h.get("Subject")) or "(sin asunto)", "time": when,
                                    "read": b"\\Seen" in part[0]})
                finally:
                    try:
                        box.logout()
                    except Exception:
                        pass
            except (imaplib.IMAP4.error, OSError) as e:
                log.warning("Correo %s: %s", acc.user, e)
        return sorted(out, key=lambda i: i["time"], reverse=True)[:limit]

    def _fetch(self, mail_id: str) -> tuple[Account, email.message.Message]:
        acc = self._account(mail_id[:1])
        uid = mail_id[1:]
        box = acc.imap()
        try:
            _, msg = box.uid("fetch", uid.encode(), "(BODY.PEEK[])")
            raw = next((p[1] for p in msg if isinstance(p, tuple)), None)
        finally:
            try:
                box.logout()
            except Exception:
                pass
        if not raw:
            raise RuntimeError("No encuentro ese correo")
        return acc, email.message_from_bytes(raw)

    def _cached(self, mail_id: str) -> tuple[Account, email.message.Message]:
        with self._lock:
            hit = self._opened.get(mail_id)
        if hit:
            return hit
        hit = self._fetch(mail_id)
        with self._lock:
            self._opened[mail_id] = hit
            while len(self._opened) > 6:
                self._opened.pop(next(iter(self._opened)))
        return hit

    def open(self, mail_id: str) -> dict:
        """Para el lector del panel: cabecera y adjuntos (el cuerpo va aparte, en html())."""
        acc, m = self._cached(mail_id)
        files = []
        for n, p in enumerate(_parts(m)):
            name = _filename(p)
            inline = p.get("Content-ID") and not p.get("Content-Disposition", "").lower().startswith("attachment")
            if name and not inline:
                files.append({"n": n, "name": name, "type": p.get_content_type(), "size": len(_payload(p))})
        return {"id": mail_id, "account": acc.label, "from": _decode(m.get("From")), "to": _decode(m.get("To")),
                "subject": _decode(m.get("Subject")), "date": m.get("Date", ""), "files": files}

    def html(self, mail_id: str) -> str:
        """El cuerpo como página: el HTML del correo (fotos incrustadas incluidas) o el texto, sin scripts."""
        _, m = self._cached(mail_id)
        parts = _parts(m)
        html = plain = None
        cids = {}
        for n, p in enumerate(parts):
            if p.get("Content-ID"):
                cids[p["Content-ID"].strip("<> ")] = n
            if _filename(p) and p.get("Content-Disposition", "").lower().startswith("attachment"):
                continue
            kind = p.get_content_type()
            if kind in ("text/html", "text/plain") and (html if kind == "text/html" else plain) is None:
                body = _payload(p).decode(p.get_content_charset() or "utf-8", errors="replace")
                if kind == "text/html":
                    html = body
                else:
                    plain = body
        if html is None:
            import html as _h
            text = _h.escape(plain or "(sin texto)")
            text = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', text)
            html = f'<pre style="white-space:pre-wrap;font:15px/1.5 system-ui,sans-serif;margin:0">{text}</pre>'
        url = "/api/mail/part?id=" + mail_id + "&n="
        html = re.sub(r"""(["'(])cid:([^"')\s]+)""", lambda g: g[1] + (url + str(cids[g[2]]) if g[2] in cids else "about:blank"), html, flags=re.I)
        html = re.sub(r"<script\b.*?</script\s*>", "", html, flags=re.S | re.I)
        head = ('<meta charset="utf-8"><base target="_blank"><meta name="referrer" content="no-referrer">'
                '<style>html{background:#fff;color:#111}body{margin:12px;font:15px/1.5 system-ui,sans-serif;'
                'overflow-wrap:anywhere}img{max-width:100%;height:auto}</style>')
        return "<!doctype html>" + head + html

    def part(self, mail_id: str, n: int) -> tuple[bytes, str, str]:
        """Un adjunto o foto del correo: (datos, tipo, nombre)."""
        _, m = self._cached(mail_id)
        p = _parts(m)[n]
        return _payload(p), p.get_content_type(), _filename(p) or f"adjunto-{n}"

    def read(self, mail_id: str, limit: int = 4000) -> dict:
        """Remitente, asunto y texto de un correo (sin marcarlo como leído)."""
        acc, m = self._fetch(mail_id)
        return {"id": mail_id, "account": acc.label, "from": _decode(m.get("From")), "subject": _decode(m.get("Subject")),
                "date": m.get("Date", ""), "text": _text_of(m)[:limit]}

    # ---------- enviar ----------
    def send(self, to: str, subject: str, body: str, account: str | None = None) -> str:
        acc = self._account(account)
        msg = email.message.EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = acc.user, to, subject
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid(domain=acc.user.split("@")[-1])
        msg.set_content(body)
        acc.send(msg)
        log.info("Correo enviado desde %s a %s: %s", acc.user, to, subject)
        return f"Enviado a {to} desde {acc.user}."

    def reply(self, mail_id: str, body: str) -> str:
        """Responde a un correo desde la cuenta que lo recibió, en el mismo hilo."""
        acc, orig = self._fetch(mail_id)
        to = orig.get("Reply-To") or orig.get("From")
        subject = _decode(orig.get("Subject"))
        msg = email.message.EmailMessage()
        msg["From"], msg["To"] = acc.user, to
        msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid(domain=acc.user.split("@")[-1])
        if orig.get("Message-ID"):
            msg["In-Reply-To"] = orig["Message-ID"]
            msg["References"] = f"{orig.get('References', '')} {orig['Message-ID']}".strip()
        msg.set_content(body)
        acc.send(msg)
        log.info("Respuesta enviada desde %s a %s: %s", acc.user, to, msg["Subject"])
        return f"Respondido a {email.utils.parseaddr(_decode(to))[1] or to}."

    def summary(self) -> dict:
        return {"configured": self.configured, "accounts": [{"label": a.label, "user": a.user} for a in self.accounts],
                "slots": [{"slot": k, "label": label, "user": os.environ.get(f"GMAIL_USER{s}", "").strip(),
                           "ok": any(a.key == k for a in self.accounts) and f"{os.environ.get(f'GMAIL_USER{s}', '')}:"
                           not in self.error} for k, label, s in SLOTS],
                "unread": self.unread, "error": self.error, "checked_at": self.checked_at}
