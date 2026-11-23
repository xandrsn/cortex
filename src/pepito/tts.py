"""Síntesis de voz: voces neuronales de Edge y Piper local como respaldo.

El audio de Edge llega en MP3 por trozos; se va decodificando y entregando para que Pepito empiece a
hablar antes de que la frase esté entera.

Abrir la conexión con Microsoft es casi todo el retraso (~0,5 s de ~0,6 s por frase), así que se deja abierta
entre frases y se abre de antemano en cuanto Charlie empieza a hablar (warm()): con ella lista, el primer
audio llega en ~0,15 s. El servidor la cierra tras ~1 min sin uso; si falla, se reintenta con una nueva.
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
FIRST_BYTES = 4000       # el primer bloque que manda Edge (~4300 B, 0,7 s de audio): se empieza con él
HOLD_BACK = 1152 * 2     # muestras que se guardan por si el siguiente trozo de MP3 las retoca

HEDGE_S = 0.7            # sin audio en este tiempo, se pide también por otra conexión
TOTAL_S = 6              # sin audio de ninguna en este tiempo: Piper
KEEP_S = 25              # tras esto sin usarla, la conexión no se reutiliza (el servidor corta entre 30 y 50 s)
CONFIG_MSG = ("Content-Type:application/json; charset=utf-8\r\nPath:speech.config\r\n\r\n"
              '{"context":{"synthesis":{"audio":{"metadataoptions":{"sentenceBoundaryEnabled":"false",'
              '"wordBoundaryEnabled":"false"},"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}\r\n')

_piper_cache: dict[str, PiperVoice] = {}


class _Conn:
    """Una conexión WebSocket abierta con el servicio de voz de Edge, reutilizable frase a frase."""

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
_lock: asyncio.Lock | None = None   # una frase a la vez por la conexión (y abrirla, también dentro)


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
    """La conexión lista o una nueva (con el candado cogido)."""
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
        log.info("No pude abrir de antemano la voz: %s", e)


def warm() -> None:
    """Abre ya la conexión de la voz (se llama al empezar a escuchar: estará lista cuando haya que hablar)."""
    global _warming
    if settings["tts_engine"] != "edge" or (_conn and _conn.fresh()) or (_warming and not _warming.done()):
        return
    _warming = asyncio.get_running_loop().create_task(_warm())


async def _edge_ws(text: str, voice: str) -> AsyncIterator[bytes]:
    """Trozos de MP3 de la frase, por la conexión que se reutiliza. Si se abandona a medias, se cierra."""
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
                        raise RuntimeError(f"Edge cerró la conexión ({m.type.name})")
                if not done:
                    raise RuntimeError("Edge cortó la frase")
                conn.used = time.monotonic()
                return
            except Exception:
                await conn.close()
                _conn = None
                if got or attempt or not reused:   # con audio ya enviado o en una conexión nueva: no se repite
                    raise
                log.info("La conexión de la voz había caducado; abro otra")
            finally:
                if not done and _conn is conn:   # abandonada a medias (interrupción): quedaría audio colgando
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
    """0-999.999.999 en palabras (masculino: «veintiuno», «doscientos»)."""
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
    """«uno» delante de un nombre: «un aviso», «veintiún días», «una hora», «veintiuna tareas», «doscientas»."""
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
    """Los números sueltos, en palabras: así las voces multilingües no se pasan al inglés con «99 céntimos».
    Se dejan las horas (18:30), los decimales y lo que vaya pegado a letras o símbolos raros."""
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
    # lo que las voces leen mal: «30/10» → «30 diagonal 10»; «3,50 €» → «centavos» (las multilingües)
    text = re.sub(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?(?![\d/])(?!\s+de\s)", _date, text)
    text = re.sub(r"(?<![\d.,])(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?\s?€", _euros, text)
    text = re.sub(r"(?<![\d.,])1 de (" + "|".join(MONTHS) + r")\b", r"uno de \1", text)   # no «el primero de»
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)   # [texto](enlace) → texto
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
    """La de la librería (una conexión por frase): respaldo si la propia falla."""
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
        await chunks.aclose()   # en orden, no cuando lo recoja el recolector (tiene la conexión cogida)
    if not mp3:
        raise RuntimeError("Edge no devolvió audio")
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
    """El primer trozo de audio por la conexión abierta; si tarda más de HEDGE_S (el servidor a veces se
    queda pensando 2 s) o falla, se pide a la vez por una conexión nueva y gana la que llegue antes.
    Devuelve (generador, primer trozo) o (None, None) si ninguna da audio en TOTAL_S."""
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
    """Va entregando (audio, frecuencia) de la frase."""
    lang = "es"   # solo español
    if settings["tts_engine"] == "edge":
        voice = settings[f"voice_{lang}"]
        # las multilingües a veces leen «99 céntimos» en inglés; con letra no
        stream, first = await _race(spell_numbers(text) if "Multilingual" in voice else text, voice)
        if stream:
            try:
                yield first, EDGE_RATE
                while True:
                    pcm = await asyncio.wait_for(anext(stream), timeout=6)
                    yield pcm, EDGE_RATE
            except StopAsyncIteration:
                return
            except Exception as e:
                log.warning("Edge TTS se cortó a mitad de frase: %s", e or type(e).__name__)
                return
            finally:
                await stream.aclose()
        log.warning("Edge TTS no respondió, tiro de Piper")
    audio, rate = await asyncio.to_thread(_piper, text, settings[f"piper_{lang}"])
    yield audio, rate
