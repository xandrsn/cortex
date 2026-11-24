"""Per-service video recipes: from "latest news broadcast" to the URL that plays, and how to press play.

find(service, query) uses each site's own public API (no keys): RTVE (api.rtve.es), Pluto TV
(boot + channels + search), Atresplayer (api.atresplayer.com) and YouTube (youtube.py's search).
Netflix, Prime Video and Disney+ need a login: only their search page is opened.
steps(url) says what to press on each site to start playback (cookie notices: always "reject").
"""

import logging
import re
import time
import unicodedata
import uuid
from datetime import date, datetime, timedelta
from urllib.parse import quote_plus

import httpx

log = logging.getLogger("video_sites")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0 Safari/537.36 Edg/130.0")
H = {"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"}

# words that do not help the search (Spanish filler words in voice requests; kept)
STOP = set("el la los las lo un una unos unas de del en y a al con por para que mi me pon ponme ver quiero echa "
           "reproduce ultimo ultima ultimos ultimas reciente nuevo nueva capitulo episodio programa completo entero "
           "hoy ayer anoche esta noche manana tarde rtve play pluto tv atresplayer canal directo vivo".split())


def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def words(query: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", norm(query)) if w not in STOP]


def wants_live(query: str) -> bool:
    return bool(re.search(r"\b(directo|en vivo|canal)\b", norm(query)))


def wanted_day(query: str) -> date | None:
    q = norm(query)
    if re.search(r"\b(anoche|ayer)\b", q):
        return date.today() - timedelta(days=1)
    if re.search(r"\b(hoy|esta (manana|tarde|noche))\b", q):
        return date.today()
    return None


def _pick_day(items: list[dict], day: date | None) -> list[dict]:
    """If "anoche"/"hoy" (last night/today) was asked for, put that day's items first (if any)."""
    if not day:
        return items
    hit = [i for i in items if i.get("date") == day.isoformat()]
    return hit + [i for i in items if i not in hit]


def _get(url: str, **kw):
    r = httpx.get(url, headers={**H, **kw.pop("headers", {})}, timeout=10, follow_redirects=True, **kw)
    r.raise_for_status()
    return r.json()


# ---------- RTVE Play ----------
RTVE_LIVE = {"la 1": "la-1", "la1": "la-1", "la 2": "la-2", "la2": "la-2", "24 horas": "24h", "24h": "24h",
             "canal 24": "24h", "teledeporte": "tdp", "clan": "clan"}
RTVE_NEWS = [45030, 135930, 135932, 135931]   # Telediario 1, 2, weekend and morning edition
RTVE_COMPLETO = 39816                           # video type "Completo" (full episodes, not clips)


def _rtve_date(s: str | None) -> str | None:
    try:
        return datetime.strptime(s or "", "%d-%m-%Y %H:%M:%S").date().isoformat()
    except ValueError:
        return None


def _rtve_latest(pid: int, n: int = 3) -> list[dict]:
    items = _get(f"https://api.rtve.es/api/programas/{pid}/videos",
                 params={"type": RTVE_COMPLETO, "size": n})["page"]["items"]
    return [{"title": v.get("title", ""), "url": v["htmlUrl"], "kind": "capítulo", "date": _rtve_date(v.get("publicationDate")),
             "sort": v.get("publicationDate") and datetime.strptime(v["publicationDate"], "%d-%m-%Y %H:%M:%S").timestamp()}
            for v in items if v.get("htmlUrl") and (v.get("duration") or 0) > 600_000]   # drop the 4-minute summaries


RTVE_HOURS = {"3": "15 horas", "15": "15 horas", "tres": "15 horas", "mediodia": "15 horas", "1": "15 horas",
              "9": "21 horas", "21": "21 horas", "nueve": "21 horas", "noche": "21 horas", "2": "21 horas",
              "matinal": "matinal", "7": "matinal", "8": "matinal"}


def _rtve_news(ws: list[str], query: str) -> list[dict]:
    """News broadcasts: the most recent of all editions (or the one asked for: \"de las 3\", \"de la noche\"...)."""
    out = []
    for pid in RTVE_NEWS:
        try:
            out += _rtve_latest(pid, 4)
        except (httpx.HTTPError, KeyError, ValueError) as e:
            log.info("RTVE program %s: %s", pid, e)
    hint = next((RTVE_HOURS[w] for w in ws if w in RTVE_HOURS), None)
    if hint == "matinal":
        out = [v for v in out if "telediario-matinal" in v["url"]] or out
    elif hint:
        out = [v for v in out if hint in v["title"]] or out
    out.sort(key=lambda v: -(v.get("sort") or 0))
    for v in out:
        v.pop("sort", None)
    return _pick_day(out, wanted_day(query))


def rtve(query: str) -> list[dict]:
    q, ws = norm(query), words(query)
    for name, slug in RTVE_LIVE.items():
        if re.search(rf"\b{name}\b", q) and (wants_live(query) or " ".join(ws) in (name, name.replace(" ", ""))):
            return [{"title": f"{name.upper()} live", "url": f"https://www.rtve.es/play/videos/directo/canales-lineales/{slug}/",
                     "kind": "directo", "date": None}]
    if not ws:
        return []
    news = {"telediario", "telediarios", "td", "noticias", "informativo"}
    if news & set(ws) and set(ws) - news <= set(RTVE_HOURS) | {"1", "2", "matinal", "mediodia", "noche"}:
        return _rtve_news(ws, query)
    else:
        progs = [p for p in _get("https://api.rtve.es/api/search/programs", params={"search": " ".join(ws), "size": 10})
                 ["page"]["items"] if p.get("videosRef")]
        exact = [p for p in progs if " ".join(words(p.get("title", ""))) == " ".join(ws)]
        close = [p for p in progs if all(w in norm(p.get("title", "")) for w in ws)]
        pids = [p["id"] for p in (exact or close or progs[:2])][:4]
    out = []
    for rank, pid in enumerate(pids):
        try:
            out += [{**v, "rank": rank} for v in _rtve_latest(pid)]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            log.info("RTVE programa %s: %s", pid, e)
    # most recent first; on the same day, the best-matching program
    out.sort(key=lambda v: (-(int((v.get("sort") or 0) // 86400)), v["rank"], -(v.get("sort") or 0)))
    if not out:   # no program: individual videos (full ones first)
        items = _get("https://api.rtve.es/api/search/contents", params={"search": " ".join(ws), "size": 10})["page"]["items"]
        items.sort(key=lambda v: (v.get("type") or {}).get("name") != "Completo")
        out = [{"title": v.get("title", ""), "url": v["htmlUrl"], "kind": "vídeo", "date": _rtve_date(v.get("publicationDate"))}
               for v in items if v.get("assetType") == "video" and v.get("htmlUrl")]
    for v in out:
        v.pop("rank", None), v.pop("sort", None)
    return _pick_day(out, wanted_day(query))


# ---------- Pluto TV ----------
_pluto: dict = {}
PLUTO_SYN = {"pelis": "cine", "peli": "cine", "peliculas": "cine", "pelicula": "cine", "cine": "cine",
             "dibujos": "infantil", "ninos": "infantil", "crios": "infantil", "miedo": "terror", "risa": "comedia",
             "policiacas": "policiac", "policiaca": "policiac", "policia": "policiac", "telenovelas": "novela",
             "documentales": "document", "documental": "document", "series": "serie"}


def _pluto_session() -> dict:
    if _pluto.get("exp", 0) < time.time():
        b = _get("https://boot.pluto.tv/v4/start", params={
            "appName": "web", "appVersion": "9", "deviceVersion": "1", "deviceModel": "web", "deviceMake": "edge",
            "deviceType": "web", "clientID": str(uuid.uuid4()), "clientModelNumber": "1"})
        _pluto.clear()
        _pluto.update(token=b["sessionToken"], exp=time.time() + 1800)
    return {"Authorization": "Bearer " + _pluto["token"]}


def _pluto_channels() -> list[dict]:
    if "channels" not in _pluto:
        auth = _pluto_session()
        chans = _get("https://service-channels.clusters.pluto.tv/v2/guide/channels",
                     params={"channelIds": "", "offset": 0, "limit": 1000, "sort": "number:asc"}, headers=auth)["data"]
        cats = _get("https://service-channels.clusters.pluto.tv/v2/guide/categories", headers=auth)["data"]
        cat_of: dict[str, str] = {}
        for c in cats:
            for cid in c.get("channelIDs", []):
                cat_of[cid] = cat_of.get(cid, "") + " " + norm(c["name"])
        _pluto["channels"] = [{"name": c["name"], "slug": c["slug"], "n": norm(c["name"]), "cat": cat_of.get(c["id"], ""),
                               "sum": norm(c.get("summary") or "")} for c in chans]
    return _pluto["channels"]


def pluto(query: str) -> list[dict]:
    ws = [PLUTO_SYN.get(w, w) for w in words(query)]
    if not ws:
        return []
    chans = []
    for c in _pluto_channels():
        score = sum(3 if w in c["n"] else 2 if w in c["cat"] else 1 if w in c["sum"] else 0 for w in ws)
        if score and all(w in c["n"] + c["cat"] + c["sum"] for w in ws):
            chans.append((score, c))
    chans.sort(key=lambda x: -x[0])
    live = [{"title": c["name"], "url": f"https://pluto.tv/es/live-tv/{c['slug']}", "kind": "canal", "date": None}
            for _, c in chans[:5]]
    if wants_live(query) or (chans and chans[0][0] >= 3 * len(ws)):
        return live
    vod = []
    try:
        auth = _pluto_session()
        for it in _get("https://service-media-search.clusters.pluto.tv/v1/search",
                       params={"q": " ".join(words(query)), "limit": 10}, headers=auth).get("data", []):
            if it["type"] == "movie":
                vod.append({"title": it["name"], "url": f"https://pluto.tv/es/on-demand/movies/{it['slug']}",
                            "kind": "película", "date": None})
            elif it["type"] == "series":
                s = _get(f"https://service-vod.clusters.pluto.tv/v4/vod/series/{it['id']}/seasons",
                         params={"offset": 1000, "page": 1}, headers=auth)
                eps = [e for season in s.get("seasons", []) for e in season.get("episodes", [])]
                if eps:   # the series from the first episode
                    e = eps[0]
                    vod.append({"title": it["name"], "kind": "serie", "date": None,
                                "url": f"https://pluto.tv/es/on-demand/series/{it['slug']}/season/{e.get('season', 1)}"
                                       f"/episode/{e['slug']}"})
            elif it["type"] == "channel":
                vod.append({"title": it["name"], "url": f"https://pluto.tv/es/live-tv/{it['slug']}", "kind": "canal",
                            "date": None})
            if len(vod) >= 5:
                break
    except (httpx.HTTPError, KeyError, ValueError) as e:
        log.info("Pluto search: %s", e)
    # the closest match to the name first
    vod.sort(key=lambda v: not all(w in norm(v["title"]) for w in words(query)))
    return vod + live


# ---------- Atresplayer ----------
ATRES_LIVE = {"antena 3": "antena3", "antena3": "antena3", "la sexta": "lasexta", "lasexta": "lasexta", "neox": "neox",
              "nova": "nova", "mega": "mega", "atreseries": "atreseries"}
ATRES_NEWS = {"antena3": ["5a7858647ed1a8991dc362df", "5a7858667ed1a8991dc362e5"],    # Antena 3 Noticias 1 y 2
              "lasexta": ["5a786ce27ed1a8991cb24494", "5a786ce47ed1a8991cb2449a"]}    # laSexta Noticias 14 y 20 h
ATRES_API = "https://api.atresplayer.com/client/v1/row/search"


def _atres_item(it: dict) -> dict:
    m = re.search(r"\((\d\d)-(\d\d)-(\d\d)\)", (it.get("image") or {}).get("title") or "")
    day = f"20{m[3]}-{m[2]}-{m[1]}" if m else None
    title = (it.get("subTitle") or "").strip()
    return {"title": f"{title}: {it['title']}" if title else it["title"], "kind": "capítulo", "date": day,
            "url": "https://www.atresplayer.com" + it["link"]["url"],
            "free": it.get("visibility", "FREE") == "FREE"}


def atresplayer(query: str) -> list[dict]:
    q, ws = norm(query), words(query)
    for name, slug in ATRES_LIVE.items():
        if re.search(rf"\b{name}\b", q) and (wants_live(query) or " ".join(ws) in (name, name.replace(" ", ""))):
            return [{"title": f"{name.title()} live", "url": f"https://www.atresplayer.com/directos/{slug}/",
                     "kind": "directo", "date": None}]
    if not ws:
        return []
    if "noticias" in ws:   # news: the channel's two editions, latest first
        chan = "lasexta" if re.search(r"sexta", q) else "antena3"
        out = []
        for i, fid in enumerate(ATRES_NEWS[chan]):
            out += [{**_atres_item(it), "ed": i} for it in _get(ATRES_API, params={
                "entityType": "ATPEpisode", "formatId": fid, "sortType": "THE_LAST", "size": 3}).get("itemRows", [])]
        out.sort(key=lambda v: (v["date"] or "", v.pop("ed")), reverse=True)
        for v in out:
            v.pop("free")
        return _pick_day(out, wanted_day(query))
    text = " ".join(ws)
    out = []
    fmts = _get(ATRES_API, params={"entityType": "ATPFormat", "text": text, "size": 3}).get("itemRows", [])
    if fmts:
        fid = fmts[0].get("formatId") or fmts[0].get("contentId")
        out = [_atres_item(it) for it in _get(ATRES_API, params={"entityType": "ATPEpisode", "formatId": fid,
                                                                  "sortType": "THE_LAST", "size": 8}).get("itemRows", [])]
    if not out:
        out = [_atres_item(it) for it in _get(ATRES_API, params={"entityType": "ATPEpisode", "text": text, "size": 8})
               .get("itemRows", [])]
    out.sort(key=lambda v: not v.pop("free"))   # paid (Premium) items last
    return _pick_day(out, wanted_day(query))


# ---------- YouTube ----------
def youtube_find(query: str) -> list[dict]:
    from . import youtube
    return [{"title": v["title"], "url": f"https://www.youtube.com/watch?v={v['id']}", "kind": "vídeo", "date": None,
             "id": v["id"]} for v in youtube.search(query, limit=5)]


# ---------- with login: search only ----------
SEARCH_URL = {
    "netflix": "https://www.netflix.com/search?q={}",                       # title: /title/ID, watch: /watch/ID
    "prime": "https://www.primevideo.com/search/ref=atv_nb_sug?phrase={}",  # title: /detail/ID
    "disney": "https://www.disneyplus.com/browse/search?q={}",              # title: /browse/entity-ID, watch: /play/ID
}

FINDERS = {"rtve": rtve, "pluto": pluto, "atresplayer": atresplayer, "youtube": youtube_find}
SERVICE_NAMES = [("rtve", r"rtve|la ?1\b|la ?2\b|24 ?h|clan|teledeporte"), ("pluto", r"pluto"),
                 ("atresplayer", r"atres|antena ?3|la ?sexta|neox|nova\b|mega\b"), ("youtube", r"youtu"),
                 ("netflix", r"netflix"), ("prime", r"prime|amazon"), ("disney", r"disney")]


def service_key(name: str) -> str | None:
    n = norm(name)
    return next((k for k, rx in SERVICE_NAMES if re.search(rx, n)), None)


VIDEO_PATH = re.compile(r"/(video|watch|ver|v|pelicula|movie|serie|episodio|episode|capitulo)"
                        r"(/|$|-)|/\d{5,}|[?&]v=", re.I)


# the search engines, opened in the videos window (a real browser: requests from Python return
# junk or get blocked); {q} = "site:domain query"
ENGINES = ["https://www.bing.com/search?q={q}&setlang=es", "https://html.duckduckgo.com/html/?q={q}"]
ENGINE_LINKS_JS = r"""[...document.querySelectorAll('a.result__a, li.b_algo h2 a')].map(a => a.href)"""


def engine_url(href: str) -> str:
    """The real address behind a result link (DuckDuckGo: uddg=...; Bing: u=a1<base64>)."""
    import base64
    from urllib.parse import unquote
    m = re.search(r"[?&]uddg=([^&]+)", href)
    if m:
        return unquote(m.group(1))
    m = re.search(r"bing\.com/ck/.*?[?&]u=a1([^&]+)", href)
    if m:
        try:
            return base64.urlsafe_b64decode(m.group(1) + "=" * (-len(m.group(1)) % 4)).decode()
        except (ValueError, UnicodeDecodeError):
            return ""
    return href


def pick_engine_result(hrefs: list[str], domain: str) -> str | None:
    """The first that looks like a video on that site; if none, the first on that site."""
    urls = [u for u in (engine_url(h) for h in hrefs or []) if u.startswith("http") and domain in u.split("/")[2]]
    return next((u for u in urls if VIDEO_PATH.search(u)), urls[0] if urls else None)


def find(service: str, query: str) -> list[dict]:
    """[{title, url, kind, date}] (best first). Empty if nothing is found; RuntimeError if the site fails."""
    key = service_key(service)
    if key in SEARCH_URL:
        return [{"title": f"Search \"{query}\"", "url": SEARCH_URL[key].format(quote_plus(query)), "kind": "búsqueda",
                 "date": None}]
    if key not in FINDERS:
        return []
    try:
        return FINDERS[key](query)
    except (httpx.HTTPError, KeyError, ValueError) as e:
        log.warning("Search %r on %s: %s", query, key, e)
        raise RuntimeError(f"Could not search {service}") from e


# ---------- start playback ----------
# cookie notices: always the option that rejects / only necessary
REJECT = ["#onetrust-reject-all-handler", "#didomi-notice-disagree-button", "#ketch-banner-button-secondary",
          "button[aria-label^='Rechazar']", "#CybotCookiebotDialogBodyButtonDecline"]
REJECT_TEXT = r"^(rechazar( todo| todas( las cookies)?)?|solo (las )?(esenciales|necesarias)|continuar sin aceptar|reject all)$"
# per site: [selector, exact text (regex) or None]; the first visible one is pressed
STEPS = {
    "rtve.es": [[".custom-big", None], [".vjs-big-play-button", None]],
    "atresplayer.com": [["[class*=StyledPlayButton]", "reproducir"], ["button", "^directo$"]],
    "pluto.tv": [],
    "youtube.com": [[".ytp-large-play-button", None]],
    "netflix.com": [["[data-uia='play-button']", None]],
    "primevideo.com": [["[data-testid='play']", None], ["a[href*='autoplay=1']", None]],
    "disneyplus.com": [["[data-testid='playback-action-button']", None]],
}


def steps(host: str) -> list:
    return next((v for d, v in STEPS.items() if host == d or host.endswith("." + d)), [])


def click_js(selectors: list) -> str:
    """JS that presses the first visible element in the list (matching its text, if given). Returns what it pressed, or null."""
    import json
    return """((sel) => {
  for (const [s, t] of sel) {
    const e = [...document.querySelectorAll(s)].find(e => e.offsetWidth > 0 && (!t || new RegExp(t, 'i').test((e.innerText || '').trim())));
    if (e) { e.click(); return s; }
  }
  return null;
})(%s)""" % json.dumps(selectors)


REJECT_JS = click_js([[s, None] for s in REJECT] + [["button, [role=button]", REJECT_TEXT]])

# the site asks for an account (RTVE: La 1 live and DRM content; Netflix/Prime/Disney+ without a session)
WALL_JS = r"""(() => {
  const t = document.body ? document.body.innerText.slice(0, 20000) : '';
  const m = t.match(/exclusivo para usuarios registrados|inicia sesi[oó]n para (ver|seguir|continuar)|reg[ií]strate (gratis )?para (ver|seguir)|ya tengo una cuenta|sign in to watch/i);
  return m ? m[0] : (/\/(login|signin|iniciar-sesion)\b/i.test(location.pathname) ? location.pathname : null);
})()"""


def generic_ok(host: str) -> bool:
    """The generic \"play\" button only on sites without a proven recipe (on these it pressed notices and frames)."""
    # on Netflix it pressed "Regresar a Explorar" (its back button has "play" in its class) and took you out of the video
    return not any(host == d or host.endswith("." + d)
                   for d in ("rtve.es", "pluto.tv", "atresplayer.com", "netflix.com", "primevideo.com", "disneyplus.com"))
