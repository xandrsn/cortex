"""Separate process that watches whether Windows has the microphone muted.

Run as `python -m hub.micmute`: writes "1"/"0" (muted or not) every 3 s and unmutes the mic if it
receives "unmute" on stdin. It also writes "out:<connected speakers>" so Cortex can switch output when a
Bluetooth speaker connects or disconnects. It runs in its own process because pycaw COM objects released
by the garbage collector from another thread crash the process that uses them.
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
        # active outputs only: enumerating every device (with all its properties) cost ~0.8 s
        # of CPU every 3 s
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
