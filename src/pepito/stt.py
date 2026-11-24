"""Transcripción local, limitada a español e inglés.

- parakeet (predeterminado): NVIDIA Parakeet TDT 0.6b v3 en ONNX int8. Procesa solo el audio que hay
  (Whisper siempre codifica 30 s), así que en CPU es ~10 veces más rápido. No dice el idioma; se
  deduce del texto.
- whisper-small / whisper-base: alternativa. Una sola pasada del codificador para detectar idioma y
  transcribir.
"""

import logging
import re
import threading
import time
import wave

import numpy as np

from .config import LOGS_DIR, MODELS_DIR

log = logging.getLogger("stt")

MODELS = ["parakeet", "whisper-small", "whisper-base"]
LANGS = ("es",)   # solo español: el multidioma no era fiable
WHISPER_VOCAB = " Cortex, Betis, Spotify, playlist, gym."

# Lo que más suele oír mal, corregido a mano
FIXES = [
    (re.compile(r"\b(bettis|vetis|betty's|bettys|betis)\b", re.I), "Betis"),
    (re.compile(r"\b(spotyfi|spotifai)\b", re.I), "Spotify"),
]

ES_WORDS = {"el", "la", "los", "las", "que", "qué", "de", "y", "es", "en", "un", "una", "me", "mi", "por", "para",
            "pon", "dime", "cómo", "como", "cuándo", "hoy", "mañana", "tengo", "hay", "no", "sí", "lo", "le", "se",
            "con", "del", "al", "muy", "pero", "más", "este", "esta", "hora", "oye", "vale", "quita", "sube", "baja"}
EN_WORDS = {"the", "what", "is", "are", "you", "how", "my", "please", "can", "do", "i", "it's", "whats", "what's",
            "play", "tell", "me", "a", "an", "of", "to", "in", "on", "and", "today", "tomorrow", "time", "when",
            "hey", "turn", "up", "down", "next", "stop", "this", "that", "have", "it", "for", "with", "be", "will"}


def guess_lang(text: str, default: str = "es") -> str:
    return "es"


def _guess_lang_multi(text: str, default: str = "es") -> str:
    t = text.lower()
    if re.search(r"[ñ¿¡áéíóú]", t):
        return "es"
    words = re.findall(r"[a-z']+", t)
    es = sum(w in ES_WORDS for w in words)
    en = sum(w in EN_WORDS for w in words)
    if en > es:
        return "en"
    if es > en:
        return "es"
    return default


def fix_text(text: str) -> str:
    for pattern, repl in FIXES:
        text = pattern.sub(repl, text)
    return text.strip()


CLIPS_DIR = LOGS_DIR / "clips"
KEEP_CLIPS = 20


def normalize(audio: np.ndarray, peak: float = 0.9) -> np.ndarray:
    """Sube el volumen hasta un pico fijo (el micro del portátil llega flojo y desde lejos)."""
    top = float(np.abs(audio).max()) if len(audio) else 0.0
    if top < 1e-4:
        return audio
    return (audio * min(peak / top, 30.0)).astype(np.float32)


HALLUCINATIONS = re.compile(
    r"^(¡?suscr[ií]bete!?|thank you\.?|thanks for watching!?|gracias por ver( el video)?\.?|subt[ií]tulos.*|"
    r"amara\.org.*|you\.?|bye\.?|\.+|música|\[.*\]|\(.*\))$", re.I)


def suspicious(text: str, seconds: float) -> bool:
    """¿Parece que Parakeet no ha entendido? Vacío, o una muletilla suelta para un audio largo."""
    words = re.findall(r"\w+", text)
    if not words:
        return True
    fillers = {"yeah", "yes", "no", "okay", "ok", "um", "uh", "hmm", "mm", "oh", "ah", "eh", "si", "sí", "the", "i"}
    return seconds > 1.2 and len(words) <= 2 and all(w.lower() in fillers for w in words)


def save_clip(audio: np.ndarray, text: str) -> None:
    """Guarda en local las últimas órdenes dichas a Cortex, para poder revisar qué entiende mal."""
    try:
        CLIPS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        with wave.open(str(CLIPS_DIR / f"{stamp}.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
        (CLIPS_DIR / f"{stamp}.txt").write_text(text, encoding="utf-8")
        for old in sorted(CLIPS_DIR.glob("*.wav"))[:-KEEP_CLIPS]:
            old.unlink(missing_ok=True)
            old.with_suffix(".txt").unlink(missing_ok=True)
    except OSError as e:
        log.warning("No pude guardar el audio: %s", e)


class Transcriber:
    def __init__(self):
        self._model = None
        self._name = None
        self._lock = threading.Lock()
        self._backup = None   # Whisper de respaldo para cuando Parakeet no entiende

    def load(self, name: str) -> None:
        if name not in MODELS:
            name = "parakeet"
        with self._lock:
            if name == self._name:
                return
            log.info("Cargando %s", name)
            if name == "parakeet":
                import onnx_asr
                local = MODELS_DIR / "parakeet"   # sin copia local se descarga a la caché de Hugging Face (y se reanuda)
                self._model = onnx_asr.load_model("nemo-parakeet-tdt-0.6b-v3", local if local.exists() else None,
                                                  quantization="int8")
            else:
                from faster_whisper import WhisperModel
                kwargs = dict(device="cpu", compute_type="int8", cpu_threads=4,
                              download_root=str(MODELS_DIR / "whisper"))
                size = name.split("-", 1)[1]
                try:
                    # sin red si ya está descargado: arranca aunque no haya internet
                    self._model = WhisperModel(size, local_files_only=True, **kwargs)
                except Exception:
                    self._model = WhisperModel(size, **kwargs)
            self._name = name

    def transcribe(self, audio: np.ndarray, default_lang: str = "es") -> tuple[str, str]:
        """Devuelve (texto, idioma)."""
        with self._lock:
            if self._name == "parakeet":
                text = fix_text(self._model.recognize(audio.astype(np.float32), sample_rate=16000))
                return text, guess_lang(text, default_lang)
            text, lang = self._whisper(audio)
            return fix_text(text), lang

    def transcribe_command(self, audio: np.ndarray, default_lang: str = "es", save: bool = True) -> tuple[str, str]:
        """Para órdenes: normaliza y transcribe. (Sin Whisper de respaldo: tardaba 5-20 s y se inventaba frases.)"""
        audio = normalize(audio)
        text, lang = self.transcribe(audio, default_lang)
        if not text.strip() and len(audio) > 0.6 * 16000:
            # Parakeet a veces devuelve nada si la voz empieza o acaba de golpe (p. ej. con música de fondo):
            # con medio segundo de silencio alrededor sí lo entiende
            pad = np.zeros(8000, dtype=np.float32)
            text, lang = self.transcribe(np.concatenate([pad, audio, pad]), default_lang)
        if save:
            save_clip(audio, f"{lang}: {text}")
        return text, lang

    def preload_backup(self) -> None:
        self._whisper_backup(np.zeros(16000, dtype=np.float32))

    def _whisper_backup(self, audio: np.ndarray) -> tuple[str, str]:
        from faster_whisper import WhisperModel
        if self._backup is None:
            kwargs = dict(device="cpu", compute_type="int8", cpu_threads=4, download_root=str(MODELS_DIR / "whisper"))
            try:
                self._backup = WhisperModel("small", local_files_only=True, **kwargs)
            except Exception:
                self._backup = WhisperModel("small", **kwargs)
        return self._whisper(audio, self._backup)

    def _whisper(self, audio: np.ndarray, model=None) -> tuple[str, str]:
        from faster_whisper.tokenizer import Tokenizer
        m = model or self._model
        frames = m.feature_extractor.nb_max_frames
        feats = m.feature_extractor(audio[: 30 * 16000])
        if feats.shape[1] < frames:
            feats = np.pad(feats, ((0, 0), (0, frames - feats.shape[1])))
        encoded = m.encode(feats[:, :frames])
        scores = {tok[2:-2]: p for tok, p in m.model.detect_language(encoded)[0]}
        lang = max(LANGS, key=lambda l: scores.get(l, 0.0))
        tok = Tokenizer(m.hf_tokenizer, True, task="transcribe", language=lang)
        prompt = [tok.sot_prev, *tok.encode(WHISPER_VOCAB), *tok.sot_sequence, tok.no_timestamps]
        out = m.model.generate(encoded, [prompt], beam_size=1, max_length=224,
                               suppress_blank=True, suppress_tokens=[-1])
        return tok.decode(out[0].sequences_ids[0]).strip(), lang
