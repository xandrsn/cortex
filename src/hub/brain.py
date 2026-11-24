"""Bridge to Claude Code in headless mode (`claude -p`) using the user's plan.

A single `claude -p --input-format stream-json` process stays open and warm; each turn is one JSON
line on stdin, so Claude Code's startup cost is not paid on every question. The conversation continues in
the same process until N minutes pass without talking or a new one is requested; then a clean process
is started. If the process dies it is relaunched with --resume so the thread is not lost.
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
from pathlib import Path
from typing import Callable

from .config import ROOT, settings

PLAN_FILE = ROOT / "logs" / "claude_plan.json"   # last plan usage: the dashboard shows it right at startup

log = logging.getLogger("brain")

TOOLS = "Read,Edit,Write,Glob,Grep,WebSearch,WebFetch"
# connectors of the claude.ai account (Gmail and Calendar); with "dontAsk" anything not listed here is denied
CONNECTORS = "mcp__claude_ai_Gmail,mcp__claude_ai_Google_Calendar"
WA_MCP = ROOT / "whatsapp-mcp.json"   # whatsapp-mcp MCP server for the brain
WA_TOOLS = ",".join(f"mcp__whatsapp__{t}" for t in ("search_contacts", "list_messages", "list_chats", "get_chat",
                    "get_direct_chat_by_contact", "get_contact_chats", "get_last_interaction", "get_message_context"))
BLOCKED = ("Bash,PowerShell,NotebookEdit,Agent,Task,CronCreate,CronDelete,RemoteTrigger,SendMessage,PushNotification,"
           "mcp__whatsapp__send_message,mcp__whatsapp__send_file,mcp__whatsapp__send_audio_message,mcp__whatsapp__download_media")


def _allowed() -> list[str]:
    if WA_MCP.exists():
        return ["--allowedTools", f"{TOOLS},{CONNECTORS},{WA_TOOLS}", "--mcp-config", str(WA_MCP)]
    return ["--allowedTools", f"{TOOLS},{CONNECTORS}"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def claude_exe() -> str | None:
    """The Claude Code executable. The official installer puts it in ~/.local/bin, which is not on the PATH of a
    process started before installing it."""
    local = Path.home() / ".local" / "bin" / "claude.exe"
    return shutil.which("claude") or (str(local) if local.exists() else None)


def claude_status() -> dict:
    """Is Claude Code installed and logged in? (only that: no email or organization)"""
    exe = claude_exe()
    if not exe:
        return {"installed": False, "logged": False}
    try:
        r = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        logged = bool(json.loads(r.stdout).get("loggedIn"))
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        logged = False
    return {"installed": True, "logged": logged}


def claude_login() -> None:
    """Open a console with the official Claude login (requested by the user with the button)."""
    exe = claude_exe()
    if not exe:
        raise BrainError("start", "Cannot find the Claude Code executable")
    subprocess.Popen([exe, "auth", "login"], creationflags=subprocess.CREATE_NEW_CONSOLE)


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
        self.plan: dict = {}          # last rate_limit_event (plan usage)
        try:
            self.plan = json.loads(PLAN_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        self.ok: bool | None = None   # None = not tested yet
        self._proc: subprocess.Popen | None = None
        self._model = None
        self._events: queue.Queue = queue.Queue()
        self._lock = threading.Lock()       # one turn at a time
        self._proc_lock = threading.Lock()  # start/stop the process
        self._interrupted = False

    # ---------- process ----------
    def _command(self, resume: str | None) -> list[str]:
        exe = claude_exe()
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
        """Start (or restart) the process. Without resume the conversation starts from scratch."""
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
            log.info("Claude started (%s, %s)", self._model, "continuing" if resume else "new conversation")

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
        # stderr is drained at the same time in another thread: if its pipe filled up (repeated warnings),
        # Claude would block writing and stop answering
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
        """Called now and then: new conversation after the idle time, or if the model changed."""
        if self._lock.locked():
            return
        idle = self.turns and time.time() - self.last_turn > settings["session_idle_minutes"] * 60
        if idle:
            log.info("New conversation due to inactivity")
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
            raise BrainError("crash", f"The connection to Claude dropped: {e}") from e

    # ---------- turns ----------
    def ask(self, text: str, lang: str, channel: str, on_delta: Callable[[str], None],
            on_tool: Callable[[str], None]) -> str:
        """Blocking. Calls on_delta with each text chunk and on_tool when a tool starts being used.

        Raises BrainError with a readable message if something fails.
        """
        with self._lock:
            if self.turns and time.time() - self.last_turn > settings["session_idle_minutes"] * 60:
                self.start()
            elif not self._alive() or self._model != settings["claude_model"]:
                self.start(self.session_id)
            self._interrupted = False
            now = datetime.now()
            # reminder on every message: with CLAUDE.md alone, replies grow longer in long conversations
            brief = "max. 2 short sentences" if channel == "voz" else "max. 3 lines"
            header = f"[{DAYS[now.weekday()]} {now:%d/%m/%Y %H:%M} · channel: {channel} · {brief}]\n"
            log.info("Claude ← %s", text)
            self._send({"type": "user", "message": {"role": "user", "content": header + text}})

            full, result = "", None
            while result is None:
                try:
                    event = self._events.get(timeout=180)
                except queue.Empty:
                    self.ok = False
                    self.start(self.session_id)
                    raise BrainError("timeout", "Claude is not responding. I restarted it, try again.")
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
                            # new text after using a tool: do not glue it to the previous sentence
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
                    log.error("Claude exited with %s: %s", event["code"], event["stderr"][-500:])
                    self._proc = None
                    if self._limit_hit(event["stderr"]):
                        raise BrainError("limit", self._limit_message())
                    raise BrainError("crash", "Claude crashed. Check the logs.")

            if self._interrupted:
                raise BrainError("cancelled", "Cancelled")
            if result.get("is_error"):
                self.ok = False
                msg = result.get("result") or result.get("subtype") or "unknown error"
                log.error("Claude returned an error: %s", msg)
                if self._limit_hit(msg):
                    raise BrainError("limit", self._limit_message())
                raise BrainError("error", f"Claude returned an error: {str(msg)[:200]}")

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
            return f"I have run out of Claude plan until {datetime.fromtimestamp(resets):%H:%M}."
        return "I have run out of Claude plan for now."


def one_shot(prompt: str, timeout: float = 420) -> str:
    """A standalone query, separate from the conversation (for the automatic update). Returns the text."""
    exe = claude_exe()
    if not exe:
        raise BrainError("start", "No encuentro el ejecutable de Claude Code")
    cmd = [exe, "-p", prompt, "--output-format", "text", "--model", settings["claude_model"],
           *_allowed(), "--disallowedTools", BLOCKED,
           "--permission-mode", "dontAsk", "--setting-sources", "project,local"]
    r = subprocess.run(cmd, cwd=settings["data_dir"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)
    if r.returncode != 0:
        raise BrainError("result", (r.stdout or r.stderr or "failed").strip()[:200])
    return r.stdout.strip()
