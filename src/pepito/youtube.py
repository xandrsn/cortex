"""Vídeos de YouTube para el widget del panel: búsqueda leyendo la página de resultados (gratis, sin clave)."""

import json
import logging
import re
import time

import httpx

log = logging.getLogger("youtube")

URL = "https://www.youtube.com/results"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
DATA = re.compile(r"var ytInitialData = (\{.*?\});</script>", re.S)
_cache: dict[str, tuple[float, list]] = {}

# «pon en youtube X», «ponme un vídeo de X», «busca en youtube X»
PLAY_RE = re.compile(r"^(?:pon(?:me)?|busca(?:me)?|ens[eé][ñn]ame|quiero ver)\s+(?:en\s+youtube\s+|un\s+v[ií]deo\s+(?:de|del|sobre)\s+"
                     r"|el\s+v[ií]deo\s+(?:de|del)\s+)(.+?)(?:\s+en\s+youtube)?[.!?]*$", re.I)
CLOSE_RE = re.compile(r"\b(quita|cierra|apaga)\b.{0,10}\bv[ií]deo\b", re.I)
PAUSE_RE = re.compile(r"\b(pausa|para)\b.{0,10}\bv[ií]deo\b", re.I)
RESUME_RE = re.compile(r"\b(sigue|contin[uú]a|reanuda|dale al)\b.{0,10}\bv[ií]deo\b", re.I)
SMALL_RE = re.compile(r"\b(minimiza|quita la pantalla completa|ponlo en peque[ñn]o|ens[eé][ñn]ame el panel)\b", re.I)
SEEK_RE = re.compile(r"\b(adelanta|atrasa|retrocede|rebobina)\b(?:.{0,10}?\b(\d+|un|una|medio)\s*(segundos?|minutos?))?", re.I)
BIG_RE = re.compile(r"\bv[ií]deo\b.{0,20}\b(en grande|pantalla completa)\b|\b(pon|ponlo|ponme)\b.{0,5}\b(en grande|"
                    r"pantalla completa)\b", re.I)


def _walk(o):
    if isinstance(o, dict):
        if "videoRenderer" in o:
            yield o["videoRenderer"]
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _text(node: dict | None) -> str:
    if not node:
        return ""
    return node.get("simpleText") or "".join(r.get("text", "") for r in node.get("runs", []))


def search(query: str, limit: int = 12) -> list[dict]:
    """[{id, title, channel, length, thumb}]. Lanza RuntimeError con un mensaje en español si no se puede."""
    query = query.strip()
    if not query:
        return []
    hit = _cache.get(query.lower())
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    try:
        r = httpx.get(URL, params={"search_query": query, "hl": "es", "gl": "ES"}, timeout=10, follow_redirects=True,
                      headers={"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"})
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("YouTube: %s", e)
        raise RuntimeError("No pude buscar en YouTube: sin conexión o no responde") from e
    m = DATA.search(r.text)
    if not m:
        raise RuntimeError("YouTube ha cambiado su página y no entiendo los resultados")
    out = []
    for v in _walk(json.loads(m.group(1))):
        if not v.get("videoId") or not v.get("lengthText"):   # sin duración: directos y cosas raras
            continue
        out.append({"id": v["videoId"], "title": _text(v.get("title")), "channel": _text(v.get("ownerText")),
                    "length": _text(v.get("lengthText")),
                    "thumb": f"https://i.ytimg.com/vi/{v['videoId']}/mqdefault.jpg"})
        if len(out) >= limit:
            break
    _cache[query.lower()] = (time.time(), out)
    if len(_cache) > 50:
        _cache.pop(next(iter(_cache)))
    return out
