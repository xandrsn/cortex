"""Oído siempre activo: detección de voz (Silero) + palabra de activación.

En reposo, cada vez que alguien habla se corta ese trozo y se decide si va dirigido a Pepito con dos
pistas:
1. Texto: se transcribe (Parakeet, ~0,3 s) y se busca «Pepito» comparando por cómo suena, no por cómo
   se escribe («Baby too» y «Pepito» suenan igual).
2. Sonido: si el texto no lo dice, se compara el audio con las grabaciones de «Pepito» (wakeprint).

Si aparece entre las primeras palabras o al final, lo que venga con él es la orden («Pepito, pon
música»); si solo se dijo «Pepito», pasa a escucha activa.

En escucha activa (tras el botón o un «Pepito» a secas) se graba hasta que el usuario se calla y se
entrega el audio.
"""

import collections
import concurrent.futures
import logging
import queue
import re
import threading
import time
import unicodedata
from difflib import SequenceMatcher
from typing import Callable

import numpy as np
import onnxruntime as ort

from .audio import Mic
from .config import MODELS_DIR, settings
from .stt import Transcriber, normalize
from .wakeprint import WakePrint
from .wakeword import WakeWord

log = logging.getLogger("listener")

RATE = 16000
SURE_SCORE = 0.995          # por encima, el detector no necesita segunda opinión
CHUNK = 512                  # 32 ms, lo que espera Silero v5
CHUNK_S = CHUNK / RATE
CONTEXT = 64

# Claves fonéticas que cuentan como «Pepito» con sensibilidad media (ver _phonetic)
WAKE_KEYS = {"pepito", "papito", "pipito", "pepita", "pepitos", "pepeto", "popito"}
VAD_THRESHOLD = {1: 0.6, 2: 0.5, 3: 0.4}
SPEC_AFTER = 0.3   # s de silencio tras los que se empieza a transcribir «por si acaso» (el corte es a los 0,7)


class SileroVad:
    def __init__(self):
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        self._sess = ort.InferenceSession(str(MODELS_DIR / "vad" / "silero_vad.onnx"), opts,
                                          providers=["CPUExecutionProvider"])
        self._sr = np.array(RATE, dtype=np.int64)
        self.reset()

    def reset(self):
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT, dtype=np.float32)

    def __call__(self, chunk: np.ndarray) -> float:
        x = np.concatenate([self._context, chunk]).astype(np.float32)[None, :]
        out, self._state = self._sess.run(None, {"input": x, "state": self._state, "sr": self._sr})
        self._context = chunk[-CONTEXT:]
        return float(out[0][0])


def _norm(word: str) -> str:
    word = unicodedata.normalize("NFKD", word.lower())
    return re.sub(r"[^a-z]", "", "".join(c for c in word if not unicodedata.combining(c)))


def _phonetic(word: str) -> str:
    """Clave de cómo suena: «Baby too» → «papito», «Bepito» → «pepito»."""
    w = word.replace("ph", "f").replace("ee", "i").replace("ea", "i").replace("oo", "o").replace("ou", "o")
    w = w.replace("h", "").translate(str.maketrans("bvyu", "ppio"))
    return re.sub(r"(.)\1+", r"\1", w)


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def find_wake(text: str, sensitivity: int) -> str | None:
    """Si el texto va dirigido a Pepito devuelve la orden que lo acompaña ("" si no hay); si no, None."""
    spans = [m.span() for m in re.finditer(r"[^\W_]+(?:'[^\W_]+)?", text)]
    keys = [_phonetic(_norm(text[a:b])) for a, b in spans]
    # «pepi to», «baby too»: une parejas de palabras
    joined = [_phonetic(keys[i] + keys[i + 1]) if i + 1 < len(keys) else "" for i in range(len(keys))]

    def is_wake(i: int) -> int:
        """Cuántas palabras ocupa la palabra de activación en la posición i (0 si no está)."""
        for candidate, size in ((keys[i], 1), (joined[i], 2)):
            if not candidate:
                continue
            if sensitivity == 1 and candidate == "pepito":
                return size
            if sensitivity == 2 and candidate in WAKE_KEYS:
                return size
            if sensitivity >= 3 and candidate[:1] == "p" and 5 <= len(candidate) <= 8 and \
                    min(_edit_distance(candidate, k) for k in WAKE_KEYS) <= 1:
                return size
        return 0

    for i in range(min(3, len(keys))):
        size = is_wake(i)
        if size:
            return text[spans[i + size - 1][1]:].lstrip(" ,.;:!?¡¿").strip()
    if len(keys) > 3:
        for i in (len(keys) - 1, len(keys) - 2):
            size = is_wake(i)
            if size and i + size == len(keys):
                return text[:spans[i][0]].rstrip(" ,.;:").strip()
    return None


class Listener:
    def __init__(self, mic: Mic, stt: Transcriber, on_wake: Callable[[str, str], None],
                 on_command: Callable[..., None], on_timeout: Callable[[], None]):
        self.mic = mic
        self.stt = stt
        self.on_wake = on_wake
        self.on_command = on_command
        self.on_timeout = on_timeout
        self.vad = SileroVad()
        self.print = WakePrint()
        self.detector = WakeWord()
        self.active = False
        self.last_heard: dict = {}   # última frase oída en reposo, solo para el panel (no se guarda)
        self._finish = False
        self._active_since = 0.0
        self._timeout = 6.0
        # transcripción especulativa: al notar una pausa se transcribe ya; si la frase acaba ahí, el texto
        # está listo cuando se da por terminada (ahorra lo que tarda Parakeet, ~0,3-0,5 s)
        self._pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="stt-spec")
        self._spec: tuple[int, concurrent.futures.Future] | None = None
        self._reset()
        threading.Thread(target=self._run, daemon=True, name="listener").start()

    # ---------- control (desde el orquestador) ----------
    def activate(self, timeout: float = 6.0):
        """Escucha activa: si en `timeout` segundos no empieza a hablar, se rinde."""
        if not (self.active and self._in_speech):   # tras el detector puede que ya esté hablando: no cortar
            self._reset()
        self._finish = False
        self._active_since = time.monotonic()
        self._timeout = timeout
        self.active = True

    def finish(self):
        self._finish = True

    def reload_detector(self):
        self.detector = WakeWord()

    def deactivate(self):
        self.active = False
        self._reset()

    # ---------- bucle ----------
    def _reset(self):
        self._preroll = collections.deque(maxlen=10)
        self._segment: list[np.ndarray] = []
        self._in_speech = False
        self._voiced = 0
        self._silence = 0
        self._spec = None

    def _run(self):
        q = self.mic.subscribe()
        buf = np.zeros(0, dtype=np.float32)
        last_block = time.monotonic()
        vad_live = True
        while True:
            try:
                block = q.get(timeout=0.2)
            except queue.Empty:
                block = None
            now = time.monotonic()
            if block is None or now - last_block > 0.3:
                # micro silenciado (Pepito hablando) o sin datos: lo que hubiera a medias no vale
                if not self.active and (self._in_speech or self._segment):
                    self._reset()
                    self.vad.reset()
            if block is None:
                try:
                    self._check_active_timeout(now)
                except Exception:
                    log.exception("Error en el oído")
                continue
            last_block = now
            buf = np.concatenate([buf, block])
            while len(buf) >= CHUNK:
                chunk, buf = buf[:CHUNK], buf[CHUNK:]
                try:
                    detect = self.detector.available and not self.active and settings["wake_enabled"]
                    if detect and self.detector.process(chunk):
                        self._detector_wake()
                        continue
                    # en reposo con detector (o sin palabra de activación) los trozos de voz no se usan:
                    # sin Silero se ahorra CPU; al volver a hacer falta empieza de cero
                    if not (self.active or (settings["wake_enabled"] and not self.detector.available)):
                        if vad_live:
                            vad_live = False
                            self._reset()
                            self.vad.reset()
                        continue
                    vad_live = True
                    self._step(chunk, self.vad(chunk))
                except Exception:
                    log.exception("Error en el oído")
                    self._reset()
            try:
                self._check_active_timeout(now)
            except Exception:
                log.exception("Error en el oído")

    def _check_active_timeout(self, now: float):
        if not self.active:
            return
        if not self._in_speech and (self._finish or now - self._active_since > self._timeout):
            self.active = False
            self._reset()
            self.detector.verdict(real=False)   # se activó y no dijiste nada: probable falsa alarma
            self.on_timeout()

    def _step(self, chunk: np.ndarray, prob: float):
        sens = int(settings["wake_sensitivity"])
        threshold = VAD_THRESHOLD.get(sens, 0.5)
        if not self._in_speech:
            self._preroll.append(chunk)
            self._voiced = self._voiced + 1 if prob >= threshold else 0
            if self._voiced >= 3:
                self._in_speech = True
                self._segment = list(self._preroll)
                self._silence = 0
            return

        self._segment.append(chunk)
        self._silence = self._silence + 1 if prob < threshold - 0.15 else 0
        if self._silence == 0:
            self._spec = None   # sigue hablando: lo especulado ya no vale
        elif (self.active and self._spec is None and self._silence * CHUNK_S >= SPEC_AFTER
              and len(self._segment) * CHUNK_S >= 0.5):
            n = len(self._segment)
            audio = np.concatenate(self._segment)
            self._spec = (n, self._pool.submit(self.stt.transcribe_command, audio, "es", False))
        seconds = len(self._segment) * CHUNK_S
        end_after = 0.7 if self.active else 0.5
        max_len = 20.0 if self.active else 7.0
        if self._silence * CHUNK_S >= end_after or seconds >= max_len or (self.active and self._finish):
            audio = np.concatenate(self._segment)
            was_active = self.active
            spec = self._spec[1] if self._spec else None
            self._reset()
            if was_active:
                self.active = False
                self.detector.verdict(real=True)
                self.on_command(audio, spec)
            elif settings["wake_enabled"] and not self.detector.available:
                self._check_wake(audio)

    def _verify(self) -> tuple[bool, str]:
        """Segunda opinión cuando el detector no está muy seguro: transcripción o huella de voz deben estar de acuerdo."""
        score, clip = self.detector.fire_score, self.detector.fired_clip
        if score >= SURE_SCORE or clip is None:
            return True, ""
        sens = int(settings["wake_sensitivity"])
        audio = normalize(clip[-int(1.6 * RATE):])
        text = self.stt.transcribe(audio, "es")[0]
        if find_wake(text, sens) is not None:
            return True, text
        dist = self.print.match(audio)[0]
        return dist <= self.print.threshold(sens) + 0.04, text

    def _detector_wake(self):
        """El detector ha oído «Pepito»: la orden se graba ya, sin esperar a que acabe la frase."""
        t = time.monotonic()
        ok, text = self._verify()
        self.last_heard = {"text": text or "Pepito", "distance": round(self.detector.fire_score, 2), "wake": ok,
                           "time": time.time()}
        if not ok:
            log.info("Detector descartado por la segunda opinión (%.2f, %r, %.2fs)", self.detector.fire_score, text,
                     time.monotonic() - t)
            self.detector.discard()
            return
        self._reset()
        self.vad.reset()
        self._finish = False
        self._active_since = time.monotonic()
        self._timeout = 6.0
        self.active = True
        self.on_wake("", "es")

    def _check_wake(self, audio: np.ndarray):
        if len(audio) < RATE * 0.35:
            return
        audio = normalize(audio)
        t = time.monotonic()
        sens = int(settings["wake_sensitivity"])
        text, lang = self.stt.transcribe(audio, settings["notice_language"])
        rest = find_wake(text, sens) if text else None
        dist, start, end = self.print.match(audio)
        how = "texto"
        if rest is None:
            limit = self.print.threshold(sens) + (0.03 if self.print.user_samples < 3 else 0.0)
            if dist <= limit:
                how = "sonido"
                rest = self._split_command(audio, start, end)
        self.last_heard = {"text": text, "distance": round(dist, 3), "wake": rest is not None, "time": time.time()}
        if rest is not None:
            # solo se registra lo que va dirigido a Pepito; lo demás que se oye en casa no se guarda
            log.info("¡Pepito! por %s (%.2fs, dist %.3f): %r → %r", how, time.monotonic() - t, dist, text, rest)
            self.on_wake(rest, lang)

    def _split_command(self, audio: np.ndarray, start: float, end: float) -> str:
        """Cuando «Pepito» se reconoce por el sonido, transcribe lo que va antes o después."""
        total = len(audio) / RATE
        if total - end > 0.4:
            return self.stt.transcribe(audio[int(end * RATE):], settings["notice_language"])[0]
        if start > 0.6:
            return self.stt.transcribe(audio[: int(start * RATE)], settings["notice_language"])[0]
        return ""


STOP_WORDS = {"para", "calla", "callate", "basta", "stop", "espera", "vale", "ya"}


class BargeIn:
    """Mientras Pepito habla: ¿le está hablando Charlie por encima?

    No hay cancelación de eco, así que el micro también oye a Pepito. Cada trozo de voz se transcribe y se
    descarta si sus palabras son las que Pepito está diciendo. Solo trabaja mientras `enabled`.
    """

    def __init__(self, mic: Mic, stt: Transcriber, on_barge: Callable[[str, bool], None]):
        self.mic = mic
        self.stt = stt
        self.on_barge = on_barge
        self.enabled = False
        self._echo: collections.deque = collections.deque()   # (momento, palabras que Pepito ha dicho)
        self._lock = threading.Lock()
        # el detector de «Pepito» también aquí: oye la palabra aunque la transcripción salga mal con su voz encima
        self.detector = WakeWord()
        threading.Thread(target=self._run, daemon=True, name="barge-in").start()

    def said(self, text: str):
        now = time.monotonic()
        with self._lock:
            self._echo.append((now, {w for w in map(_norm, text.split()) if w}))
            while self._echo and now - self._echo[0][0] > 30:
                self._echo.popleft()

    def _echo_words(self, within: float = 30) -> set[str]:
        now = time.monotonic()
        with self._lock:
            return set().union(*(w for t, w in self._echo if now - t <= within)) if self._echo else set()

    def _wake_heard(self, block: np.ndarray) -> bool:
        """«Pepito» dicho por encima (si no es él mismo diciendo su nombre)."""
        det = self.detector
        if not det.available or not det.process(block):
            return False
        if "pepito" in self._echo_words(within=8):
            log.info("¿Interrupción? «Pepito» del detector (%.2f) → descartada", det.fire_score)
            return False
        log.info("¿Interrupción? «Pepito» del detector (%.2f) · SÍ", det.fire_score)
        return True

    def _run(self):
        q = self.mic.subscribe(raw=True)
        vad = SileroVad()
        buf = np.zeros(0, dtype=np.float32)
        segment: list[np.ndarray] = []
        preroll: collections.deque = collections.deque(maxlen=6)
        voiced = silence = 0
        was_enabled = False
        while True:
            block = q.get()
            if not (self.enabled and settings["barge_in"]):
                if was_enabled:
                    self.detector._streak = 0
                    buf, segment, voiced, silence = np.zeros(0, dtype=np.float32), [], 0, 0
                    preroll.clear()
                    vad.reset()
                was_enabled = False
                continue
            was_enabled = True
            if self._wake_heard(block):
                self.enabled = False
                self.on_barge("", False)   # se calla y le escucha
                buf, segment, voiced, silence = np.zeros(0, dtype=np.float32), [], 0, 0
                preroll.clear()
                continue
            buf = np.concatenate([buf, block])
            while len(buf) >= CHUNK:
                chunk, buf = buf[:CHUNK], buf[CHUNK:]
                try:
                    prob = vad(chunk)
                except Exception:
                    log.exception("Error en el VAD de interrupciones")
                    continue
                if not segment:
                    preroll.append(chunk)
                    voiced = voiced + 1 if prob >= 0.5 else 0
                    if voiced >= 4:
                        segment, silence = list(preroll), 0
                    continue
                segment.append(chunk)
                silence = silence + 1 if prob < 0.35 else 0
                if silence * CHUNK_S >= 0.4 or len(segment) * CHUNK_S >= 3.0:
                    audio = np.concatenate(segment)
                    segment, voiced = [], 0
                    preroll.clear()
                    try:
                        self._judge(audio)
                    except Exception:
                        log.exception("Error mirando una interrupción")

    def _judge(self, audio: np.ndarray):
        if len(audio) < RATE * 0.35:
            return
        text, lang = self.stt.transcribe(normalize(audio), "es")
        text = text.strip()
        words = [w for w in map(_norm, text.split()) if w]
        if not words or not self.enabled:
            return
        if lang != "es":   # su propia voz mal transcrita suele salir como inglés sin sentido
            log.info("¿Interrupción? %r → descartada (%s)", text, lang)
            return
        echo = self._echo_words()
        novel = [w for w in words
                 if w not in echo and not any(SequenceMatcher(None, w, e).ratio() >= 0.75 for e in echo)]
        share = len(novel) / len(words)
        wake = find_wake(text, int(settings["wake_sensitivity"])) is not None and "pepito" not in echo
        stop = len(words) <= 2 and any(w in STOP_WORDS for w in novel)
        barge = wake or stop or (len(novel) >= 3 and share >= 0.6)
        log.info("¿Interrupción? %r → nuevas %d/%d%s", text, len(novel), len(words), " · SÍ" if barge else "")
        if barge:
            self.enabled = False
            # si casi todo lo oído es de Charlie (y no es solo «para»), se usa como orden; si no, se le vuelve a escuchar
            clean = share >= 0.8 and not stop and len(words) >= 2
            self.on_barge(text if clean else "", stop and not wake)
