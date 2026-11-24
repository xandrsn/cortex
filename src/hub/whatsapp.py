"""WhatsApp: arranca el puente de whatsapp-mcp y lee sus mensajes para el panel y los avisos.

El puente (whatsapp-mcp/whatsapp-bridge, Go + whatsmeow) se vincula como «dispositivo» con un QR la primera vez
y guarda los mensajes en store/messages.db (SQLite). Aquí se lee esa base; solo se envía lo que el
usuario escribe en el panel, y solo si eligió «leer y escribir» (whatsapp_mode).
El QR que pide al vincular se escribe en store/qr.txt y el panel lo pinta para escanearlo.
"""

import logging
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx
import psutil
import yaml

from .config import ROOT, settings

log = logging.getLogger("whatsapp")

BRIDGE_DIR = ROOT / "whatsapp-mcp" / "whatsapp-bridge"
BRIDGE_EXE = BRIDGE_DIR / "whatsapp-bridge.exe"
STORE = BRIDGE_DIR / "store"
BRIDGE_PORT = 8080
BRIDGE_API = f"http://127.0.0.1:{BRIDGE_PORT}/api"
LOG_FILE = ROOT / "logs" / "whatsapp-bridge.log"
MOTES_FILE = lambda: Path(settings["data_dir"]) / "motes.yaml"
PREFS_FILE = lambda: Path(settings["data_dir"]) / "whatsapp.yaml"   # qué chats avisan
# estados («status@broadcast») y canales no son chats: ni en la lista ni avisos
REAL_CHAT = "chat_jid NOT LIKE '%@broadcast' AND chat_jid NOT LIKE '%@newsletter'"
FRESH = 15 * 60   # lo más viejo que se avisa (lo que llega al sincronizar el historial no se anuncia)


MEDIA = {"sticker": "Sticker", "image": "📷 Foto", "video": "🎥 Vídeo", "audio": "🎤 Audio", "document": "📄 Documento"}


def _media(kind: str | None) -> str:
    return MEDIA.get(kind or "", "Adjunto") if kind else ""


def _ts(value) -> float:
    """«2026-10-27 18:30:00.123456789+01:00» → epoch (Python solo admite 6 decimales)."""
    if not value:
        return 0.0
    s = str(value).replace("T", " ")
    head, _, rest = s.partition(".")
    if rest:
        digits = len(rest) - len(rest.lstrip("0123456789"))
        s = head + "." + rest[:min(digits, 6)] + rest[digits:]
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return 0.0


class WhatsApp:
    def __init__(self):
        self.available = BRIDGE_EXE.exists()
        self.state = "off"           # off · starting · qr · connected · error
        self._stop = False
        self._started = False
        self._last_rowid: int | None = None   # None = primera lectura: lo que ya hay no se anuncia
        self.chats: list[dict] = []
        self._contact_names: dict[str, str] = {}
        self._contacts_at = 0.0
        self._lid_pn: dict[str, str] = {}
        # shortcut: el puente no guarda lo que envía, así que se ve hasta reiniciar; guardarlo si se echa en falta
        self._sent: dict[str, list[dict]] = {}

    # ---------- puente ----------
    # El puente va por libre: si Cortex se reinicia, sigue conectado y Cortex se engancha a él al volver
    # (antes se reconectaba en cada reinicio y durante ese rato no llegaba nada).
    def start(self, link: bool = False):
        """Al arrancar solo se lanza si ya está vinculado; `link` = lo pide el usuario para vincularlo con el QR."""
        if self.available and not self._started and (link or (STORE / "whatsapp.db").exists()):
            self._started = True
            threading.Thread(target=self._supervise, name="whatsapp-bridge", daemon=True).start()

    def stop(self):
        self._stop = True   # el puente se queda funcionando

    def _bridge(self) -> psutil.Process | None:
        for p in psutil.process_iter(["name", "exe"]):
            try:
                if p.info["name"] == BRIDGE_EXE.name and Path(p.info["exe"] or "") == BRIDGE_EXE:
                    return p
            except (psutil.Error, OSError):
                continue
        return None

    def _read_state(self, line: str) -> None:
        if "Scan this QR" in line or "logged out" in line.lower():
            self.state = "qr"
        elif "Connected to WhatsApp" in line or "Successfully connected" in line:
            self.state = "connected"
        elif "Starting WhatsApp client" in line:
            self.state = "starting"
        elif self.state != "qr" and ("] ← " in line or "] → " in line or "Using existing chat name" in line):
            # le llegan mensajes: está conectado (al engancharse a un puente de hace días, la línea de
            # «Connected» ya no está entre las últimas y se quedaba en «sin conexión»)
            self.state = "connected"

    def _supervise(self):
        STORE.mkdir(parents=True, exist_ok=True)
        proc = self._bridge()
        if proc:
            log.info("Puente de WhatsApp ya en marcha (pid %s): me engancho", proc.pid)
            try:   # su estado, por lo último que dejó escrito
                for line in LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]:
                    self._read_state(line)
            except OSError:
                pass
        pos = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
        while not self._stop:
            if proc is None or not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                if proc is not None:
                    log.warning("El puente de WhatsApp se cerró; lo relanzo en 20 s")
                    self.state = "error"
                    time.sleep(20)
                self.state = "starting"
                (STORE / "qr.txt").unlink(missing_ok=True)
                if LOG_FILE.exists() and LOG_FILE.stat().st_size > 10_000_000:   # el historial lo llena rápido
                    LOG_FILE.replace(LOG_FILE.with_suffix(".log.1"))
                pos = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
                try:
                    with open(LOG_FILE, "a", encoding="utf-8") as out:
                        popen = subprocess.Popen([str(BRIDGE_EXE)], cwd=BRIDGE_DIR, stdin=subprocess.DEVNULL,
                                                 stdout=out, stderr=subprocess.STDOUT,
                                                 creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
                                                 | subprocess.CREATE_NEW_PROCESS_GROUP)
                    proc = psutil.Process(popen.pid)
                except (OSError, psutil.Error) as e:
                    log.warning("No pude arrancar el puente de WhatsApp: %s", e)
                    self.state = "error"
                    self._started = False
                    return
                log.info("Puente de WhatsApp arrancado")
            try:   # lo nuevo del registro del puente dice en qué estado está
                with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    for line in f:
                        self._read_state(line)
                    pos = f.tell()
            except OSError:
                pass
            time.sleep(2)

    @property
    def qr(self) -> str | None:
        try:
            if self.state == "qr" or self.state == "starting":
                return (STORE / "qr.txt").read_text(encoding="utf-8").strip() or None
        except OSError:
            pass
        return None

    # ---------- nombres y motes ----------
    def _contacts(self) -> dict[str, str]:
        """Número o LID (sin @…) → nombre de la agenda. Se rehace cada 5 minutos."""
        if time.monotonic() - self._contacts_at < 300 and self._contact_names:
            return self._contact_names
        names: dict[str, str] = {}
        path = STORE / "whatsapp.db"
        if path.exists():
            con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
            try:
                for jid, full, first, push, biz in con.execute(
                        "SELECT their_jid, full_name, first_name, push_name, business_name FROM whatsmeow_contacts"):
                    name = full or first or push or biz
                    if name:
                        names[jid.split("@")[0]] = name
                for lid, pn in con.execute("SELECT lid, pn FROM whatsmeow_lid_map"):
                    self._lid_pn[lid] = pn
                    if lid not in names and pn in names:
                        names[lid] = names[pn]
                    elif pn not in names and lid in names:
                        names[pn] = names[lid]
            except sqlite3.Error as e:
                log.warning("WhatsApp: no pude leer la agenda: %s", e)
            finally:
                con.close()
        self._contact_names, self._contacts_at = names, time.monotonic()
        return names

    # ---------- silenciar ----------
    def prefs(self) -> dict:
        try:
            data = yaml.safe_load(PREFS_FILE().read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            data = {}
        return {"silenciados": dict(data.get("silenciados") or {}), "avisan": dict(data.get("avisan") or {})}

    def muted(self, jid: str, prefs: dict | None = None) -> bool:
        """Los grupos callan salvo que estén en «avisan»; los chats individuales avisan salvo en «silenciados»."""
        p = prefs or self.prefs()
        return jid not in p["avisan"] if jid.endswith("@g.us") else jid in p["silenciados"]

    def set_muted(self, jid: str, name: str, muted: bool) -> None:
        p = self.prefs()
        p["silenciados"].pop(jid, None)
        p["avisan"].pop(jid, None)
        if jid.endswith("@g.us") and not muted:
            p["avisan"][jid] = name
        elif not jid.endswith("@g.us") and muted:
            p["silenciados"][jid] = name
        text = ("# WhatsApp: qué chats avisan. Los grupos están callados salvo los de «avisan»;\n"
                "# los chats individuales avisan salvo los de «silenciados». (identificador: nombre)\n")
        text += yaml.safe_dump(p, allow_unicode=True, sort_keys=True, default_flow_style=False)
        PREFS_FILE().write_text(text, encoding="utf-8")
        for c in self.chats:
            if c["jid"] == jid:
                c["muted"] = muted

    def motes(self) -> dict[str, str]:
        """Nombre de WhatsApp → cómo lo dice Cortex (motes.yaml, en la carpeta de datos)."""
        try:
            data = yaml.safe_load(MOTES_FILE().read_text(encoding="utf-8")) or {}
            return {str(k): str(v) for k, v in data.items() if v}
        except (OSError, yaml.YAMLError, AttributeError):
            return {}

    def set_mote(self, name: str, mote: str) -> None:
        motes = self.motes()
        if mote.strip() and mote.strip() != name:
            motes[name] = mote.strip()
        else:
            motes.pop(name, None)
        text = "# Motes de WhatsApp: cómo dice Cortex a cada contacto («Nombre en WhatsApp: mote»).\n"
        text += yaml.safe_dump(motes, allow_unicode=True, sort_keys=True, default_flow_style=False) if motes else ""
        MOTES_FILE().write_text(text, encoding="utf-8")

    def _namer(self, con: sqlite3.Connection):
        chat_names = {r["jid"]: r["name"] for r in con.execute("SELECT jid, name FROM chats")}
        contacts, motes = self._contacts(), self.motes()

        def real(jid: str) -> str:
            user = jid.split("@")[0]
            if not jid.endswith("@g.us") and "@" not in jid:
                jid = f"{jid}@s.whatsapp.net"
            name = chat_names.get(jid)
            if not name or name.lstrip("+").isdigit():   # sin nombre o con el número/ID interno de WhatsApp
                pn = self._lid_pn.get(user, "" if jid.endswith("@lid") else user)   # un LID sin @ (remitente de grupo) también
                name = contacts.get(user) or contacts.get(pn) or ("+" + pn if pn else "Alguien")
            return name

        def show(jid: str) -> tuple[str, str]:   # (como se dice, nombre real)
            name = real(jid)
            return motes.get(name, name), name
        return show

    # ---------- leer ----------
    def _db(self) -> sqlite3.Connection | None:
        path = STORE / "messages.db"
        if not path.exists():
            return None
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        return con

    def check(self) -> list[dict]:
        """Refresca los chats recientes y devuelve los mensajes recibidos nuevos desde la última vez."""
        con = self._db()
        if con is None:
            return []
        try:
            show = self._namer(con)
            prefs = self.prefs()
            chats = []
            for c in con.execute(f"""SELECT m.chat_jid AS jid, m.content AS text, m.media_type AS media, m.sender,
                                           m.is_from_me AS mine, MAX(m.timestamp) AS ts
                                    FROM messages m WHERE {REAL_CHAT} GROUP BY m.chat_jid ORDER BY ts DESC LIMIT 14"""):
                group = c["jid"].endswith("@g.us")
                name, real = show(c["jid"])
                chats.append({"jid": c["jid"], "name": name, "real": real, "group": group, "time": _ts(c["ts"]),
                              "mine": bool(c["mine"]), "text": c["text"] or _media(c["media"]),
                              "from": "" if c["mine"] or not group else show(c["sender"])[0],
                              "muted": self.muted(c["jid"], prefs)})
            self.chats = chats

            top = con.execute("SELECT MAX(rowid) FROM messages").fetchone()[0] or 0
            if self._last_rowid is None:
                self._last_rowid = top
                return []
            new, now = [], time.time()
            # un mensaje que entre entre las dos consultas sale aquí: se apunta su rowid para no repetirlo luego
            for m in con.execute(f"""SELECT rowid, id, chat_jid, sender, content, media_type, timestamp FROM messages
                                    WHERE rowid > ? AND is_from_me = 0 AND {REAL_CHAT} ORDER BY rowid""", (self._last_rowid,)):
                top = max(top, m["rowid"])
                if now - _ts(m["timestamp"]) > FRESH:
                    continue
                new.append({"id": m["id"], "jid": m["chat_jid"], "media": m["media_type"] or "",
                            "chat": show(m["chat_jid"])[0], "group": m["chat_jid"].endswith("@g.us"),
                            "muted": self.muted(m["chat_jid"], prefs),
                            "from": show(m["sender"])[0], "text": m["content"] or _media(m["media_type"])})
            self._last_rowid = top
            return new
        except sqlite3.Error as e:
            log.warning("WhatsApp: no pude leer los mensajes: %s", e)
            return []
        finally:
            con.close()

    def messages(self, jid: str, limit: int = 60) -> dict:
        """Los últimos mensajes de un chat, del más viejo al más nuevo."""
        con = self._db()
        if con is None:
            return {"jid": jid, "messages": []}
        try:
            show = self._namer(con)
            rows = con.execute("""SELECT id, sender, content, media_type, is_from_me, timestamp FROM messages
                                  WHERE chat_jid = ? ORDER BY timestamp DESC LIMIT ?""", (jid, limit)).fetchall()
            name, real = show(jid)
            return {"jid": jid, "name": name, "real": real, "group": jid.endswith("@g.us"), "muted": self.muted(jid),
                    "messages": sorted([
                {"id": r["id"], "media": r["media_type"] or "", "caption": r["content"] or "",
                 "from": "" if r["is_from_me"] else show(r["sender"])[0], "mine": bool(r["is_from_me"]),
                 "text": r["content"] or _media(r["media_type"]), "time": _ts(r["timestamp"])}
                for r in reversed(rows)] + self._sent.get(jid, []), key=lambda m: m["time"])}
        finally:
            con.close()

    def media_file(self, jid: str, msg_id: str) -> Path:
        """Descarga (el puente la guarda en store/<chat>/) una nota de voz, foto… y devuelve dónde está. Solo
        leer: es lo mismo que hace WhatsApp al abrir el mensaje."""
        r = httpx.post(f"{BRIDGE_API}/download", json={"message_id": msg_id, "chat_jid": jid}, timeout=60)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if not data.get("success"):
            raise RuntimeError(data.get("message") or f"el puente dijo {r.status_code}")
        path = Path(data["path"]).resolve()
        if STORE.resolve() not in path.parents:   # solo ficheros de su carpeta
            raise RuntimeError("ruta inesperada")
        return path

    def _owns_port(self) -> bool:
        """¿Escucha en el puerto el puente de Cortex? Puede haber otro, vinculado a otra cuenta."""
        proc = self._bridge()
        try:
            return bool(proc) and any(c.laddr and c.laddr.port == BRIDGE_PORT and c.status == psutil.CONN_LISTEN
                                      for c in proc.net_connections("tcp"))
        except psutil.Error:
            return False

    def send(self, jid: str, text: str) -> str:
        """Envía un texto a un chat de la lista. Devuelve el error, o "" si salió."""
        text = text.strip()
        if settings["whatsapp_mode"] != "write":
            return "WhatsApp está en solo lectura (Ajustes → Cerebro)."
        if not any(c["jid"] == jid for c in self.chats):
            return "Ese chat no está en la lista."
        if not 1 <= len(text) <= 4000:
            return "El mensaje tiene que tener entre 1 y 4000 letras."
        if self.state != "connected" or not self._owns_port():
            return "WhatsApp no está conectado."
        try:
            r = httpx.post(f"{BRIDGE_API}/send", json={"recipient": jid, "message": text}, timeout=30)
            ok = r.json().get("success") is True
        except httpx.TimeoutException:
            return "WhatsApp no ha contestado a tiempo: mira en el móvil si ha salido antes de repetirlo."
        except (httpx.HTTPError, ValueError, AttributeError):
            ok = False
        if not ok:
            return "No se ha podido enviar."
        now = time.time()
        self._sent.setdefault(jid, []).append({"id": f"sent-{now}", "media": "", "caption": "", "from": "", "mine": True,
                                               "text": text, "time": now})
        return ""

    def summary(self) -> dict:
        return {"available": self.available, "state": self.state, "qr": self.qr, "chats": self.chats}
