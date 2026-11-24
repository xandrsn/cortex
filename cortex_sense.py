"""Cortex Sense: gestos de la webcam → comandos. Panel en http://127.0.0.1:8766.

La visión (gestos, mirada) y el servidor corren en hilos; el control de ventanas con pellizco necesita la
ventana de pygame y corre en el hilo principal. Los dos leen la misma webcam (sense.camera.SharedCamera).

`--open` abre el panel en el navegador. Si Sense ya está en marcha, no se arranca otro.
`CortexSense.exe --selftest` solo importa librerías y modelos (sin webcam ni ventanas) y sale con 0 si todo carga.
"""

import os
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
ROOT = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
if getattr(sys, "frozen", False) and (sys.stdout is None or sys.stderr is None):   # sin consola: al registro
    (ROOT / "logs").mkdir(exist_ok=True)
    _log = open(ROOT / "logs" / "sense-console.log", "a", encoding="utf-8", buffering=1)
    sys.stdout, sys.stderr = sys.stdout or _log, sys.stderr or _log


def selftest() -> int:
    import importlib
    import importlib.util

    failed = []
    for name in ["cv2", "numpy", "mediapipe", "pygame", "uvicorn", "fastapi", "sense.config", "sense.commands",
                 "sense.calibration", "sense.camera", "sense.vision", "sense.window.hand_tracker"]:
        try:
            mod = importlib.import_module(name)
            print(f"  ok  {name} {getattr(mod, '__version__', '')}".rstrip())
        except Exception as e:  # noqa: BLE001 - queremos ver cualquier fallo de carga
            failed.append(name)
            print(f"  FALLO {name}: {type(e).__name__}: {e}")
    for name in ["sense.app", "sense.window.main"]:   # se buscan sin importar: importarlos arranca cosas
        ok = importlib.util.find_spec(name) is not None
        failed += [] if ok else [name]
        print(f"  {'ok' if ok else 'FALLO'}  {name} (encontrado, sin importar)")
    from sense.config import WEB_DIR
    from sense.window import hand_tracker
    for f in (Path(hand_tracker._MODEL_PATH), WEB_DIR / "index.html"):
        print(f"  {'ok' if f.exists() else 'FALTA'}  {f}")
        failed += [] if f.exists() else [f.name]
    print("SELFTEST OK" if not failed else f"SELFTEST FALLO: {failed}")
    return 0 if not failed else 1


def running(port: int) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def open_panel(port: int, wait: float = 30) -> None:
    import webbrowser
    end = time.monotonic() + wait
    while not running(port) and time.monotonic() < end:
        time.sleep(0.3)
    webbrowser.open(f"http://127.0.0.1:{port}/")


def main() -> None:
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    from sense.config import settings, setup_logging
    port = settings["ui_port"]
    if running(port):
        if "--open" in sys.argv:
            open_panel(port)
        return
    setup_logging()
    from sense.app import serve, sense

    server = serve(port)
    if "--open" in sys.argv or not settings["setup_done"]:   # la primera vez, al asistente de instalación
        threading.Thread(target=open_panel, args=(port,), daemon=True).start()
    if settings["window_gestures"]:
        from sense.window import config as wcfg
        wcfg.GRAB_THRESHOLD, wcfg.RELEASE_THRESHOLD = settings["pinch_grab"], settings["pinch_release"]
        cap = sense.reader(int(settings["camera_index"]))
        if cap is None:
            print("Control de ventanas desactivado: no se pudo abrir la cámara")
        else:
            try:
                from sense.window.main import main as run_windows
                run_windows(cap)
            except SystemExit as e:
                print(f"Control de ventanas desactivado: {e}")
    server.join()


if __name__ == "__main__":
    main()
