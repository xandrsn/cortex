"""Enlace con Cortex Sense (el programa de los gestos): misma interfaz que tenía la visión dentro de Hub.

Se conecta a ws://127.0.0.1:8766/ws, se presenta como Hub y recibe el estado de la cámara, los ajustes de
gestos y los gestos cuya acción es de Hub (siguiente canción, escuchar, callar…). Si Sense no está en marcha,
lo reintenta cada pocos segundos; Hub funciona igual, sin gestos.
"""

import json
import logging
import threading
import urllib.request
from typing import Callable

log = logging.getLogger("hub")


class SenseLink:
    def __init__(self, url: str, on_gesture: Callable[[str, str], None] | None = None,
                 on_presence: Callable[[bool], None] | None = None, connect=None):
        self.url = url.rstrip("/")
        self.on_gesture = on_gesture
        self.on_presence = on_presence
        self.connected = False
        self.settings: dict = {}      # ajustes de Sense (los de gestos se ven y se cambian desde Hub)
        self.preview_clients = 0      # el vídeo lo sirve Sense directamente
        self._state: dict = {}
        self._ws = None
        self._yield = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connect = connect       # para las pruebas; si no, websockets
        self._lock = threading.Lock()

    # ── conexión ──
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="sense-link", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self):
        if self._connect is None:
            from websockets.sync.client import connect
            self._connect = lambda u: connect(u, open_timeout=3, max_size=2**22)
        ws_url = "ws" + self.url[4:] + "/ws"
        while not self._stop.is_set():
            try:
                with self._connect(ws_url) as ws:
                    self._ws = ws
                    ws.send(json.dumps({"type": "hello", "role": "hub"}))
                    if self._yield:
                        ws.send(json.dumps({"type": "yield_cpu", "on": True}))
                    if not self.connected:
                        log.info("Conectado con Cortex Sense")
                    self.connected = True
                    while not self._stop.is_set():
                        try:
                            raw = ws.recv(timeout=1)
                        except TimeoutError:
                            continue
                        self.handle(json.loads(raw))
            except Exception as e:  # noqa: BLE001 - Sense apagado, reiniciándose, etc.
                if self.connected:
                    log.info("Se perdió la conexión con Cortex Sense: %s", e)
            self._ws, self.connected, self._state = None, False, {}
            self._stop.wait(3)

    def handle(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "vision":
            self._state = {k: v for k, v in msg.items() if k != "type"}
        elif kind == "settings":
            self.settings = msg.get("settings") or {}
        elif kind == "gesture" and msg.get("handled_by") == "hub" and self.on_gesture:
            self.on_gesture(str(msg.get("gesture")), str(msg.get("action")))
        elif kind == "presence" and self.on_presence:
            self.on_presence(bool(msg.get("engaged")))

    # ── interfaz de la antigua Vision ──
    @property
    def yield_cpu(self) -> bool:
        return self._yield

    @yield_cpu.setter
    def yield_cpu(self, on: bool) -> None:
        if on == self._yield:
            return
        self._yield = on
        ws = self._ws
        if ws is not None:
            try:
                ws.send(json.dumps({"type": "yield_cpu", "on": on}))
            except Exception:
                pass

    def state(self) -> dict:
        if not self.connected:
            return {"enabled": False, "camera_ok": False, "sense": False, "custom": [],
                    "error": "Cortex Sense no está en marcha"}
        return {**self._state, "sense": True}

    def list_custom(self) -> list[dict]:
        return list(self._state.get("custom") or [])

    def frame_jpeg(self) -> None:
        return None

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read() or b"{}")
        except Exception as e:  # noqa: BLE001
            return {"error": f"Cortex Sense no responde: {e}"}

    def record_custom(self, name: str, seconds: float = 3.0) -> bool:
        return bool(self._post("/api/gestures/record", {"name": name, "seconds": seconds}).get("ok"))

    def delete_custom(self, name: str) -> bool:
        return bool(self._post("/api/gestures/delete", {"name": name}).get("ok"))

    def update_settings(self, changes: dict) -> str | None:
        """Cambia ajustes de gestos en Sense. Devuelve el error, si lo hay."""
        r = self._post("/api/settings", changes)
        if r.get("settings"):
            self.settings = r["settings"]
        return r.get("error")
