"""Audio input and output with WASAPI.

- Player: a persistent OutputStream that audio chunks are queued onto. Being always open, a Bluetooth
  Echo does not fall asleep and does not swallow the first syllables.
- Mic: a persistent InputStream that hands 16 kHz mono blocks to whoever listens (the listener).
  While Cortex speaks the mic is ignored so it does not hear itself.
"""

import logging
import re
import queue
import threading

import numpy as np
import sounddevice as sd

log = logging.getLogger("audio")

MIC_RATE = 16000
BLOCK_SECONDS = 0.03


def _wasapi_index() -> int:
    for i, api in enumerate(sd.query_hostapis()):
        if api["name"] == "Windows WASAPI":
            return i
    raise RuntimeError("WASAPI not available")


def refresh_devices() -> None:
    """PortAudio does not notice new devices (Bluetooth) without restarting."""
    sd._terminate()
    sd._initialize()


def list_devices() -> dict:
    api = _wasapi_index()
    inputs, outputs = [], []
    for d in sd.query_devices():
        if d["hostapi"] != api:
            continue
        # Bluetooth hands-free mics are no good (phone quality): they are not even offered
        if d["max_input_channels"] > 0 and not _is_handsfree(d["name"]):
            inputs.append(d["name"])
        if d["max_output_channels"] > 0:
            outputs.append(d["name"])
    return {"inputs": inputs, "outputs": outputs}


def resolve_device(name: str | None, kind: str) -> int:
    """WASAPI index of the device with that name, or the default if it is missing."""
    api = _wasapi_index()
    if kind == "input" and name and _is_handsfree(name):
        name = None
    if name:
        for i, d in enumerate(sd.query_devices()):
            channels = d["max_input_channels"] if kind == "input" else d["max_output_channels"]
            if d["hostapi"] == api and channels > 0 and d["name"] == name:
                return i
        log.warning("Device %r not found, using the default", name)
    key = "default_input_device" if kind == "input" else "default_output_device"
    default = sd.query_hostapis(api)[key]
    if kind == "input" and _is_handsfree(sd.query_devices(default)["name"]):
        # Bluetooth hands-free mics record like a phone and also drop the speaker to call quality:
        # better to use the laptop mic if there is one.
        for i, d in enumerate(sd.query_devices()):
            if d["hostapi"] == api and d["max_input_channels"] > 0 and not _is_handsfree(d["name"]):
                return i
    return default


def _is_handsfree(name: str) -> bool:
    return bool(re.search(r"headset|hands-free|manos libres", name, re.I))


class WindowsMicMute:
    """Watches whether Windows has the mic muted and allows unmuting it (via the micmute process)."""

    def __init__(self):
        self.muted: bool | None = None
        self.outputs: str | None = None   # active speakers in Windows (changes when connecting/disconnecting)
        self._proc = None
        threading.Thread(target=self._run, daemon=True, name="mic-mute").start()

    def unmute(self) -> None:
        try:
            self._proc.stdin.write(b"unmute\n")
            self._proc.stdin.flush()
        except Exception as e:
            log.warning("Cannot unmute the mic: %s", e)

    def _run(self) -> None:
        import subprocess
        import sys
        import time
        while True:
            proc = None
            try:
                proc = self._proc = subprocess.Popen(
                    [sys.executable, "-m", "hub.micmute"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
                for line in proc.stdout:
                    state = line.strip()
                    if state.startswith(b"out:"):
                        self.outputs = state[4:].decode("utf-8", "replace")
                        continue
                    self.muted = {b"1": True, b"0": False}.get(state)
            except Exception as e:
                log.warning("Failed while watching the Windows mic: %s", e)
            finally:
                # if the process dies it is relaunched; without closing its pipes handles would leak on every loop
                if proc is not None:
                    for pipe in (proc.stdin, proc.stdout):
                        try:
                            pipe.close()
                        except Exception:
                            pass
                    try:
                        proc.kill()
                        proc.wait(timeout=3)
                    except Exception:
                        pass
            self.muted = None
            time.sleep(5)


def resample(audio: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or len(audio) == 0:
        return audio.astype(np.float32)
    n = int(round(len(audio) * dst / src))
    x = np.linspace(0, len(audio) - 1, n)
    return np.interp(x, np.arange(len(audio)), audio).astype(np.float32)


class StreamResampler:
    """Continuous, filtered (polyphase) sample-rate conversion: no false treble from linear interpolation
    when upsampling (Edge 24 kHz -> 48 kHz speaker) and no noise leaking in when downsampling unfiltered
    (48 kHz mic -> 16 kHz Parakeet). Keeps state between chunks: no jumps at the seams.
    Works for any pair of rates (including the 44.1 kHz of many Bluetooth speakers)."""

    def __init__(self, src: int, dst: int):
        from math import gcd
        from scipy.signal import firwin
        g = gcd(src, dst)
        self.src, self.dst, self.up, self.down = src, dst, dst // g, src // g
        if src == dst:
            return
        n = 24 * max(self.up, self.down) + 1
        h = firwin(n, 0.95 / max(self.up, self.down), window=("kaiser", 7.0)) * self.up
        self.k = -(-n // self.up)   # coefficients per phase
        h = np.concatenate([h, np.zeros(self.k * self.up - n)])
        self.taps = h.reshape(self.k, self.up).T.astype(np.float32)   # taps[r, j] = h[r + j·up]
        self.reset()

    def reset(self) -> None:
        if self.src != self.dst:
            self.hist = np.zeros(self.k - 1, dtype=np.float32)
            self.n_in = 0      # input samples already received
            self.m_next = 0    # next output sample

    def __call__(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32)
        if self.src == self.dst or len(x) == 0:
            return x if self.src == self.dst else np.zeros(0, dtype=np.float32)
        if len(x) > 4096:   # in bites: each one releases the GIL soon so the audio thread does not lag
            return np.concatenate([self(x[i:i + 4096]) for i in range(0, len(x), 4096)])
        b = np.concatenate([self.hist, x])
        total = self.n_in + len(x)
        m_last = (total * self.up - 1) // self.down
        m = np.arange(self.m_next, m_last + 1, dtype=np.int64)
        p = m * self.down
        li = p // self.up - (self.n_in - (self.k - 1))          # position in b of the most recent sample
        idx = li[:, None] - np.arange(self.k)[None, :]
        out = np.einsum("mj,mj->m", b[idx], self.taps[p % self.up])
        self.hist = b[len(b) - (self.k - 1):] if self.k > 1 else np.zeros(0, dtype=np.float32)
        self.n_in, self.m_next = total, m_last + 1
        return out.astype(np.float32)


class Player:
    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._current = np.zeros(0, dtype=np.float32)
        self._current_mutes = False
        self._lock = threading.Lock()
        self._stream = None
        self.rate = 48000
        self.device_name = None
        self.ok = False
        self._rs: dict[int, StreamResampler] = {}   # one per source rate (Edge 24 kHz, Piper 22 kHz)

    def open(self, device_name: str | None) -> None:
        self.close()
        self._rs = {}
        idx = resolve_device(device_name, "output")
        info = sd.query_devices(idx)
        self.rate = int(info["default_samplerate"])
        self.device_name = info["name"]
        self._stream = sd.OutputStream(device=idx, samplerate=self.rate, channels=1, dtype="float32",
                                       callback=self._callback, blocksize=int(self.rate * 0.02))
        self._stream.start()
        self.ok = True
        log.info("Audio output: %s @ %d Hz", self.device_name, self.rate)

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
        self._stream = None
        self.ok = False

    def _callback(self, outdata, frames, _time, status):
        out = outdata[:, 0]
        filled = 0
        with self._lock:
            while filled < frames:
                if len(self._current) == 0:
                    try:
                        self._current, self._current_mutes = self._queue.get_nowait()
                    except queue.Empty:
                        break
                take = min(frames - filled, len(self._current))
                out[filled:filled + take] = self._current[:take]
                self._current = self._current[take:]
                filled += take
        out[filled:] = 0

    def play(self, audio: np.ndarray, rate: int, mutes_mic: bool = True) -> float:
        """Queue audio and return its duration in seconds.

        mutes_mic=False for short sounds (the chime) that must not cut off what the user is saying.
        """
        rs = self._rs.get(rate)
        if rs is None or rs.dst != self.rate:
            rs = self._rs[rate] = StreamResampler(rate, self.rate)
        data = rs(np.asarray(audio, dtype=np.float32))
        self._queue.put((data, mutes_mic))
        return len(data) / self.rate

    def busy(self) -> bool:
        """Is something playing that should mute the mic?"""
        with self._lock:
            if len(self._current) > 0 and self._current_mutes:
                return True
            return any(m for _, m in list(self._queue.queue))

    def stop(self) -> None:
        with self._lock:
            self._current = np.zeros(0, dtype=np.float32)
            while not self._queue.empty():
                self._queue.get_nowait()
        for rs in self._rs.values():
            rs.reset()   # what was cut off must not leak into the start of the next audio


def chime(rate: int = 48000, up: bool = True) -> np.ndarray:
    """Soft two-note chime (rises on activation, falls on cancel)."""
    notes = (880.0, 1320.0) if up else (1100.0, 740.0)
    parts = []
    for f in notes:
        t = np.arange(int(rate * 0.07)) / rate
        env = np.minimum(1, t / 0.005) * np.exp(-t * 30)
        parts.append(0.22 * np.sin(2 * np.pi * f * t) * env)
    return np.concatenate(parts).astype(np.float32)


class Mic:
    def __init__(self):
        self._stream = None
        self._rate = 48000
        self._listeners: list[queue.Queue] = []
        self._raw: list[queue.Queue] = []
        self._lock = threading.Lock()
        self.muted = False
        self.device_name = None
        self.ok = False
        self.level = 0.0

    def open(self, device_name: str | None) -> None:
        self.close()
        idx = resolve_device(device_name, "input")
        info = sd.query_devices(idx)
        self._rate = int(info["default_samplerate"])
        self._rs = StreamResampler(self._rate, MIC_RATE)
        self.device_name = info["name"]
        # With WASAPI some mics (e.g. 2-channel arrays) give silence if opened in mono
        channels = max(1, min(int(info["max_input_channels"]), 2))
        self._stream = sd.InputStream(device=idx, samplerate=self._rate, channels=channels, dtype="float32",
                                      blocksize=int(self._rate * BLOCK_SECONDS), callback=self._callback)
        self._stream.start()
        self.ok = True
        log.info("Mic: %s @ %d Hz", self.device_name, self._rate)

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
        self._stream = None
        self.ok = False

    def _callback(self, indata, frames, _time, status):
        block = self._rs(indata.mean(axis=1))
        self.level = float(np.sqrt(np.mean(block ** 2))) if len(block) else 0.0
        with self._lock:
            for q in self._raw:
                q.put(block)
            if self.muted:
                return
            for q in self._listeners:
                q.put(block)

    def subscribe(self, raw: bool = False) -> queue.Queue:
        """raw=True: also receives audio while the mic is muted (to hear if you interrupt it)."""
        q: queue.Queue = queue.Queue()
        with self._lock:
            (self._raw if raw else self._listeners).append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._listeners:
                self._listeners.remove(q)
