"""Asistente de voz y panel (Pepito), parte de Cortex."""


def main() -> None:
    import faulthandler
    import logging
    import os
    import sys
    import threading

    import uvicorn

    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

    from .config import ROOT, settings, setup_logging

    setup_logging()
    # El error de un cierre inesperado sale por stderr, que run.ps1 no guarda: que quede en el registro.
    log = logging.getLogger("pepito")
    sys.excepthook = lambda t, e, tb: log.critical("Error sin capturar; Cortex se cierra", exc_info=(t, e, tb))
    threading.excepthook = lambda a: log.critical("Error sin capturar en el hilo %s", a.thread and a.thread.name,
                                                  exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    # y si revienta por debajo de Python (una librería nativa), al menos la pila de cada hilo
    faulthandler.enable(open(ROOT / "logs" / "crash.log", "a", encoding="utf-8"), all_threads=True)
    try:
        uvicorn.run("pepito.app:app", host="127.0.0.1", port=settings["ui_port"], log_level="warning")
    except BaseException:
        log.critical("Cortex se cierra por un error", exc_info=True)
        raise
