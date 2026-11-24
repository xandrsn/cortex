"""Proceso aparte que vigila si Windows tiene el micro silenciado.

Se ejecuta como `python -m hub.micmute`: escribe "1"/"0" (silenciado o no) cada 3 s y reactiva el
micro si recibe "unmute" por stdin. También escribe "out:<altavoces conectados>" para que Cortex cambie
de salida cuando se conecta o desconecta un altavoz Bluetooth. Va en otro proceso porque los objetos COM de pycaw, liberados
desde otro hilo por el recolector de basura, tumban el proceso que los usa.
"""

import sys
import threading
import warnings
from ctypes import POINTER, cast

import comtypes
from comtypes import CLSCTX_ALL
from pycaw.constants import DEVICE_STATE, EDataFlow
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume


def speakers() -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        # solo salidas activas: enumerar todos los dispositivos (con todas sus propiedades) costaba ~0,8 s
        # de CPU cada 3 s
        names = [d.FriendlyName for d in AudioUtilities.GetAllDevices(EDataFlow.eRender.value,
                                                                      DEVICE_STATE.ACTIVE.value)]
    return "|".join(sorted(n or "" for n in names))


def main() -> None:
    comtypes.CoInitialize()
    wake = threading.Event()
    unmute = threading.Event()

    def read_stdin():
        for line in sys.stdin:
            if line.strip() == "unmute":
                unmute.set()
                wake.set()
        sys.exit(0)

    threading.Thread(target=read_stdin, daemon=True).start()
    while True:
        try:
            endpoint = cast(AudioUtilities.GetMicrophone().Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None),
                            POINTER(IAudioEndpointVolume))
            if unmute.is_set():
                endpoint.SetMute(0, None)
                unmute.clear()
            state = "1" if endpoint.GetMute() else "0"
        except Exception:
            state = "?"
        print(state, flush=True)
        try:
            print("out:" + speakers(), flush=True)
        except Exception:
            pass
        wake.wait(3)
        wake.clear()


if __name__ == "__main__":
    main()
