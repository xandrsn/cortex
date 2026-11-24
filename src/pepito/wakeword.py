"""Detector de «Cortex» entrenado a medida (tools/train_wake.py): escucha la palabra continuamente.

Usa los embeddings de openWakeWord (uno cada 80 ms) y una red pequeña entrenada con miles de «Cortex»
sintéticos y las grabaciones de Charlie. No transcribe nada: salta en cuanto acaba la palabra.
"""

import collections
import logging
import time
import warnings
import wave

import numpy as np

from .config import MODELS_DIR, settings

log = logging.getLogger("wakeword")

MODEL_FILE = MODELS_DIR / "cortex_wake.npz"
REAL_DIR = MODELS_DIR / "wake_train_cortex"   # activaciones reales: pos_real (luego hablaste) y neg_real (falsas)
KEEP_REAL = 300
# Umbral de la red según la sensibilidad (1 baja · 2 media · 3 alta)
THRESHOLDS = {1: 0.95, 2: 0.85, 3: 0.7}
COOLDOWN_S = 2.0


class WakeWord:
    def __init__(self):
        self.available = MODEL_FILE.exists()
        self._recent = collections.deque(maxlen=int(2.0 * 16000 / 512) + 1)   # ~2 s de audio
        self._fired_clip = None
        self.fire_score = 0.0
        self.last_score = 0.0
        self._last_fire = -1e9
        self._clock = 0.0   # segundos de audio procesado: el cooldown va por audio, no por reloj
        self._thresholds = dict(THRESHOLDS)
        self._consec = 1
        self._streak = 0
        if not self.available:
            log.info("Sin modelo de «Cortex» entrenado; se usa el método por transcripción")
            return
        from openwakeword.utils import AudioFeatures
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self._features = AudioFeatures(ncpu=1)
        # openWakeWord guarda 10 s de audio crudo y lo convierte entero a lista cada 80 ms, aunque solo usa
        # los últimos 1280 + 480: con un búfer corto da lo mismo y gasta un tercio menos de CPU
        self._features.raw_data_buffer = collections.deque(maxlen=1280 * 2 + 480)
        m = np.load(MODEL_FILE)
        self._mean, self._std = m["mean"], m["std"]
        # umbrales elegidos al entrenar (por falsas alarmas/hora) y regla de N ventanas seguidas
        if "thresholds" in m.files:
            self._thresholds = {i + 1: float(t) for i, t in enumerate(m["thresholds"])}
        self._consec = int(m["consec"]) if "consec" in m.files else 1
        n = len([k for k in m.files if k.startswith("W")])
        self._layers = [(m[f"W{i}"], m[f"b{i}"]) for i in range(n)]
        self._buf = np.zeros(0, dtype=np.float32)
        log.info("Detector de «Cortex» cargado")

    def score(self, emb: np.ndarray) -> float:
        x = (emb.reshape(-1) - self._mean) / self._std
        for i, (W, b) in enumerate(self._layers):
            x = x @ W + b
            if i < len(self._layers) - 1:
                x = np.maximum(x, 0)
        return float(1 / (1 + np.exp(-x[0])))

    def process(self, chunk: np.ndarray) -> bool:
        """chunk: audio float32 a 16 kHz (cualquier tamaño). True si acaba de oír «Cortex»."""
        self._recent.append(chunk)
        self._clock += len(chunk) / 16000
        self._buf = np.concatenate([self._buf, chunk])
        if len(self._buf) < 1280:   # un embedding nuevo cada 1280 muestras (80 ms)
            return False
        block, self._buf = self._buf[:1280], self._buf[1280:]
        self._features((np.clip(block, -1, 1) * 32767).astype(np.int16))
        s = self.score(self._features.get_features(16))
        self.last_score = max(s, self.last_score * 0.9)
        now = self._clock
        self._streak = self._streak + 1 if s >= self._thresholds.get(int(settings["wake_sensitivity"]), 0.85) else 0
        if self._streak >= self._consec and now - self._last_fire > COOLDOWN_S:
            self._streak = 0
            self._last_fire = now
            self._fired_clip = np.concatenate(self._recent)
            self.fire_score = s
            log.info("¡Cortex! (detector, %.2f)", s)
            return True
        return False

    def verdict(self, real: bool) -> None:
        """Tras una activación: ¿iba en serio (hubo orden) o fue falsa? Se guarda para reentrenar."""
        clip, self._fired_clip = self._fired_clip, None
        if clip is None:
            return
        folder = REAL_DIR / ("pos_real" if real else "neg_real")
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with wave.open(str(folder / f"{time.strftime('%Y%m%d-%H%M%S')}.wav"), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes((np.clip(clip, -1, 1) * 32767).astype(np.int16).tobytes())
            for old in sorted(folder.glob("*.wav"))[:-KEEP_REAL]:
                old.unlink(missing_ok=True)
        except OSError as e:
            log.warning("No pude guardar la activación: %s", e)

    @property
    def fired_clip(self):
        return self._fired_clip

    def discard(self) -> None:
        self._fired_clip = None
