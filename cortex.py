"""Cortex: una sola app con el asistente de voz (Pepito) y el control de ventanas con gestos (Jarvis).

El asistente corre en un hilo (panel en http://127.0.0.1:<ui_port>); el control de gestos, que necesita la
ventana de pygame, corre en el hilo principal. Si falla (p. ej. sin webcam), el asistente sigue funcionando.

`Cortex.exe --selftest` solo importa las librerías y los módulos (sin webcam, sin ventanas) y sale con 0 si todo carga.
"""

import os
import sys
import threading
from pathlib import Path

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
ROOT = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))


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
    print("SELFTEST OK" if not failed else f"SELFTEST FALLO: {failed}")
    return 0 if not failed else 1


def main() -> None:
    if "--selftest" in sys.argv:
        sys.exit(selftest())

    from pepito import main as run_assistant
    from pepito.config import settings
    import pepito.app  # noqa: F401  (uvicorn lo carga por nombre; así PyInstaller lo empaqueta)

    # No abre ningún navegador: el panel queda en http://127.0.0.1:<ui_port> para abrirlo a mano.
    server = threading.Thread(target=run_assistant, name="cortex-assistant", daemon=True)
    server.start()

    if settings["window_gestures"]:
        try:
            from gesture.main import main as run_gestures
            run_gestures()
        except SystemExit as e:   # gesture main() sale así si no hay webcam: el asistente sigue
            print(f"Control de gestos desactivado: {e}")
    server.join()


if __name__ == "__main__":
    main()
