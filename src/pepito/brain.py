"""Puente con Claude Code en modo headless (`claude -p`) usando el plan del usuario.

Un único proceso `claude -p --input-format stream-json` se queda abierto y precalentado; cada turno es
una línea JSON por stdin. Así no se paga el arranque de Claude Code en cada pregunta. La conversación
sigue en el mismo proceso hasta que pasan N minutos sin hablar o se pide una nueva; entonces se
arranca otro proceso limpio. Si el proceso muere, se relanza con --resume para no perder el hilo.
"""

import collections
import json
import logging
import queue
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from typing import Callable

from .config import ROOT, settings

PLAN_FILE = ROOT / "logs" / "claude_plan.json"   # último uso del plan: el panel lo enseña nada más arrancar

log = logging.getLogger("brain")

TOOLS = "Read,Edit,Write,Glob,Grep,WebSearch,WebFetch"
# conectores de la cuenta de claude.ai (Gmail y Calendar); con «dontAsk» lo que no esté aquí se deniega
CONNECTORS = "mcp__claude_ai_Gmail,mcp__claude_ai_Google_Calendar"
WA_MCP = ROOT / "whatsapp-mcp.json"   # servidor MCP de whatsapp-mcp para el cerebro
WA_TOOLS = ",".join(f"mcp__whatsapp__{t}" for t in ("search_contacts", "list_messages", "list_chats", "get_chat",
                    "get_direct_chat_by_contact", "get_contact_chats", "get_last_interaction", "get_message_context"))
BLOCKED = ("Bash,PowerShell,NotebookEdit,Agent,Task,CronCreate,CronDelete,RemoteTrigger,SendMessage,PushNotification,"
           "mcp__whatsapp__send_message,mcp__whatsapp__send_file,mcp__whatsapp__send_audio_message,mcp__whatsapp__download_media")


def _allowed() -> list[str]:
    if WA_MCP.exists():
        return ["--allowedTools", f"{TOOLS},{CONNECTORS},{WA_TOOLS}", "--mcp-config", str(WA_MCP)]
    return ["--allowedTools", f"{TOOLS},{CONNECTORS}"]
DAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


class BrainError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


class Brain:
    def __init__(self):
        self.session_id: str | None = None
        self.last_turn = 0.0
        self.turns = 0
        self.plan: dict = {}          # último rate_limit_event (uso del plan)
        try:
            self.plan = json.loads(PLAN_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        self.ok: bool | None = None   # None = sin probar todavía
        self._proc: subprocess.Popen | None = None
        self._model = None
        self._events: queue.Queue = queue.Queue()
        self._lock = threading.Lock()       # un turno a la vez
        self._proc_lock = threading.Lock()  # arrancar/parar el proceso
        self._interrupted = False

    # ---------- proceso ----------
    def _command(self, resume: str | None) -> list[str]:
        exe = shutil.which("claude")
        if not exe:
            raise BrainError("start", "No encuentro el ejecutable de Claude Code")
        model = settings["claude_model"]
        cmd = [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--include-partial-messages", "--model", model, *_allowed(), "--disallowedTools", BLOCKED,
               "--permission-mode", "dontAsk", "--setting-sources", "project,local"]
        if model != "haiku":
            cmd += ["--effort", "low"]
        if resume:
            cmd += ["--resume", resume]
        return cmd

    def start(self, resume: str | None = None) -> None:
        """Arranca (o rearranca) el proceso. Sin resume, la conversación empieza de cero."""
        with self._proc_lock:
            self._stop_locked()
            self._events = queue.Queue()
            self._model = settings["claude_model"]
            cmd = self._command(resume)
            self._proc = subprocess.Popen(
                cmd, cwd=settings["data_dir"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
            self.session_id = resume
            if not resume:
                self.turns = 0
            threading.Thread(target=self._reader, args=(self._proc, self._events), daemon=True).start()
            log.info("Claude arrancado (%s, %s)", self._model, "continúa" if resume else "conversación nueva")

    def _stop_locked(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.close()
                self._proc.wait(timeout=3)
            except Exception:
                self._proc.kill()
                try:
                    self._proc.wait(timeout=3)
                except Exception:
                    pass
        self._proc = None

    def stop(self) -> None:
        with self._proc_lock:
            self._stop_locked()

    @staticmethod
    def _reader(proc: subprocess.Popen, events: queue.Queue) -> None:
        # stderr se vacía a la vez en otro hilo: si se llenara su tubería (avisos repetidos), Claude se
        # quedaría bloqueado escribiendo y dejaría de contestar
        tail: collections.deque = collections.deque(maxlen=200)

        def drain():
            for line in proc.stderr:
                tail.append(line.decode("utf-8", "replace"))

        errors = threading.Thread(target=drain, daemon=True, name="claude-stderr")
        errors.start()
        for raw in proc.stdout:
            try:
                events.put(json.loads(raw))
            except json.JSONDecodeError:
                continue
        code = proc.wait()
        errors.join(timeout=5)
        events.put({"type": "_exit", "code": code, "stderr": "".join(tail).strip()})

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def new_conversation(self) -> None:
        self.start()

    def maintain(self) -> None:
        """Llamado de vez en cuando: conversación nueva tras el tiempo de inactividad, o si cambió el modelo."""
        if self._lock.locked():
            return
        idle = self.turns and time.time() - self.last_turn > settings["session_idle_minutes"] * 60
        if idle:
            log.info("Conversación nueva por inactividad")
            self.start()
        elif self._model != settings["claude_model"] or not self._alive():
            self.start(self.session_id)

    def cancel(self) -> None:
        if self._lock.locked() and self._alive():
            self._interrupted = True
            self._send({"type": "control_request", "request_id": uuid.uuid4().hex,
                        "request": {"subtype": "interrupt"}})

    def _send(self, obj: dict) -> None:
        try:
            self._proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            self._proc.stdin.flush()
        except (OSError, AttributeError) as e:
            raise BrainError("crash", f"Se ha caído la conexión con Claude: {e}") from e

    # ---------- turnos ----------
    def ask(self, text: str, lang: str, channel: str, on_delta: Callable[[str], None],
            on_tool: Callable[[str], None]) -> str:
        """Bloqueante. Llama a on_delta con cada trozo de texto y on_tool al empezar a usar una herramienta.

        Lanza BrainError con un mensaje legible si algo falla.
        """
        with self._lock:
            if self.turns and time.time() - self.last_turn > settings["session_idle_minutes"] * 60:
                self.start()
            elif not self._alive() or self._model != settings["claude_model"]:
                self.start(self.session_id)
            self._interrupted = False
            now = datetime.now()
            # recordatorio en cada mensaje: con el CLAUDE.md solo, en conversaciones largas se alarga
            brief = "máx. 2 frases cortas" if channel == "voz" else "máx. 3 líneas"
            header = f"[{DAYS[now.weekday()]} {now:%d/%m/%Y %H:%M} · canal: {channel} · {brief}]\n"
            log.info("Claude ← %s", text)
            self._send({"type": "user", "message": {"role": "user", "content": header + text}})

            full, result = "", None
            while result is None:
                try:
                    event = self._events.get(timeout=180)
                except queue.Empty:
                    self.ok = False
                    self.start(self.session_id)
                    raise BrainError("timeout", "Claude no contesta. Lo he reiniciado, prueba otra vez.")
                kind = event.get("type")
                if kind == "stream_event" and event.get("parent_tool_use_id") is None:
                    ev = event["event"]
                    delta = ev.get("delta", {})
                    if delta.get("type") == "text_delta":
                        full += delta["text"]
                        on_delta(delta["text"])
                    elif ev.get("type") == "content_block_start":
                        block = ev.get("content_block", {})
                        if block.get("type") == "tool_use":
                            on_tool(block.get("name", ""))
                        elif block.get("type") == "text" and full and not full.endswith("\n"):
                            # texto nuevo tras usar una herramienta: que no se pegue a la frase anterior
                            full += "\n"
                            on_delta("\n")
                elif kind == "system" and event.get("subtype") == "init":
                    self.session_id = event.get("session_id", self.session_id)
                elif kind == "rate_limit_event":
                    self.plan = event.get("rate_limit_info", {})
                    try:
                        PLAN_FILE.write_text(json.dumps(self.plan), encoding="utf-8")
                    except OSError:
                        pass
                elif kind == "result":
                    result = event
                elif kind == "_exit":
                    self.ok = False
                    log.error("Claude salió con %s: %s", event["code"], event["stderr"][-500:])
                    self._proc = None
                    if self._limit_hit(event["stderr"]):
                        raise BrainError("limit", self._limit_message())
                    raise BrainError("crash", "Claude se ha caído. Mira los logs.")

            if self._interrupted:
                raise BrainError("cancelled", "Cancelado")
            if result.get("is_error"):
                self.ok = False
                msg = result.get("result") or result.get("subtype") or "error desconocido"
                log.error("Claude devolvió error: %s", msg)
                if self._limit_hit(msg):
                    raise BrainError("limit", self._limit_message())
                raise BrainError("error", f"Claude ha dado un error: {str(msg)[:200]}")

            self.ok = True
            self.session_id = result.get("session_id", self.session_id)
            self.last_turn = time.time()
            self.turns += 1
            return full or result.get("result", "")

    def _limit_hit(self, msg: str) -> bool:
        return self.plan.get("status") == "rejected" or "limit" in str(msg).lower()

    def _limit_message(self) -> str:
        resets = self.plan.get("resetsAt")
        if resets:
            return f"Me he quedado sin plan de Claude hasta las {datetime.fromtimestamp(resets):%H:%M}."
        return "Me he quedado sin plan de Claude por ahora."


def one_shot(prompt: str, timeout: float = 420) -> str:
    """Una consulta suelta, aparte de la conversación (para la actualización automática). Devuelve el texto."""
    exe = shutil.which("claude")
    if not exe:
        raise BrainError("start", "No encuentro el ejecutable de Claude Code")
    cmd = [exe, "-p", prompt, "--output-format", "text", "--model", settings["claude_model"],
           *_allowed(), "--disallowedTools", BLOCKED,
           "--permission-mode", "dontAsk", "--setting-sources", "project,local"]
    r = subprocess.run(cmd, cwd=settings["data_dir"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)
    if r.returncode != 0:
        raise BrainError("result", (r.stdout or r.stderr or "falló").strip()[:200])
    return r.stdout.strip()
