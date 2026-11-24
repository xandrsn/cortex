"""Cortex Hub: asistente de voz y panel (http://127.0.0.1:<ui_port>, 8765 de serie).

Los gestos son de Cortex Sense (sense.py, otro programa): Hub se conecta a ws://127.0.0.1:8766/ws y ejecuta
los gestos con acción de Hub. Sin Sense, Hub funciona igual, solo que sin gestos.

`--open` (accesos directos del instalador) abre el panel en el navegador. Si Cortex ya está en marcha, no se arranca otro.
`CortexHub.exe --selftest` solo importa las librerías y los módulos (sin webcam, sin ventanas) y sale con 0 si todo carga.
"""

import os
import sys
import threading
from pathlib import Path

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

    # hub.app se busca sin importarlo: al importarlo arranca el asistente (micro, modelos)
    mods = ["numpy", "hub.config", "hub.sense_link", "websockets",
            "uvicorn", "faster_whisper", "onnx_asr", "openwakeword", "piper", "edge_tts", "sounddevice"]
    lazy = ["hub.app"]
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
    oww = Path(importlib.util.find_spec("openwakeword").origin).parent / "resources" / "models"
    for f in (Path(importlib.util.find_spec("hub").origin).with_name("silero_vad.onnx"),
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
    # En el exe, sys.executable es CortexHub.exe: `-m hub.micmute` (audio.py) tiene que ejecutar solo ese módulo,
    # o cada Hub arrancaría otro Hub sin fin.
    if sys.argv[1:3] == ["-m", "hub.micmute"]:
        from hub.micmute import main as run_micmute
        return run_micmute()
    if "--selftest" in sys.argv:
        sys.exit(selftest())

    from hub.config import settings
    port = settings["ui_port"]
    if already_running(port):
        if "--open" in sys.argv:
            open_panel(port)
        return
    from hub import main as run_assistant
    import hub.app  # noqa: F401  (uvicorn lo carga por nombre; así PyInstaller lo empaqueta)

    # Sin --open no abre ningún navegador: el panel queda en http://127.0.0.1:<ui_port>.
    if "--open" in sys.argv:
        threading.Thread(target=open_panel, args=(port,), daemon=True).start()
    run_assistant()


if __name__ == "__main__":
    main()
