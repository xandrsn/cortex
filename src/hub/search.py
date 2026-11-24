"""Buscador del panel: resultados de DuckDuckGo (gratis, sin clave). Caché de 10 min por búsqueda."""

import html
import logging
import re
import time
from urllib.parse import parse_qs, unquote, urlparse

import httpx

log = logging.getLogger("search")

URL = "https://html.duckduckgo.com/html/"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
RESULT = re.compile(r'<a rel="nofollow" class="result__a" href="([^"]+)">(.*?)</a>.*?'
                    r'<a class="result__snippet"[^>]*>(.*?)</a>', re.S)
_cache: dict[str, tuple[float, list]] = {}


def _text(fragment: str) -> str:
    return html.unescape(re.sub(r"<.*?>", "", fragment)).strip()


def _real_url(href: str) -> str:
    """DuckDuckGo a veces envuelve el enlace en una redirección (/l/?uddg=…)."""
    if href.startswith("//"):
        href = "https:" + href
    q = parse_qs(urlparse(href).query)
    return unquote(q["uddg"][0]) if "uddg" in q else href


def search(query: str, limit: int = 8) -> list[dict]:
    """[{title, url, site, snippet}]. Lanza RuntimeError con un mensaje en español si no se puede."""
    query = query.strip()
    if not query:
        return []
    hit = _cache.get(query.lower())
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        r = httpx.post(URL, data={"q": query, "kl": "es-es"}, headers={"User-Agent": UA}, timeout=10)
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("Buscador: %s", e)
        raise RuntimeError("No pude buscar: sin conexión o el buscador no responde") from e
    results = []
    for href, title, snippet in RESULT.findall(r.text):
        url = _real_url(html.unescape(href))
        if "duckduckgo.com/y.js" in url:   # anuncios
            continue
        results.append({"title": _text(title), "url": url, "site": urlparse(url).netloc.removeprefix("www."),
                        "snippet": _text(snippet)})
        if len(results) >= limit:
            break
    if not results and "anomaly" in r.text.lower():
        raise RuntimeError("El buscador ha bloqueado la búsqueda un rato; pregúntaselo a Cortex")
    _cache[query.lower()] = (time.time(), results)
    if len(_cache) > 50:
        _cache.pop(next(iter(_cache)))
    return results
