"""Entrada y salida de audio con WASAPI.

- Player: un OutputStream persistente al que se le encolan trozos de audio. Al estar siempre abierto,
  el Echo por Bluetooth no se duerme y no se come las primeras sílabas.
- Mic: un InputStream persistente que reparte bloques de 16 kHz mono a quien escuche (el oído).
  Mientras Cortex habla, el micro se ignora para que no se escuche a sí mismo.
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
    raise RuntimeError("WASAPI no disponible")


def refresh_devices() -> None:
    """PortAudio no se entera de dispositivos nuevos (Bluetooth) sin reiniciarse."""
    sd._terminate()
    sd._initialize()


def list_devices() -> dict:
    api = _wasapi_index()
    inputs, outputs = [], []
    for d in sd.query_devices():
        if d["hostapi"] != api:
            continue
        # los micros manos libres Bluetooth no sirven (calidad de teléfono): ni se ofrecen
        if d["max_input_channels"] > 0 and not _is_handsfree(d["name"]):
            inputs.append(d["name"])
        if d["max_output_channels"] > 0:
            outputs.append(d["name"])
    return {"inputs": inputs, "outputs": outputs}


def resolve_device(name: str | None, kind: str) -> int:
    """Índice WASAPI del dispositivo con ese nombre, o el predeterminado si no está."""
    api = _wasapi_index()
    if kind == "input" and name and _is_handsfree(name):
        name = None
    if name:
        for i, d in enumerate(sd.query_devices()):
            channels = d["max_input_channels"] if kind == "input" else d["max_output_channels"]
            if d["hostapi"] == api and channels > 0 and d["name"] == name:
                return i
        log.warning("Dispositivo %r no encontrado, uso el predeterminado", name)
    key = "default_input_device" if kind == "input" else "default_output_device"
    default = sd.query_hostapis(api)[key]
    if kind == "input" and _is_handsfree(sd.query_devices(default)["name"]):
        # Los micros manos libres Bluetooth graban como un teléfono y además ponen el altavoz en
        # calidad de llamada: mejor el micro del portátil si lo hay.
        for i, d in enumerate(sd.query_devices()):
            if d["hostapi"] == api and d["max_input_channels"] > 0 and not _is_handsfree(d["name"]):
                return i
    return default


def _is_handsfree(name: str) -> bool:
    return bool(re.search(r"headset|hands-free|manos libres", name, re.I))


class WindowsMicMute:
    """Vigila si Windows tiene el micro silenciado y permite reactivarlo (vía el proceso micmute)."""

    def __init__(self):
        self.muted: bool | None = None
        self.outputs: str | None = None   # altavoces activos en Windows (cambia al conectar/desconectar)
        self._proc = None
        threading.Thread(target=self._run, daemon=True, name="mic-mute").start()

    def unmute(self) -> None:
        try:
            self._proc.stdin.write(b"unmute\n")
            self._proc.stdin.flush()
        except Exception as e:
            log.warning("No puedo reactivar el micro: %s", e)

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
                log.warning("Fallo vigilando el micro de Windows: %s", e)
            finally:
                # si el proceso muere se relanza; sin cerrar sus tuberías se perderían handles en cada vuelta
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
    """Cambio de frecuencia en continuo y con filtro (polifase): sin los agudos falsos de la interpolación
    lineal al subir (24 kHz de Edge → 48 kHz del altavoz) ni el ruido que se cuela al bajar sin filtrar
    (48 kHz del micro → 16 kHz de Parakeet). Guarda el estado entre trozos: sin saltos en las uniones.
    Vale para cualquier pareja de frecuencias (también 44,1 kHz de muchos altavoces Bluetooth)."""

    def __init__(self, src: int, dst: int):
        from math import gcd
        from scipy.signal import firwin
        g = gcd(src, dst)
        self.src, self.dst, self.up, self.down = src, dst, dst // g, src // g
        if src == dst:
            return
        n = 24 * max(self.up, self.down) + 1
        h = firwin(n, 0.95 / max(self.up, self.down), window=("kaiser", 7.0)) * self.up
        self.k = -(-n // self.up)   # coeficientes por fase
        h = np.concatenate([h, np.zeros(self.k * self.up - n)])
        self.taps = h.reshape(self.k, self.up).T.astype(np.float32)   # taps[r, j] = h[r + j·up]
        self.reset()

    def reset(self) -> None:
        if self.src != self.dst:
            self.hist = np.zeros(self.k - 1, dtype=np.float32)
            self.n_in = 0      # muestras de entrada ya recibidas
            self.m_next = 0    # siguiente muestra de salida

    def __call__(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32)
        if self.src == self.dst or len(x) == 0:
            return x if self.src == self.dst else np.zeros(0, dtype=np.float32)
        if len(x) > 4096:   # a bocados: cada uno suelta pronto el GIL y el hilo del audio no se retrasa
            return np.concatenate([self(x[i:i + 4096]) for i in range(0, len(x), 4096)])
        b = np.concatenate([self.hist, x])
        total = self.n_in + len(x)
        m_last = (total * self.up - 1) // self.down
        m = np.arange(self.m_next, m_last + 1, dtype=np.int64)
        p = m * self.down
        li = p // self.up - (self.n_in - (self.k - 1))          # posición en b de la muestra más reciente
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
        self._rs: dict[int, StreamResampler] = {}   # uno por frecuencia de origen (Edge 24 kHz, Piper 22 kHz)

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
        log.info("Salida de audio: %s @ %d Hz", self.device_name, self.rate)

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
        """Encola audio y devuelve su duración en segundos.

        mutes_mic=False para sonidos cortos (el pitido) que no deben cortar lo que el usuario dice.
        """
        rs = self._rs.get(rate)
        if rs is None or rs.dst != self.rate:
            rs = self._rs[rate] = StreamResampler(rate, self.rate)
        data = rs(np.asarray(audio, dtype=np.float32))
        self._queue.put((data, mutes_mic))
        return len(data) / self.rate

    def busy(self) -> bool:
        """¿Está sonando algo que deba silenciar el micro?"""
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
            rs.reset()   # lo cortado no debe colarse al principio de lo siguiente


def chime(rate: int = 48000, up: bool = True) -> np.ndarray:
    """Pitido suave de dos notas (sube al activarse, baja al cancelar)."""
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
        # Con WASAPI hay micros (p. ej. los arrays de 2 canales) que dan silencio si se abren en mono
        channels = max(1, min(int(info["max_input_channels"]), 2))
        self._stream = sd.InputStream(device=idx, samplerate=self._rate, channels=channels, dtype="float32",
                                      blocksize=int(self._rate * BLOCK_SECONDS), callback=self._callback)
        self._stream.start()
        self.ok = True
        log.info("Micro: %s @ %d Hz", self.device_name, self._rate)

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
        """raw=True: recibe el audio también con el micro silenciado (para oír si le interrumpes)."""
        q: queue.Queue = queue.Queue()
        with self._lock:
            (self._raw if raw else self._listeners).append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._listeners:
                self._listeners.remove(q)
