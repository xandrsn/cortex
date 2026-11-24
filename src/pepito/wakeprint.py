"""Huella acústica de «Cortex»: compara lo que se oye con grabaciones de la propia palabra.

Usa los modelos de openWakeWord (espectrograma + embeddings de voz de Google, pensados para palabras de
activación con pocos ejemplos) y DTW de subsecuencia: busca el trozo del audio que más se parece a
alguna plantilla. Las plantillas son las muestras que graba el usuario desde Ajustes (en
C:la carpeta del programa\\wake, fuera de Drive) y, de base, unas cuantas voces sintéticas.
"""

import asyncio
import logging
import threading
import wave
from pathlib import Path

import numpy as np

from .config import MODELS_DIR, ROOT

log = logging.getLogger("wakeprint")

SAMPLES_DIR = ROOT / "wake_cortex"
DEFAULTS_DIR = MODELS_DIR / "wake_cortex_default"
RATE = 16000
PAD = 4000                 # 0,25 s de silencio a cada lado (el modelo mira ventanas de 0,775 s)
FRAME_S = 0.08
WINDOW_S = 0.775
# Distancia máxima para dar por buena la palabra, según la sensibilidad (1 baja · 2 media · 3 alta)
THRESHOLDS = {1: 0.10, 2: 0.13, 3: 0.16}
DEFAULT_VOICES = [("es-ES-AlvaroNeural", "+0%"), ("es-ES-AlvaroNeural", "+15%"), ("es-ES-ElviraNeural", "+0%"),
                  ("es-ES-ElviraNeural", "-10%"), ("es-MX-JorgeNeural", "+0%"), ("es-ES-XimenaNeural", "+10%")]


def trim(audio: np.ndarray, margin: float = 0.08) -> np.ndarray:
    """Recorta el silencio de los extremos por energía."""
    frame = 320
    if len(audio) < frame * 3:
        return audio
    energy = np.sqrt(np.mean(audio[: len(audio) // frame * frame].reshape(-1, frame) ** 2, axis=1))
    active = np.where(energy > max(energy.max() * 0.12, 0.003))[0]
    if not len(active):
        return audio
    start = max(0, active[0] * frame - int(margin * RATE))
    end = min(len(audio), (active[-1] + 1) * frame + int(margin * RATE))
    return audio[start:end]


def _write_wav(path: Path, audio: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())


def _read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32767


class WakePrint:
    def __init__(self):
        from openwakeword.utils import AudioFeatures
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self._features = AudioFeatures(ncpu=1)
        self._lock = threading.Lock()
        self._templates: list[np.ndarray] = []
        self.user_samples = 0
        SAMPLES_DIR.mkdir(exist_ok=True)
        self._ensure_defaults()
        self.reload()

    # ---------- plantillas ----------
    def _ensure_defaults(self) -> None:
        DEFAULTS_DIR.mkdir(parents=True, exist_ok=True)
        if any(DEFAULTS_DIR.glob("*.wav")):
            return
        try:
            import edge_tts
            import miniaudio

            async def make(voice: str, rate: str) -> np.ndarray:
                mp3 = b""
                async for chunk in edge_tts.Communicate("Cortex", voice, rate=rate).stream():
                    if chunk["type"] == "audio":
                        mp3 += chunk["data"]
                d = miniaudio.decode(mp3, output_format=miniaudio.SampleFormat.FLOAT32, nchannels=1, sample_rate=RATE)
                return trim(np.asarray(d.samples, dtype=np.float32))

            for i, (voice, rate) in enumerate(DEFAULT_VOICES):
                _write_wav(DEFAULTS_DIR / f"{i}.wav", asyncio.run(make(voice, rate)))
            log.info("Plantillas base de «Cortex» generadas")
        except Exception as e:
            log.warning("No pude generar las plantillas base: %s", e)

    def reload(self) -> None:
        user = sorted(SAMPLES_DIR.glob("*.wav"))
        files = user + sorted(DEFAULTS_DIR.glob("*.wav"))
        templates = [self.embed(_read_wav(f)) for f in files]
        with self._lock:
            self._templates = [t for t in templates if len(t)]
            self.user_samples = len(user)

    def add_sample(self, audio: np.ndarray) -> int:
        from .stt import normalize
        clip = normalize(trim(audio))
        n = len(list(SAMPLES_DIR.glob("*.wav")))
        _write_wav(SAMPLES_DIR / f"cortex_{n + 1:02d}.wav", clip)
        self.reload()
        return self.user_samples

    def clear_samples(self) -> None:
        for f in SAMPLES_DIR.glob("*.wav"):
            f.unlink()
        self.reload()

    # ---------- comparación ----------
    def embed(self, audio: np.ndarray) -> np.ndarray:
        padded = np.concatenate([np.zeros(PAD, np.float32), audio.astype(np.float32), np.zeros(PAD, np.float32)])
        if len(padded) < RATE:
            padded = np.concatenate([padded, np.zeros(RATE - len(padded), np.float32)])
        emb = self._features._get_embeddings((np.clip(padded, -1, 1) * 32767).astype(np.int16))
        emb = np.asarray(emb, dtype=np.float32)
        return emb / (np.linalg.norm(emb, axis=1, keepdims=True) + 1e-9)

    @staticmethod
    def _subsequence_dtw(q: np.ndarray, s: np.ndarray) -> tuple[float, int, int]:
        """Mejor encaje de q dentro de s: (distancia media, frame inicial, frame final) en s."""
        cost = 1 - q @ s.T
        n, m = cost.shape
        acc = np.full((n + 1, m + 1), np.inf)
        acc[0, :] = 0
        start = np.zeros((n + 1, m + 1), dtype=np.int32)
        start[0, :] = np.arange(-1, m)
        for i in range(1, n + 1):
            for j in range(1, m + 1):
                options = (acc[i - 1, j], acc[i, j - 1], acc[i - 1, j - 1])
                k = int(np.argmin(options))
                acc[i, j] = cost[i - 1, j - 1] + options[k]
                if i == 1 and k != 1:
                    start[i, j] = j - 1          # el encaje empieza aquí
                else:
                    start[i, j] = (start[i - 1, j], start[i, j - 1], start[i - 1, j - 1])[k]
        end = int(np.argmin(acc[n, 1:])) + 1
        return float(acc[n, end] / n), int(start[n, end]), end - 1

    def match(self, audio: np.ndarray) -> tuple[float, float, float]:
        """(distancia, inicio s, fin s) del trozo que más se parece a «Cortex»."""
        with self._lock:
            templates = list(self._templates)
        if not templates:
            return 1.0, 0.0, 0.0
        seg = self.embed(audio)
        best = (1.0, 0, 0)
        for t in templates:
            result = self._subsequence_dtw(t, seg)
            if result[0] < best[0]:
                best = result
        dist, f0, f1 = best
        pad_s = PAD / RATE
        start = max(0.0, f0 * FRAME_S + 0.2 - pad_s)
        end = min(len(audio) / RATE, f1 * FRAME_S + WINDOW_S - 0.15 - pad_s)
        return dist, start, end

    def threshold(self, sensitivity: int) -> float:
        return THRESHOLDS.get(sensitivity, THRESHOLDS[2])
