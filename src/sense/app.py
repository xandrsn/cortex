"""Servidor de Cortex Sense en http://127.0.0.1:8766.

- /           panel de Sense: instalación (cámara + calibración), qué hace cada gesto, gestos propios
- /ws         eventos en vivo: {"type": "vision"|"gesture"|"presence"|"settings", ...}. Cortex Hub se conecta
              aquí, se presenta con {"type": "hello", "role": "hub"} y recibe los gestos con acción de Hub.
- /api/...    estado, ajustes, vídeo de la cámara, grabar/borrar gestos propios, calibrar, probar una acción
"""

import asyncio
import ctypes
import logging
import sys
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import calibration, commands
from .camera import SharedCamera
from .config import DEFAULTS, HUB_ACTIONS, WEB_DIR, settings
from .vision import GESTURES, MOTIONS, TWO_HANDS, H, W, Vision

log = logging.getLogger("sense")
# Solo el panel de Sense, el de Hub y programas locales (sin Origin) pueden cambiar cosas: si no, cualquier web
# abierta podría mandar un POST a 127.0.0.1 y asignar «run:...» a un gesto.
ALLOWED_ORIGINS = {f"http://{h}:{p}" for h in ("127.0.0.1", "localhost") for p in (DEFAULTS["ui_port"], 8765)}


def origin_ok(origin: str | None) -> bool:
    return origin is None or origin in ALLOWED_ORIGINS


def desktop_size() -> tuple[int, int]:
    if sys.platform != "win32":
        return 1920, 1080
    m = ctypes.windll.user32.GetSystemMetrics
    return m(78) or 1920, m(79) or 1080   # SM_CXVIRTUALSCREEN / SM_CYVIRTUALSCREEN


class Sense:
    def __init__(self):
        self.cams: dict[int, SharedCamera] = {}
        self.vision = Vision(on_gesture=self.on_gesture, on_presence=self.on_presence, capture_factory=self.reader)
        self.clients: set[WebSocket] = set()
        self.hubs: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.samples: dict[str, list[dict]] = {}
        self.calibrating: str | None = None
        self._sent: dict = {}

    def reader(self, index: int):
        cam = self.cams.setdefault(index, SharedCamera(index, W, H))
        return cam.reader()

    # ── eventos de la visión (llegan desde su hilo) ──
    def on_gesture(self, gesture: str, action: str):
        target = commands.route(action, hub_connected=bool(self.hubs))
        self._emit({"type": "gesture", "gesture": gesture, "action": action, "handled_by": target and
                    ("hub" if target == "hub" else "sense")})
        if target and target != "hub":
            try:
                commands.run(target)
            except Exception:
                log.exception("No se pudo ejecutar %s", target)

    def on_presence(self, engaged: bool):
        self._emit({"type": "presence", "engaged": engaged})

    def _emit(self, msg: dict):
        if self.loop:
            asyncio.run_coroutine_threadsafe(self.broadcast(msg), self.loop)

    async def broadcast(self, msg: dict):
        for ws in list(self.clients):
            try:
                await ws.send_json(msg)
            except Exception:
                self.clients.discard(ws)
                self.hubs.discard(ws)

    def state(self) -> dict:
        return {**self.vision.state(), "hub_connected": bool(self.hubs), "calibrating": self.calibrating}

    async def pump(self):
        """Estado de la cámara a los clientes: rápido si alguien mira el vídeo, si no cuando cambia."""
        while True:
            await asyncio.sleep(0.3 if self.vision.preview_clients or self.calibrating else 1.0)
            if not self.clients:
                continue
            st = self.state()
            light = {k: v for k, v in st.items() if k not in ("fps", "cam_fps", "yaw", "pitch", "score",
                                                               "brightness", "hand_size", "pinch")}
            if light != self._sent or self.vision.preview_clients or self.calibrating:
                self._sent = light
                await self.broadcast({"type": "vision", **st})


sense = Sense()


@asynccontextmanager
async def lifespan(_app):
    sense.loop = asyncio.get_running_loop()
    sense.vision.start()
    pump = asyncio.create_task(sense.pump())
    yield
    pump.cancel()
    await asyncio.to_thread(sense.vision.stop)


app = FastAPI(title="Cortex Sense", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB_DIR, check_dir=False), name="static")


@app.middleware("http")
async def same_origin_only(request: Request, call_next):
    if request.method != "GET" and not origin_ok(request.headers.get("origin")):
        return JSONResponse({"error": "origen no permitido"}, status_code=403)
    return await call_next(request)


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/state")
async def get_state():
    return sense.state()


@app.get("/api/catalog")
async def catalog():
    return {"gestures": GESTURES, "motions": MOTIONS, "two_hands": TWO_HANDS, "hub_actions": HUB_ACTIONS, "media": list(commands.MEDIA),
            "window": commands.WINDOW, "defaults": DEFAULTS["gesture_actions"]}


@app.get("/api/settings")
async def get_settings():
    return settings.all()


@app.post("/api/settings")
async def post_settings(body: dict):
    for gid, action in (body.get("gesture_actions") or {}).items():
        if err := commands.validate(action):
            return {"error": f"{gid}: {err}"}
    if "gesture_actions" in body:   # se mezcla: Hub manda solo las que conoce
        body["gesture_actions"] = {**settings["gesture_actions"], **body["gesture_actions"]}
    data = await asyncio.to_thread(settings.update, body)
    await sense.broadcast({"type": "settings", "settings": data})
    return {"ok": True, "settings": data}


@app.post("/api/action/test")
async def test_action(body: dict):
    action = str(body.get("action", ""))
    if err := commands.validate(action):
        return {"error": err}
    target = commands.route(action, hub_connected=bool(sense.hubs))
    if target == "hub":
        await sense.broadcast({"type": "gesture", "gesture": "test", "action": action, "handled_by": "hub"})
    elif target:
        await asyncio.to_thread(commands.run, target)
    return {"ok": True, "handled_by": target and ("hub" if target == "hub" else "sense")}


@app.get("/api/camera.mjpg")
async def camera_stream():
    vision = sense.vision

    async def frames():
        vision.preview_clients += 1
        try:
            last = None
            while True:
                jpg = vision.frame_jpeg()
                if jpg and jpg is not last:
                    last = jpg
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpg)).encode()
                           + b"\r\n\r\n" + jpg + b"\r\n")
                await asyncio.sleep(0.06)
        finally:
            vision.preview_clients = max(0, vision.preview_clients - 1)

    return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store"})


@app.post("/api/gestures/record")
async def record_gesture(body: dict):
    name = str(body.get("name", "")).strip()
    if not await asyncio.to_thread(sense.vision.record_custom, name, float(body.get("seconds", 3))):
        return {"error": "No puedo grabar ahora: ¿cámara activada y nombre sin «:»?"}
    return {"ok": True}


@app.post("/api/gestures/delete")
async def delete_gesture(body: dict):
    name = str(body.get("name", ""))
    ok = await asyncio.to_thread(sense.vision.delete_custom, name)
    if ok:   # la acción del gesto borrado tampoco se queda colgando
        actions = {k: v for k, v in settings["gesture_actions"].items() if k != f"custom:{name}"}
        await asyncio.to_thread(settings.update, {"gesture_actions": actions})
    return {"ok": ok}


@app.post("/api/calibrate")
async def calibrate(body: dict):
    """Un paso de la calibración: guarda muestras del estado durante `seconds` y devuelve cuántas."""
    step = str(body.get("step", ""))
    if step not in calibration.STEPS:
        return {"error": f"paso desconocido: {step}"}
    if sense.calibrating:
        return {"error": "ya estoy calibrando"}
    seconds = min(10.0, max(1.0, float(body.get("seconds", 4))))
    sense.calibrating, got = step, []
    try:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            got.append(sense.vision.state())
            await asyncio.sleep(1 / 15)
    finally:
        sense.calibrating = None
    sense.samples[step] = got
    return {"ok": True, "step": step, "samples": len(got), "with_hand": sum(1 for s in got if s.get("hand"))}


@app.post("/api/calibrate/apply")
async def calibrate_apply():
    changes, report = calibration.suggest(sense.samples, desktop_size(), DEFAULTS)
    data = await asyncio.to_thread(settings.update, {**changes, "setup_done": True,
                                                     "calibration": {**report, "time": time.strftime("%Y-%m-%d %H:%M")}})
    sense.samples = {}
    await sense.broadcast({"type": "settings", "settings": data})
    return {"ok": True, "changes": changes, "report": report}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if not origin_ok(ws.headers.get("origin")):
        await ws.close(code=1008)
        return
    await ws.accept()
    sense.clients.add(ws)
    try:
        await ws.send_json({"type": "settings", "settings": settings.all()})
        await ws.send_json({"type": "vision", **sense.state()})
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "hello" and msg.get("role") == "hub":
                sense.hubs.add(ws)
                log.info("Cortex Hub conectado")
            elif msg.get("type") == "yield_cpu":   # Hub está transcribiendo: la CPU, para eso
                sense.vision.yield_cpu = bool(msg.get("on"))
    except (WebSocketDisconnect, RuntimeError, ValueError):
        pass
    finally:
        sense.clients.discard(ws)
        if ws in sense.hubs:
            sense.hubs.discard(ws)
            sense.vision.yield_cpu = False
            log.info("Cortex Hub desconectado")


def serve(port: int) -> threading.Thread:
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, name="sense-server", daemon=True)
    t.start()
    return t
