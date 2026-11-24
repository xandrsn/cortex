"""Speech synthesis: Edge neural voices with local Piper as a fallback.

Edge audio arrives as MP3 in chunks; it is decoded and delivered as it comes so Cortex starts
speaking before the sentence is complete.

Opening the connection to Microsoft is almost all of the delay (~0.5 s of ~0.6 s per sentence), so it is kept open
between sentences and opened in advance as soon as the user starts talking (warm()): with it ready, the first
audio arrives in ~0.15 s. The server closes it after ~1 min idle; if it fails, a new one is tried.
"""

import asyncio
import logging
import re
import time
from typing import AsyncIterator

import aiohttp
import edge_tts
from edge_tts import communicate as _ec
from edge_tts.constants import SEC_MS_GEC_VERSION, WSS_HEADERS, WSS_URL
from edge_tts.data_classes import TTSConfig
from edge_tts.drm import DRM
import miniaudio
import numpy as np
from piper import PiperVoice
from piper.config import SynthesisConfig

from .config import MODELS_DIR, settings

log = logging.getLogger("tts")

EDGE_RATE = 24000
FIRST_BYTES = 4000       # the first block Edge sends (~4300 B, 0.7 s of audio): start with it
HOLD_BACK = 1152 * 2     # samples held back in case the next MP3 chunk alters them

HEDGE_S = 0.7            # no audio in this time: also request over another connection
TOTAL_S = 6              # no audio from either in this time: Piper
KEEP_S = 25              # after this unused, the connection is not reused (the server cuts it between 30 and 50 s)
CONFIG_MSG = ("Content-Type:application/json; charset=utf-8\r\nPath:speech.config\r\n\r\n"
              '{"context":{"synthesis":{"audio":{"metadataoptions":{"sentenceBoundaryEnabled":"false",'
              '"wordBoundaryEnabled":"false"},"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}\r\n')

_piper_cache: dict[str, PiperVoice] = {}


class _Conn:
    """An open WebSocket connection to the Edge speech service, reusable sentence after sentence."""

    def __init__(self, session: aiohttp.ClientSession, ws):
        self.session, self.ws, self.used = session, ws, time.monotonic()

    def fresh(self) -> bool:
        return not self.ws.closed and time.monotonic() - self.used < KEEP_S

    async def close(self):
        try:
            await self.ws.close()
        except Exception:
            pass
        await self.session.close()


_conn: _Conn | None = None
_warming: asyncio.Task | None = None
_lock: asyncio.Lock | None = None   # one sentence at a time per connection (and opening it, also inside)


def _get_lock() -> asyncio.Lock:
    global _lock
    _lock = _lock or asyncio.Lock()
    return _lock


async def _open() -> _Conn:
    session = aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=None, connect=5, sock_read=8))
    try:
        ws = await session.ws_connect(
            f"{WSS_URL}&ConnectionId={_ec.connect_id()}&Sec-MS-GEC={DRM.generate_sec_ms_gec()}"
            f"&Sec-MS-GEC-Version={SEC_MS_GEC_VERSION}",
            compress=15, headers=DRM.headers_with_muid(WSS_HEADERS), ssl=_ec._SSL_CTX)
        await ws.send_str(f"X-Timestamp:{_ec.date_to_string()}\r\n" + CONFIG_MSG)
        return _Conn(session, ws)
    except BaseException:
        await session.close()
        raise


async def _take() -> _Conn:
    """The ready connection or a new one (with the lock held)."""
    global _conn
    if _conn is None or not _conn.fresh():
        if _conn:
            await _conn.close()
        _conn = None
        _conn = await _open()
    return _conn


async def _warm():
    try:
        async with _get_lock():
            await _take()
    except Exception as e:
        log.info("Could not open the voice connection in advance: %s", e)


def warm() -> None:
    """Open the voice connection now (called when listening starts: it will be ready when it is time to speak)."""
    global _warming
    if settings["tts_engine"] != "edge" or (_conn and _conn.fresh()) or (_warming and not _warming.done()):
        return
    _warming = asyncio.get_running_loop().create_task(_warm())


async def _edge_ws(text: str, voice: str) -> AsyncIterator[bytes]:
    """MP3 chunks of the sentence over the reused connection. If abandoned midway, it is closed."""
    global _conn
    async with _get_lock():
        for attempt in (0, 1):
            conn = await _take()
            reused = time.monotonic() - conn.used > 0.05 or attempt
            done = got = False
            try:
                cfg = TTSConfig(voice, f"{int(settings['tts_rate']):+d}%", "+0%", "+0Hz", "SentenceBoundary")
                await conn.ws.send_str(_ec.ssml_headers_plus_data(_ec.connect_id(), _ec.date_to_string(),
                                                                  _ec.mkssml(cfg, _ec.escape(text))))
                async for m in conn.ws:
                    if m.type == aiohttp.WSMsgType.BINARY:
                        head = int.from_bytes(m.data[:2], "big")
                        if len(m.data) > head + 2:
                            got = True
                            yield m.data[head + 2:]
                    elif m.type == aiohttp.WSMsgType.TEXT:
                        if "Path:turn.end" in m.data:
                            done = True
                            break
                    else:
                        raise RuntimeError(f"Edge closed the connection ({m.type.name})")
                if not done:
                    raise RuntimeError("Edge cut the sentence short")
                conn.used = time.monotonic()
                return
            except Exception:
                await conn.close()
                _conn = None
                if got or attempt or not reused:   # with audio already sent or on a new connection: do not retry
                    raise
                log.info("The voice connection had expired; opening another")
            finally:
                if not done and _conn is conn:   # abandoned midway (interruption): audio would be left hanging
                    await conn.close()
                    _conn = None


MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
          "noviembre", "diciembre"]


def _date(m: re.Match) -> str:
    d, mo, y = int(m[1]), int(m[2]), m[3]
    if not (1 <= d <= 31 and 1 <= mo <= 12):
        return m[0]
    year = f" de {('20' + y) if y and len(y) == 2 else y}" if y else ""
    return f"{d} de {MONTHS[mo - 1]}{year}"


def _euros(m: re.Match) -> str:
    whole, cents = m[1].replace(".", ""), m[2]
    if whole == "0" and cents and int(cents):
        return f"{int(cents.ljust(2, '0'))} céntimos"
    out = f"{whole} {'euro' if whole == '1' else 'euros'}"
    if cents and int(cents):
        out += f" con {int(cents.ljust(2, '0'))} céntimos"
    return out


_UNITS = ("cero uno dos tres cuatro cinco seis siete ocho nueve diez once doce trece catorce quince dieciséis "
          "diecisiete dieciocho diecinueve veinte veintiuno veintidós veintitrés veinticuatro veinticinco "
          "veintiséis veintisiete veintiocho veintinueve").split()
_TENS = {3: "treinta", 4: "cuarenta", 5: "cincuenta", 6: "sesenta", 7: "setenta", 8: "ochenta", 9: "noventa"}
_HUNDREDS = {1: "ciento", 2: "doscientos", 3: "trescientos", 4: "cuatrocientos", 5: "quinientos", 6: "seiscientos",
             7: "setecientos", 8: "ochocientos", 9: "novecientos"}
_MASC_A = {"día", "días", "mapa", "mapas", "problema", "problemas", "programa", "programas", "tema", "temas", "sofá",
           "planeta", "idioma", "idiomas", "sistema", "clima"}


def number_words(n: int) -> str:
    """0-999,999,999 in Spanish words (masculine: "veintiuno", "doscientos")."""
    if n < 30:
        return _UNITS[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else " y " + _UNITS[n % 10])
    if n < 1000:
        return "cien" if n == 100 else _HUNDREDS[n // 100] + ("" if n % 100 == 0 else " " + number_words(n % 100))
    if n < 1_000_000:
        k, r = divmod(n, 1000)
        head = "mil" if k == 1 else _apocope(number_words(k)) + " mil"
        return head + ("" if r == 0 else " " + number_words(r))
    m, r = divmod(n, 1_000_000)
    head = "un millón" if m == 1 else _apocope(number_words(m)) + " millones"
    return head + ("" if r == 0 else " " + number_words(r))


def _apocope(words: str, feminine: bool = False) -> str:
    """Apocope of "uno" before a noun: "un aviso", "veintiún días", "una hora", "veintiuna tareas", "doscientas"."""
    if feminine and "millón" not in words and "millones" not in words:
        words = words.replace("ientos", "ientas")
    if words.endswith("veintiuno"):
        return words[:-len("veintiuno")] + ("veintiuna" if feminine else "veintiún")
    if words == "uno" or words.endswith(" uno"):
        return words[:-3] + ("una" if feminine else "un")
    return words


def _feminine(noun: str) -> bool:
    w = noun.lower()
    return w not in _MASC_A and bool(re.search(r"(as?|ión|iones|dad|dades|vez|veces|tarde|tardes|noche|noches)$", w))


def spell_numbers(text: str) -> str:
    """Standalone numbers as Spanish words: so multilingual voices do not switch to English on "99 céntimos".
    Times (18:30), decimals and anything attached to letters or odd symbols are left alone."""
    text = re.sub(r"(?<![\d.,:/])(\d{1,9})\s?%", lambda m: number_words(int(m[1])) + " por ciento", text)
    text = re.sub(r"(?<![\d.,:/])(\d{1,9})\s?°", lambda m: number_words(int(m[1])) + " grados", text)

    def one(m: re.Match) -> str:
        art, words, noun = m["art"] or "", number_words(int(m["n"])), m["noun"]
        fem_art = art.strip().lower() in ("la", "las", "una", "unas")   # «a la 1» → «a la una»
        if noun and noun.lower() not in _NOT_NOUN:
            return art + _apocope(words, fem_art or _feminine(noun)) + " " + noun
        return art + (_apocope(words, True) if fem_art else words) + (" " + noun if noun else "")
    return re.sub(r"(?<![\d.,:/\w])(?P<art>(?:las?|unas?)\s+)?(?P<n>\d{1,9})(?![\d:/\w°%])(?![.,:]\d)(?:\s+(?P<noun>[a-záéíóúñü]+))?",
                  one, text, flags=re.I)


_NOT_NOUN = {"y", "o", "u", "e", "de", "del", "a", "al", "en", "con", "por", "para", "que", "más", "menos", "es", "son",
             "está", "están", "hay", "se", "lo", "la", "las", "el", "los", "le", "les", "me", "te", "si", "sí", "no", "ni",
             "como", "pero", "entre", "sobre", "hasta", "desde", "contra", "sin", "tras", "ya", "veces"}


def clean_for_speech(text: str) -> str:
    # what voices read badly: "30/10" -> "30 diagonal 10"; "3,50 €" -> "centavos" (the multilingual ones)
    text = re.sub(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?(?![\d/])(?!\s+de\s)", _date, text)
    text = re.sub(r"(?<![\d.,])(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?\s?€", _euros, text)
    text = re.sub(r"(?<![\d.,])1 de (" + "|".join(MONTHS) + r")\b", r"uno de \1", text)   # not "el primero de"
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)   # [text](link) -> text
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_#`>|~]", "", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)
    return re.sub(r"\s+", " ", text).strip()


def _decode(mp3: bytes) -> np.ndarray:
    try:
        d = miniaudio.decode(mp3, output_format=miniaudio.SampleFormat.FLOAT32, nchannels=1, sample_rate=EDGE_RATE)
    except miniaudio.DecodeError:
        return np.zeros(0, dtype=np.float32)
    return np.asarray(d.samples, dtype=np.float32)


async def _edge_lib(text: str, voice: str) -> AsyncIterator[bytes]:
    """The library one (a connection per sentence): fallback if our own fails."""
    async for chunk in edge_tts.Communicate(text, voice, rate=f"{int(settings['tts_rate']):+d}%").stream():
        if chunk["type"] == "audio":
            yield chunk["data"]


async def _edge(text: str, voice: str, source=None) -> AsyncIterator[np.ndarray]:
    mp3, sent, next_at = b"", 0, FIRST_BYTES
    chunks = (source or _edge_ws)(text, voice)
    try:
        async for data in chunks:
            mp3 += data
            if len(mp3) >= next_at:
                pcm = _decode(mp3)
                end = max(sent, len(pcm) - HOLD_BACK)
                if end > sent:
                    yield pcm[sent:end]
                    sent = end
                next_at = len(mp3) + 6000
    finally:
        await chunks.aclose()   # in order, not whenever the garbage collector gets to it (it holds the connection)
    if not mp3:
        raise RuntimeError("Edge returned no audio")
    pcm = _decode(mp3)
    if len(pcm) > sent:
        yield pcm[sent:]


def preload_piper() -> None:
    _load_piper(settings["piper_es"])


def _load_piper(voice: str) -> PiperVoice:
    if voice not in _piper_cache:
        _piper_cache[voice] = PiperVoice.load(MODELS_DIR / "piper" / f"{voice}.onnx")
    return _piper_cache[voice]


def _piper(text: str, voice: str) -> tuple[np.ndarray, int]:
    model = _load_piper(voice)
    speed = 1 + int(settings["tts_rate"]) / 100
    chunks = list(model.synthesize(text, SynthesisConfig(length_scale=1 / max(speed, 0.5))))
    audio = np.concatenate([c.audio_float_array for c in chunks]) if chunks else np.zeros(0, np.float32)
    return audio.astype(np.float32), chunks[0].sample_rate if chunks else 22050


async def _race(text: str, voice: str):
    """The first audio chunk over the open connection; if it takes longer than HEDGE_S (the server sometimes
    thinks for 2 s) or fails, it is also requested over a new connection and whichever arrives first wins.
    Returns (generator, first chunk) or (None, None) if neither yields audio within TOTAL_S."""
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    tries: list[tuple] = []
    told: set = set()

    def start(source):
        g = _edge(text, voice, source)
        tries.append((g, asyncio.ensure_future(anext(g))))

    start(_edge_ws)
    winner = None
    try:
        while True:
            winner = next(((g, t) for g, t in tries if t.done() and not t.cancelled() and not t.exception()), None)
            if winner:
                return winner[0], winner[1].result()
            for g, t in tries:
                if t.done() and not t.cancelled() and t.exception() and t not in told:
                    told.add(t)
                    log.info("Edge TTS: %s", t.exception() or type(t.exception()).__name__)
            pending = [t for _, t in tries if not t.done()]
            hedged = len(tries) > 1
            if not hedged and (not pending or loop.time() - t0 >= HEDGE_S):
                start(_edge_lib)
                continue
            left = (t0 + HEDGE_S if not hedged else t0 + TOTAL_S) - loop.time()
            if not pending or (hedged and left <= 0):
                return None, None
            await asyncio.wait(pending, timeout=max(left, 0.01), return_when=asyncio.FIRST_COMPLETED)
    finally:
        for g, t in tries:
            if winner and g is winner[0]:
                continue
            t.cancel()
            try:
                await t
            except BaseException:
                pass
            await g.aclose()


async def synthesize(text: str, lang: str) -> AsyncIterator[tuple[np.ndarray, int]]:
    """Yields (audio, sample rate) of the sentence as it goes."""
    lang = "en" if lang == "en" else "es"
    if settings["tts_engine"] == "edge":
        voice = settings[f"voice_{lang}"]
        # multilingual voices sometimes read "99 céntimos" in English; spelled out they do not
        stream, first = await _race(spell_numbers(text) if lang == "es" and "Multilingual" in voice else text, voice)
        if stream:
            try:
                yield first, EDGE_RATE
                while True:
                    pcm = await asyncio.wait_for(anext(stream), timeout=6)
                    yield pcm, EDGE_RATE
            except StopAsyncIteration:
                return
            except Exception as e:
                log.warning("Edge TTS cut out mid-sentence: %s", e or type(e).__name__)
                return
            finally:
                await stream.aclose()
        log.warning("Edge TTS did not respond, falling back to Piper")
    piper = settings[f"piper_{lang}"]
    if not (MODELS_DIR / "piper" / f"{piper}.onnx").exists():   # offline fallback: only the Spanish model may be installed
        piper = settings["piper_es"]
    audio, rate = await asyncio.to_thread(_piper, text, piper)
    yield audio, rate
