"""Cortex Hub: voice assistant and panel (http://127.0.0.1:<ui_port>, 8765 by default).

Gestures belong to Cortex Sense (sense.py, a separate program): Hub connects to ws://127.0.0.1:8766/ws and runs
the gestures bound to Hub actions. Without Sense, Hub works the same, just without gestures.

`--open` (installer shortcuts) opens the panel in the browser. If Cortex is already running, no second instance starts.
`CortexHub.exe --selftest` only imports the libraries and modules (no webcam, no windows) and exits 0 if everything loads.
"""

import os
import sys
import threading
from pathlib import Path

ROOT = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
if getattr(sys, "frozen", False):
    os.environ.setdefault("HF_HOME", str(ROOT / "models" / "hf"))   # so the voice model is removed on uninstall
    if sys.stdout is None or sys.stderr is None:   # no console: write printed output to the log
        (ROOT / "logs").mkdir(exist_ok=True)
        _log = open(ROOT / "logs" / "console.log", "a", encoding="utf-8", buffering=1)
        sys.stdout, sys.stderr = sys.stdout or _log, sys.stderr or _log


def selftest() -> int:
    """Load everything the app uses without opening the camera or windows. Prints versions and returns 0 on success."""
    import importlib
    import importlib.util
    import platform

    # hub.app is looked up without importing: importing it starts the assistant (mic, models)
    mods = ["numpy", "hub.config", "hub.sense_link", "websockets",
            "uvicorn", "faster_whisper", "onnx_asr", "openwakeword", "piper", "edge_tts", "sounddevice"]
    lazy = ["hub.app"]
    failed = []
    print(f"Python {platform.python_version()} on {platform.platform()}")
    for name in mods:
        try:
            mod = importlib.import_module(name)
            print(f"  ok  {name} {getattr(mod, '__version__', '')}".rstrip())
        except Exception as e:  # noqa: BLE001 - we want to see any load failure
            failed.append(name)
            print(f"  FAILED {name}: {type(e).__name__}: {e}")
    for name in lazy:
        if importlib.util.find_spec(name) is None:
            failed.append(name)
        print(f"  {'ok' if name not in failed else 'FAILED'}  {name} (found, not imported)")
    oww = Path(importlib.util.find_spec("openwakeword").origin).parent / "resources" / "models"
    for f in (Path(importlib.util.find_spec("hub").origin).with_name("silero_vad.onnx"),
              oww / "melspectrogram.onnx", oww / "embedding_model.onnx"):
        print(f"  {'ok' if f.exists() else 'MISSING'}  {f.name}")
        if not f.exists():
            failed.append(f.name)
    print("SELFTEST OK" if not failed else f"SELFTEST FAILED: {failed}")
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
    # In the exe, sys.executable is CortexHub.exe: `-m hub.micmute` (audio.py) must run only that module,
    # or every Hub would start another Hub endlessly.
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
    import hub.app  # noqa: F401  (uvicorn loads it by name; this way PyInstaller bundles it)

    # Without --open no browser is opened: the panel stays at http://127.0.0.1:<ui_port>.
    if "--open" in sys.argv:
        threading.Thread(target=open_panel, args=(port,), daemon=True).start()
    run_assistant()


if __name__ == "__main__":
    main()
