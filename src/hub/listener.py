"""Always-on listener: voice detection (Silero) + wake word.

At rest, every time someone speaks that chunk is cut out and two clues decide whether it is addressed to Cortex:
1. Text: it is transcribed (Parakeet, ~0.3 s) and "Cortex" is searched by how it sounds, not how it is
   spelled ("Kortecs" and "Cortex" sound the same).
2. Sound: if the text does not say it, the audio is compared with the recordings of "Cortex" (wakeprint).

If it appears among the first words or at the end, whatever comes with it is the command ("Cortex, pon
música"); if only "Cortex" was said, it switches to active listening.

In active listening (after the button or a bare "Cortex") it records until the user stops talking and
hands over the audio.
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
from pathlib import Path
from typing import Callable

import numpy as np
import onnxruntime as ort

from .audio import Mic
from .config import settings
from .stt import Transcriber, normalize
from .wakeprint import WakePrint
from .wakeword import WakeWord

log = logging.getLogger("listener")

RATE = 16000
SURE_SCORE = 0.995          # above this, the detector needs no second opinion
CHUNK = 512                  # 32 ms, what Silero v5 expects
CHUNK_S = CHUNK / RATE
CONTEXT = 64

# Phonetic keys that count as "Cortex" at medium sensitivity (see _phonetic)
WAKE_KEYS = {"cortex", "kortex", "qortex", "qoartex", "cortecs", "corteks", "kortecs", "korteks", "cortec", "kortek",
             "cortek", "coretex", "cordex", "cortix"}
# at high sensitivity it must end the way it sounds: otherwise it would fire on "corte", "cortes" or "corto"
WAKE_SHAPE = re.compile(r"[ckgq][a-z]{2,5}(?:x|[ck]s?)")
VAD_THRESHOLD = {1: 0.6, 2: 0.5, 3: 0.4}
SPEC_AFTER = 0.3   # s of silence after which it starts transcribing "just in case" (the cut is at 0.7)


class SileroVad:
    def __init__(self):
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        self._sess = ort.InferenceSession(str(Path(__file__).with_name("silero_vad.onnx")), opts,
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
    """Key for how it sounds: "Korthex" -> "kortex"."""
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
    """If the text is addressed to Cortex return the accompanying command ("" if none); otherwise None."""
    spans = [m.span() for m in re.finditer(r"[^\W_]+(?:'[^\W_]+)?", text)]
    keys = [_phonetic(_norm(text[a:b])) for a, b in spans]
    # "cor tex", "core tex": join pairs of words
    joined = [_phonetic(keys[i] + keys[i + 1]) if i + 1 < len(keys) else "" for i in range(len(keys))]

    def is_wake(i: int) -> int:
        """How many words the wake word takes at position i (0 if absent)."""
        for candidate, size in ((keys[i], 1), (joined[i], 2)):
            if not candidate:
                continue
            if sensitivity == 1 and candidate == "cortex":
                return size
            if sensitivity == 2 and candidate in WAKE_KEYS:
                return size
            if sensitivity >= 3 and WAKE_SHAPE.fullmatch(candidate) and \
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
        self.last_heard: dict = {}   # last phrase heard at rest, only for the dashboard (not stored)
        self._finish = False
        self._active_since = 0.0
        self._timeout = 6.0
        # speculative transcription: on noticing a pause it transcribes right away; if the sentence ends there,
        # the text is ready when it is declared finished (saves Parakeet's latency, ~0.3-0.5 s)
        self._pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="stt-spec")
        self._spec: tuple[int, concurrent.futures.Future] | None = None
        self._reset()
        threading.Thread(target=self._run, daemon=True, name="listener").start()

    # ---------- control (from the orchestrator) ----------
    def activate(self, timeout: float = 6.0):
        """Active listening: gives up if speech does not start within `timeout` seconds."""
        if not (self.active and self._in_speech):   # after the detector the user may already be speaking: do not cut
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

    # ---------- loop ----------
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
                # mic muted (Cortex speaking) or no data: anything half-captured is void
                if not self.active and (self._in_speech or self._segment):
                    self._reset()
                    self.vad.reset()
            if block is None:
                try:
                    self._check_active_timeout(now)
                except Exception:
                    log.exception("Error in the listener")
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
                    # at rest with the detector (or without a wake word) voice chunks are not used:
                    # skipping Silero saves CPU; when needed again it starts from scratch
                    if not (self.active or (settings["wake_enabled"] and not self.detector.available)):
                        if vad_live:
                            vad_live = False
                            self._reset()
                            self.vad.reset()
                        continue
                    vad_live = True
                    self._step(chunk, self.vad(chunk))
                except Exception:
                    log.exception("Error in the listener")
                    self._reset()
            try:
                self._check_active_timeout(now)
            except Exception:
                log.exception("Error in the listener")

    def _check_active_timeout(self, now: float):
        if not self.active:
            return
        if not self._in_speech and (self._finish or now - self._active_since > self._timeout):
            self.active = False
            self._reset()
            self.detector.verdict(real=False)   # it activated and you said nothing: probably a false alarm
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
            self._spec = None   # still talking: the speculation is void
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
        """Second opinion when the detector is unsure: the transcription or the voice print must agree."""
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
        """The detector heard "Cortex": the command is recorded right away, without waiting for the sentence to end."""
        t = time.monotonic()
        ok, text = self._verify()
        self.last_heard = {"text": text or "Cortex", "distance": round(self.detector.fire_score, 2), "wake": ok,
                           "time": time.time()}
        if not ok:
            log.info("Detector rejected by the second opinion (%.2f, %r, %.2fs)", self.detector.fire_score, text,
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
        how = "text"
        if rest is None:
            limit = self.print.threshold(sens) + (0.03 if self.print.user_samples < 3 else 0.0)
            if dist <= limit:
                how = "sound"
                rest = self._split_command(audio, start, end)
        self.last_heard = {"text": text, "distance": round(dist, 3), "wake": rest is not None, "time": time.time()}
        if rest is not None:
            # only what is addressed to Cortex is logged; anything else heard at home is not stored
            log.info("Cortex! via %s (%.2fs, dist %.3f): %r -> %r", how, time.monotonic() - t, dist, text, rest)
            self.on_wake(rest, lang)

    def _split_command(self, audio: np.ndarray, start: float, end: float) -> str:
        """When "Cortex" is recognized by sound, transcribe what comes before or after."""
        total = len(audio) / RATE
        if total - end > 0.4:
            return self.stt.transcribe(audio[int(end * RATE):], settings["notice_language"])[0]
        if start > 0.6:
            return self.stt.transcribe(audio[: int(start * RATE)], settings["notice_language"])[0]
        return ""


STOP_WORDS = {"para", "calla", "callate", "basta", "stop", "espera", "vale", "ya"}


class BargeIn:
    """While Cortex speaks: is the user talking over it?

    There is no echo cancellation, so the mic also hears Cortex. Each voice chunk is transcribed and
    discarded if its words are the ones Cortex is saying. Only works while `enabled`.
    """

    def __init__(self, mic: Mic, stt: Transcriber, on_barge: Callable[[str, bool], None]):
        self.mic = mic
        self.stt = stt
        self.on_barge = on_barge
        self.enabled = False
        self._echo: collections.deque = collections.deque()   # (time, words Cortex has said)
        self._lock = threading.Lock()
        # the "Cortex" detector here too: it hears the word even if the transcription fails with its voice on top
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
        """\"Cortex\" spoken over it (unless it is Cortex itself saying its name)."""
        det = self.detector
        if not det.available or not det.process(block):
            return False
        if "cortex" in self._echo_words(within=8):
            log.info("Interruption? \"Cortex\" from the detector (%.2f) -> discarded", det.fire_score)
            return False
        log.info("Interruption? \"Cortex\" from the detector (%.2f) - YES", det.fire_score)
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
                self.on_barge("", False)   # it stops talking and listens
                buf, segment, voiced, silence = np.zeros(0, dtype=np.float32), [], 0, 0
                preroll.clear()
                continue
            buf = np.concatenate([buf, block])
            while len(buf) >= CHUNK:
                chunk, buf = buf[:CHUNK], buf[CHUNK:]
                try:
                    prob = vad(chunk)
                except Exception:
                    log.exception("Error in the interruption VAD")
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
                        log.exception("Error checking an interruption")

    def _judge(self, audio: np.ndarray):
        if len(audio) < RATE * 0.35:
            return
        text, lang = self.stt.transcribe(normalize(audio), "es")
        text = text.strip()
        words = [w for w in map(_norm, text.split()) if w]
        if not words or not self.enabled:
            return
        if lang != "es":   # its own voice mistranscribed usually comes out as nonsense English
            log.info("Interruption? %r -> discarded (%s)", text, lang)
            return
        echo = self._echo_words()
        novel = [w for w in words
                 if w not in echo and not any(SequenceMatcher(None, w, e).ratio() >= 0.75 for e in echo)]
        share = len(novel) / len(words)
        wake = find_wake(text, int(settings["wake_sensitivity"])) is not None and "cortex" not in echo
        stop = len(words) <= 2 and any(w in STOP_WORDS for w in novel)
        barge = wake or stop or (len(novel) >= 3 and share >= 0.6)
        log.info("Interruption? %r -> new %d/%d%s", text, len(novel), len(words), " - YES" if barge else "")
        if barge:
            self.enabled = False
            # if almost everything heard is the user's (and not just "para"), it is used as the command; otherwise listen again
            clean = share >= 0.8 and not stop and len(words) >= 2
            self.on_barge(text if clean else "", stop and not wake)
