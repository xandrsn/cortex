"""Cortex: una sola app con el asistente de voz (Cortex) y el control de ventanas con gestos (Jarvis).

El asistente corre en un hilo (panel en http://127.0.0.1:<ui_port>); el control de gestos, que necesita la
ventana de pygame, corre en el hilo principal. Si falla (p. ej. sin webcam), el asistente sigue funcionando.

`--open` (accesos directos del instalador) abre el panel en el navegador. Si Cortex ya está en marcha, no se arranca otro.
`Cortex.exe --selftest` solo importa las librerías y los módulos (sin webcam, sin ventanas) y sale con 0 si todo carga.
"""

import os
import sys
import threading
from pathlib import Path

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
ROOT = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
if getattr(sys, "frozen", False):
    os.environ.setdefault("HF_HOME", str(ROOT / "models" / "hf"))   # que el modelo de voz se vaya al desinstalar
    if sys.stdout is None or sys.stderr is None:   # sin consola: lo que se imprime, al registro
        (ROOT / "logs").mkdir(exist_ok=True)
        _log = open(ROOT / "logs" / "console.log", "a", encoding="utf-8", buffering=1)
        sys.stdout, sys.stderr = sys.stdout or _log, sys.stderr or _log


def selftest() -> int:
    """Carga todo lo que usa la app sin abrir cámara ni ventanas. Imprime versiones y devuelve 0 si va bien."""
    import importlib
    import importlib.util
    import platform

    # pepito.app y gesture.main se buscan sin importarlos: al importarlos arrancan el asistente (micro, modelos)
    mods = ["cv2", "numpy", "mediapipe", "pygame", "gesture.hand_tracker", "pepito.config",
            "uvicorn", "faster_whisper", "onnx_asr", "openwakeword", "piper", "edge_tts", "sounddevice"]
    lazy = ["pepito.app", "gesture.main"]
    failed = []
    print(f"Python {platform.python_version()} en {platform.platform()}")
    for name in mods:
        try:
            mod = importlib.import_module(name)
            print(f"  ok  {name} {getattr(mod, '__version__', '')}".rstrip())
        except Exception as e:  # noqa: BLE001 - queremos ver cualquier fallo de carga
            failed.append(name)
            print(f"  FALLO {name}: {type(e).__name__}: {e}")
    for name in lazy:
        if importlib.util.find_spec(name) is None:
            failed.append(name)
        print(f"  {'ok' if name not in failed else 'FALLO'}  {name} (encontrado, sin importar)")
    import cv2
    print(f"OpenCV {cv2.__version__} · build: {'contrib' if 'contrib' in cv2.getBuildInformation().lower() else 'base'}")
    from gesture import hand_tracker
    hand_ok = Path(hand_tracker._MODEL_PATH).exists()   # el modelo va empaquetado junto al módulo gesture
    print(f"modelo de manos: {'ok' if hand_ok else 'FALTA ' + hand_tracker._MODEL_PATH}")
    if not hand_ok:
        failed.append("hand_landmarker.task")
    oww = Path(importlib.util.find_spec("openwakeword").origin).parent / "resources" / "models"
    for f in (Path(importlib.util.find_spec("pepito").origin).with_name("silero_vad.onnx"),
              oww / "melspectrogram.onnx", oww / "embedding_model.onnx"):
        print(f"  {'ok' if f.exists() else 'FALTA'}  {f.name}")
        if not f.exists():
            failed.append(f.name)
    print("SELFTEST OK" if not failed else f"SELFTEST FALLO: {failed}")
    return 0 if not failed else 1


def already_running(port: int) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def open_panel(port: int, wait: float = 60) -> None:
    import time
    import webbrowser
    end = time.monotonic() + wait
    while not already_running(port) and time.monotonic() < end:
        time.sleep(0.5)
    webbrowser.open(f"http://127.0.0.1:{port}/")


def main() -> None:
    # En el exe, sys.executable es Cortex.exe: `-m pepito.micmute` (audio.py) tiene que ejecutar solo ese módulo,
    # o cada Cortex arrancaría otro Cortex sin fin.
    if sys.argv[1:3] == ["-m", "pepito.micmute"]:
        from pepito.micmute import main as run_micmute
        return run_micmute()
    if "--selftest" in sys.argv:
        sys.exit(selftest())

    from pepito.config import settings
    port = settings["ui_port"]
    if already_running(port):
        if "--open" in sys.argv:
            open_panel(port)
        return
    from pepito import main as run_assistant
    import pepito.app  # noqa: F401  (uvicorn lo carga por nombre; así PyInstaller lo empaqueta)

    # Sin --open no abre ningún navegador: el panel queda en http://127.0.0.1:<ui_port>.
    server = threading.Thread(target=run_assistant, name="cortex-assistant", daemon=True)
    server.start()
    if "--open" in sys.argv:
        threading.Thread(target=open_panel, args=(port,), daemon=True).start()

    if settings["window_gestures"]:
        try:
            from gesture.main import main as run_gestures
            run_gestures()
        except SystemExit as e:   # gesture main() sale así si no hay webcam: el asistente sigue
            print(f"Control de gestos desactivado: {e}")
    server.join()


if __name__ == "__main__":
    main()
