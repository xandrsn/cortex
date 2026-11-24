"""Web video player (RTVE Play, YouTube, Netflix, Pluto TV...).

Cortex opens the page in an Edge window with its own profile (sessions the user signs in to are kept) and
controls it over the Chrome DevTools Protocol (CDP): on any page it finds the main <video> (also inside
iframes) and drives it the same way everywhere. The URL comes from video_sites (with each site's search) or
from Claude. Any site works (except a service disabled in Settings or one on the blocked list); for sites
without a recipe, search_site searches the page itself the way a person would.
To start playback it rejects cookies and presses that site's play button (video_sites.steps) or a generic one.

Inside the panel: the window becomes a child of the panel window (SetParent), placed over the Videos widget's
box and clipped (SetWindowRgn) so only the page shows, with no title bar. That way it moves with the panel: it
does not float above other applications, and it minimizes and moves with it. It is the same window and the same
profile, so sessions (Netflix...) are still there. To sign in, it is released as a normal window ("Window" in
the widget).
"""

import ctypes
import json
import logging
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import psutil
from websockets.sync.client import connect

from . import video_sites, webaccounts
from .config import ROOT, settings

log = logging.getLogger("webplayer")

PORT = 9333
PROFILE = ROOT / "logs" / "edge-tv"
EDGE = next((p for p in (Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
                         Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")) if p.exists()), None)

# ad networks and popups: in the video Edge they never load (like a simple blocker).
# Ads embedded in the video itself (broadcast TV ads, for example) cannot be removed this way.
AD_HOSTS = ["doubleclick.net", "googlesyndication.com", "googleadservices.com", "adservice.google.com",
            "imasdk.googleapis.com", "pubads.g.doubleclick.net", "adnxs.com", "taboola.com", "outbrain.com",
            "popads.net", "popcash.net", "propellerads.com", "adsterra.com", "exoclick.com", "juicyads.com",
            "trafficjunky.net", "onclickads.net", "onclckads.com", "hilltopads.net", "a-ads.com", "adcash.com",
            "clickadu.com", "mgid.com", "revcontent.com", "zedo.com", "criteo.com", "amazon-adsystem.com",
            "smartadserver.com", "rubiconproject.com", "pubmatic.com", "openx.net", "teads.tv", "yieldmo.com"]
HOST_RULES = ", ".join(f"MAP {h} 0.0.0.0, MAP *.{h} 0.0.0.0" for h in AD_HOSTS)

# the <video> that matters: the largest one on the page (or the one that already has content)
FIND = """(() => {
  const vs = [...document.querySelectorAll('video')]
    .map(v => [v, v.offsetWidth * v.offsetHeight + (v.readyState > 0 ? 1 : 0)]).filter(([, a]) => a > 0)
    .sort((a, b) => b[1] - a[1]);
  if (!vs.length) return null;
  const v = vs[0][0];
  %s
})()"""
STATE = "return {paused: v.paused, pos: v.currentTime || 0, duration: isFinite(v.duration) ? v.duration : null, " \
        "volume: v.volume, title: document.title};"
ACTIONS = {
    "toggle": "v.paused ? v.play() : v.pause();",
    "pause": "v.pause();",
    "resume": "v.play();",
    "seek": "v.currentTime = Math.max(0, v.currentTime + (%s));",
    "volume": "v.muted = false; v.volume = Math.min(1, Math.max(0, v.volume + (%s) / 100));",
    "duck": "if (!v.dataset.pepitoVol) { v.dataset.pepitoVol = v.volume; v.volume = v.volume * 0.5; }",
    "unduck": "if (v.dataset.pepitoVol) { v.volume = Number(v.dataset.pepitoVol); delete v.dataset.pepitoVol; }",
    "fullscreen": "if (document.fullscreenElement) document.exitFullscreen(); "
                  "else (v.closest('[class*=player i], [id*=player i]') || v).requestFullscreen();",
}
# if there is no video yet, press whatever looks like a play button; never a link to another page
# (on RTVE the "RTVE Play" logo links to the home page: pressing it used to leave the home page stuck)
CLICK_PLAY = """(() => {
  const sel = ['[aria-label*="reproducir" i]', '[aria-label*="play" i]', '[title*="reproducir" i]', '[title*="play" i]',
               '[class*="play-button" i]', '[class*="playbutton" i]', '[data-testid*="play" i]', 'button[class*="play" i]'];
  const nav = e => { const a = e.closest('a[href]'); return a && !/^(#|javascript:)/.test(a.getAttribute('href')); };
  for (const s of sel) {
    const b = [...document.querySelectorAll(s)].find(e => e.offsetWidth > 0 && !nav(e) && !/logo/i.test(e.className) &&
      !/playlist|lista|rtve ?play|pluto ?tv|youtube/i.test(e.textContent + (e.getAttribute('aria-label') || '')) &&
      !/player/i.test((e.getAttribute('aria-label') || '') + (e.getAttribute('title') || '')));   // "video player": that is the frame, not the button
    if (b) { b.click(); return s + ' «' + (b.getAttribute('aria-label') || b.textContent).trim().slice(0, 40) + '»'; }
  }
  return null;
})()"""


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def check(url: str) -> str | None:
    """None if it can be opened; otherwise the reason."""
    host = _host(url)
    if not host or urlparse(url).scheme not in ("http", "https"):
        return "That address is not a website."
    if any(b in host for b in settings["blocked_sites"]):
        return "That site is on your blocked list (Settings)."
    for s in settings["video_services"]:
        if any(host == d or host.endswith("." + d) for d in s["domains"]):
            return None if s.get("enabled", True) else f"{s['name']} is disabled in Settings."
    return None


def site(url: str) -> str:
    """The base domain (cuevana.biz, rtve.es...): links to other domains are treated as ads."""
    return ".".join(_host(url).split(".")[-2:])


# ---------- sign in with the saved account (webaccounts) ----------
# fills in the username and/or password (also on two-step logins: email first, then password) and presses
# sign in. Returns "password", "user" or null if there is nothing to fill in on the page.
LOGIN_JS = r"""((user, pass) => {
  const vis = e => e && e.offsetWidth > 0 && e.offsetHeight > 0 && !e.disabled;
  const set = (e, v) => {
    e.focus();
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(e, v);
    e.dispatchEvent(new Event('input', {bubbles: true}));
    e.dispatchEvent(new Event('change', {bubbles: true}));
  };
  const inputs = [...document.querySelectorAll('input')].filter(vis);
  const pw = inputs.find(e => e.type === 'password');
  const userBox = inputs.find(e => /^(email|tel)$/.test(e.type) || /username|email/i.test(e.autocomplete || '')
      || /user|email|login|correo|usuario|identifier/i.test((e.name || '') + ' ' + (e.id || '')))
    || (pw ? inputs.filter(e => e.type === 'text' && (e.compareDocumentPosition(pw) & 4)).pop() : null);
  const login = /login|signin|sign-in|iniciar|acceso|auth|cuenta|account/i.test(location.href)
    || /inicia(r)? sesi|sign in|log in|identif|acceder/i.test(document.title + ' ' + [...document.querySelectorAll('h1, h2')].map(h => h.innerText).join(' '));
  if (!pw && !(userBox && login)) return null;
  if (userBox && userBox.value !== user) set(userBox, user);
  if (pw) set(pw, pass);
  const form = (pw || userBox).form;
  const btn = [...(form || document).querySelectorAll('button, input[type=submit], [role=button]')].find(b => vis(b) &&
    (b.type === 'submit' || /^(iniciar sesi[oó]n|entrar|acceder|log ?in|sign ?in|continuar|siguiente|next|continue)$/i
      .test((b.innerText || b.value || '').trim())));
  if (btn) btn.click(); else if (form) (form.requestSubmit ? form.requestSubmit() : form.submit());
  return pw ? 'password' : 'user';
})(%s, %s)"""


# ---------- search on any site ----------
# types into the search box (if it is hidden behind a magnifier icon, presses the icon); returns what it did
SEARCH_JS = r"""((q) => {
  const vis = e => e && e.offsetWidth > 0 && e.offsetHeight > 0;
  const boxes = () => [...document.querySelectorAll('input[type=search], input[name=q], input[name=s], input[name=search], '
    + 'input[name=query], input[name=keyword], input[name=k], input[placeholder*="busca" i], input[placeholder*="search" i], '
    + 'input[aria-label*="busca" i], input[aria-label*="search" i], input[id*="search" i], input[class*="search" i]')]
    .filter(e => vis(e) && !e.disabled && !/^(hidden|checkbox|radio|submit)$/i.test(e.type));
  let box = boxes()[0];
  if (!box) {
    const icon = [...document.querySelectorAll('button, a, [role=button], i, svg, span')].find(e => vis(e) &&
      /(search|busca|lupa)/i.test((e.getAttribute('aria-label') || '') + ' ' + (e.getAttribute('title') || '') + ' '
        + (typeof e.className === 'string' ? e.className : '') + ' ' + (e.id || '')));
    if (!icon) return null;
    (icon.closest('button, a, [role=button]') || icon).click();
    return 'icon';
  }
  box.focus();
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  set.call(box, q);
  box.dispatchEvent(new Event('input', {bubbles: true}));
  box.dispatchEvent(new Event('change', {bubbles: true}));
  return 'box';
})(%s)"""

# submits the search: the box's form, or Enter
SUBMIT_JS = r"""(() => {
  const box = document.activeElement;
  if (!box || box.tagName !== 'INPUT') return false;
  const f = box.form;
  if (f) { (f.requestSubmit ? f.requestSubmit() : f.submit()); return true; }
  for (const t of ['keydown', 'keypress', 'keyup'])
    box.dispatchEvent(new KeyboardEvent(t, {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
  return true;
})()"""

# the first result (in page order) that resembles the query, on the same domain and not looking like an ad
PICK_JS = r"""((q, base) => {
  const norm = t => (t || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
  const words = norm(q).split(/[^a-z0-9]+/).filter(w => w.length > 1);
  if (!words.length) return null;
  const here = location.href.split('#')[0];
  const ad = /(^|[^a-z])(ads?|sponsor|promo|patrocinad|anuncio|banner|advert)([^a-z]|$)/i;
  let best = null, bestScore = 0;
  for (const a of document.querySelectorAll('a[href]')) {
    if (!(a.offsetWidth > 0 && a.offsetHeight > 0)) continue;
    let u; try { u = new URL(a.href, location.href); } catch { continue; }
    if (!/^https?:$/.test(u.protocol)) continue;
    if (u.hostname.split('.').slice(-2).join('.') !== base) continue;   // other domain: ad
    if (u.href.split('#')[0] === here) continue;
    if (/[?&](q|s|search|query|keyword)=|\/(search|buscar|busqueda|login|signin|registro|register|genero|genre|category|categoria|tag)(\/|$)/i.test(u.pathname + u.search)) continue;
    if (a.closest('nav, header, footer, [class*="menu" i], [class*="pagination" i]')) continue;
    const box = a.closest('[class*="ad" i], [id*="ad" i]');
    const cls = e => (typeof e.className === 'string' ? e.className : '') + ' ' + (e.id || '');
    if (ad.test(cls(a)) || (box && ad.test(cls(box)))) continue;
    const img = a.querySelector('img');
    let path = u.pathname; try { path = decodeURIComponent(path); } catch {}
    const text = norm([a.innerText, a.title, a.getAttribute('aria-label'), img && img.alt, path].join(' '));
    const score = words.filter(w => text.includes(w)).length / words.length;
    if (score > bestScore + 0.001) { best = u.href; bestScore = score; }
    if (score === 1) break;   // the first full match: that one
  }
  return bestScore >= 0.5 ? best : null;
})(%s, %s)"""


# the video fills the page (or its iframe) when inside the panel
FILL_JS = """(() => {
  let best = null, area = 0;
  for (const v of document.querySelectorAll('video')) {
    const r = v.getBoundingClientRect();
    if (r.width * r.height > area) { area = r.width * r.height; best = v; }
  }
  if (!best && window === top) {
    for (const f of document.querySelectorAll('iframe')) {
      const r = f.getBoundingClientRect();
      if (r.width * r.height > area && r.width > 300) { area = r.width * r.height; best = f; }
    }
  }
  document.querySelectorAll('[data-pepito-fill]').forEach(e => e !== best && e.removeAttribute('data-pepito-fill'));
  if (!best) return null;
  // if it already fills almost the whole window (Netflix, Prime...) leave it alone: touching it breaks its player
  if (!best.hasAttribute('data-pepito-fill') && area > innerWidth * innerHeight * .8) return 'already full';
  best.setAttribute('data-pepito-fill', '');
  if (!document.getElementById('pepito-fill')) {
    const s = document.createElement('style');
    s.id = 'pepito-fill';
    s.textContent = '[data-pepito-fill]{position:fixed!important;inset:0!important;width:100vw!important;height:100vh!important;'
      + 'max-width:none!important;max-height:none!important;z-index:2147483646!important;background:#000!important;object-fit:contain!important;margin:0!important}';
    document.documentElement.append(s);
  }
  return best.tagName;
})()"""
UNFILL_JS = "document.getElementById('pepito-fill')?.remove(); document.querySelectorAll('[data-pepito-fill]').forEach(e => e.removeAttribute('data-pepito-fill')); true"

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _dpi_aware():
    """Coordinates in real screen pixels (as the panel sends them)."""
    try:   # it is per thread, and each command arrives on a different one
        _user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))   # per-monitor v2
    except Exception:
        pass


def _listening(port: int) -> bool:
    try:
        return any(c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN
                   for c in psutil.net_connections("tcp4"))
    except psutil.Error:
        return True   # cannot check: try as before


class WebPlayer:
    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._id = 0
        self._win: int | None = None   # the video window
        self._logins = 0   # sign-in attempts on the open site (so it does not keep trying if the password fails)
        self.wall: str | None = None   # site that asked to sign in when opened
        self.rect: dict | None = None   # widget box on screen (real pixels)
        self.docked = True              # inside the panel; False = normal window (to sign in)

    # ---------- CDP ----------
    def _targets(self) -> list[dict]:
        # with no window open, Windows takes 1 s to refuse the connection: first check whether anyone is
        # listening (3 ms). This used to delay every voice command by 1 s (it asks whether a web video is playing)
        if not _listening(PORT):
            return []
        try:
            return httpx.get(f"http://127.0.0.1:{PORT}/json", timeout=1).json()
        except (httpx.HTTPError, ValueError):
            return []

    def active(self) -> bool:
        return any(t.get("type") == "page" for t in self._targets())

    def _call(self, target: dict, method: str, params: dict | None = None):
        with self._lock:
            self._id += 1
            mid = self._id
        with connect(target["webSocketDebuggerUrl"], open_timeout=3, close_timeout=1, max_size=None) as ws:
            ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                msg = json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
                if msg.get("id") == mid:
                    return msg.get("result", {})
        return {}

    def _eval(self, target: dict, expression: str):
        r = self._call(target, "Runtime.evaluate", {"expression": expression, "returnByValue": True, "userGesture": True})
        return (r.get("result") or {}).get("value")

    def _on_video(self, body: str):
        """Runs `body` on the main video (page or iframes). None if there is no video."""
        targets = [t for t in self._targets() if t.get("type") in ("page", "iframe")]
        targets.sort(key=lambda t: t.get("type") != "page")
        for t in targets:
            try:
                value = self._eval(t, FIND % body)
            except Exception:
                continue
            if value is not None and not isinstance(value, str):   # some sites (Pluto) return odd text
                return value
        # the player in a frame from another site (Dailymotion...): it is not in /json, reached through the page
        for page in [t for t in targets if t.get("type") == "page"][:1]:
            try:
                for value in self._eval_subframes(page, FIND % body):
                    if value is not None and not isinstance(value, str):
                        return value
            except Exception:
                pass
        return None

    def _eval_subframes(self, page: dict, expression: str) -> list:
        """Evaluates in the page's cross-site frames (CDP auto-attach), which /json does not list."""
        out = []
        with connect(page["webSocketDebuggerUrl"], open_timeout=3, close_timeout=1, max_size=None) as ws:
            ws.send(json.dumps({"id": 1, "method": "Target.setAutoAttach",
                                "params": {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True}}))
            sessions, end = [], time.monotonic() + 0.6
            while time.monotonic() < end:
                try:
                    msg = json.loads(ws.recv(timeout=max(0.05, end - time.monotonic())))
                except TimeoutError:
                    break
                if msg.get("method") == "Target.attachedToTarget" and msg["params"]["targetInfo"].get("type") == "iframe":
                    sessions.append(msg["params"]["sessionId"])
            ids = iter(range(2, 10_000))

            def ask(sid, method, params):
                n = next(ids)
                ws.send(json.dumps({"id": n, "method": method, "params": params, **({"sessionId": sid} if sid else {})}))
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    try:
                        msg = json.loads(ws.recv(timeout=max(0.05, deadline - time.monotonic())))
                    except TimeoutError:
                        break
                    if msg.get("id") == n:
                        return msg.get("result") or {}
                return {}

            # cross-domain frames in the same process (geo.dailymotion.com inside www.): an "isolated world" in
            # each shares the DOM, so its <video> can be touched; frames in another process, via their session
            for sid in [None, *sessions]:
                tree = ask(sid, "Page.getFrameTree", {}).get("frameTree") or {}
                frames, stack = [], list(tree.get("childFrames") or []) + ([tree] if sid else [])
                while stack:
                    f = stack.pop()
                    frames.append(f["frame"]["id"])
                    stack += f.get("childFrames") or []
                for fid in frames:
                    ctx = ask(sid, "Page.createIsolatedWorld", {"frameId": fid, "worldName": "pepito"}).get("executionContextId")
                    if ctx:
                        r = ask(sid, "Runtime.evaluate", {"expression": expression, "contextId": ctx,
                                                          "returnByValue": True, "userGesture": True})
                        out.append((r.get("result") or {}).get("value"))
        return out

    # ---------- commands ----------
    def _page(self) -> dict | None:
        pages = [t for t in self._targets() if t.get("type") == "page"]
        return pages[0] if pages else None

    def _kill_popups(self, seconds: float = 40):
        """Closes new tabs or windows the site opens (click ads): only the original stays."""
        end = time.monotonic() + seconds
        first = None
        while time.monotonic() < end:
            pages = [t for t in self._targets() if t.get("type") == "page"]
            if pages and first is None:
                first = pages[-1]["id"]   # /json lists the most recent first: the original is the last one
            for t in pages:
                if first and t["id"] != first:
                    log.info("Closing popup window: %s", t.get("url", "")[:100])
                    try:
                        httpx.get(f"http://127.0.0.1:{PORT}/json/close/{t['id']}", timeout=1)
                    except httpx.HTTPError:
                        pass
            time.sleep(0.5)

    def search_site(self, url: str, query: str, wait: float = 30) -> str | None:
        """On any site with its home page already open: searches for `query` (its search box or, failing that, the
        typical search URLs) and returns the URL of the first result that is not an ad. None if nothing is found."""
        from urllib.parse import quote_plus
        base = site(url)
        root = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        q = quote_plus(query)
        guesses = [f"{root}/search?q={q}", f"{root}/?s={q}", f"{root}/buscar?q={q}", f"{root}/search/{q}"]
        # the caller (open) has already opened the home page
        enc = json.dumps
        box_done, misses, icons = False, 0, 0
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            time.sleep(1.5)
            if self.wall:
                return None
            page = self._page()
            if not page or site(page.get("url", "")) != base:
                continue
            try:
                self._eval(page, video_sites.REJECT_JS)
                if self.try_login(page, back_to=url):
                    continue
                if box_done:   # already searched: are there results?
                    found = self._eval(page, PICK_JS % (enc(query), enc(base)))
                    if found:
                        return found
                    misses += 1
                    if misses < 3:   # on some sites the results take a while to render
                        continue
                    misses = 0
                else:
                    did = self._eval(page, SEARCH_JS % enc(query))
                    if did == "icon" and icons < 2:
                        icons += 1
                        continue   # the magnifier icon opens the box: type into it on the next pass
                    box_done = True
                    if did == "box":
                        time.sleep(0.3)
                        # a real Enter (via CDP): React sites ignore one simulated in JS
                        for kind in ("keyDown", "keyUp"):
                            self._call(page, "Input.dispatchKeyEvent", {"type": kind, "key": "Enter", "code": "Enter",
                                                                        "windowsVirtualKeyCode": 13,
                                                                        **({"text": "\r"} if kind == "keyDown" else {})})
                        time.sleep(1)
                        if site(self._page().get("url", "")) == base:
                            self._eval(page, SUBMIT_JS)   # in case Enter did nothing: the form
                        time.sleep(1.5)
                        continue
                if guesses:   # no box, or the box gave nothing: the most typical search URLs
                    nxt = guesses.pop(0)
                    log.info("Searching %s by trying %s", base, nxt)
                    self._call(page, "Page.navigate", {"url": nxt})
                    time.sleep(2)
            except Exception as e:
                log.info("Search on %s: %s", base, e)
        return None

    def try_login(self, page: dict, back_to: str | None = None) -> bool:
        """If the site asks for an account and one is saved for it, fills it in and signs in (max. 4 steps per open site).
        With back_to, returns to that address afterwards (many sites leave you on their home page after signing in)."""
        host = _host(page.get("url", ""))
        account = self._logins < 4 and webaccounts.get(host)
        if not account:
            return False
        try:
            did = self._eval(page, LOGIN_JS % (json.dumps(account[0]), json.dumps(account[1])))
        except Exception:
            did = None
        if not did:
            return False
        self._logins += 1
        log.info("Signing in to %s (%s)", host, "password" if did == "password" else "user")
        if did == "password" and back_to:
            time.sleep(6)
            now = self._page()
            if now and now.get("url", "").split("#")[0] != back_to and site(now.get("url", "")) == site(back_to):
                try:
                    self._call(now, "Page.navigate", {"url": back_to})
                except Exception:
                    pass
        return True

    def search_engine(self, url: str, query: str) -> str | None:
        """Plan B: \"site:domain query\" on a search engine, inside the same window, and the first result."""
        from urllib.parse import quote_plus
        domain = site(url)
        page = self._page()
        if not page:
            return None
        for engine in video_sites.ENGINES:
            target = engine.format(q=quote_plus(f"site:{domain} {query}"))
            log.info("Searching with the search engine: %s", target)
            try:
                self._call(page, "Page.navigate", {"url": target})
            except Exception:
                continue
            for _ in range(8):
                time.sleep(1)
                page = self._page() or page
                try:
                    found = video_sites.pick_engine_result(self._eval(page, video_sites.ENGINE_LINKS_JS), domain)
                except Exception:
                    found = None
                if found:
                    return found
        return None

    def open(self, url: str) -> None:
        reason = check(url)
        if reason:
            raise RuntimeError(reason)
        pages = [t for t in self._targets() if t.get("type") == "page"]
        self.wall = None
        self._logins = 0
        if pages:
            try:   # some sites are slow to release the previous page: navigation continues even if it does not answer
                self._call(pages[0], "Page.navigate", {"url": url})
                self._call(pages[0], "Page.bringToFront")
            except TimeoutError:
                log.info("Edge is slow to change page")
        else:
            if EDGE is None:
                raise RuntimeError("Edge not found")
            PROFILE.mkdir(parents=True, exist_ok=True)
            # windowed (right half), not full screen: that is requested separately
            self._proc = subprocess.Popen([
                str(EDGE), f"--app={url}", f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}",
                "--no-first-run", "--autoplay-policy=no-user-gesture-required",
                # covered by other windows it still counts as "visible": otherwise RTVE stops loading the video
                "--disable-features=Translate,CalculateNativeWinOcclusion", "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding", "--disable-background-timer-throttling",
                # without DirectComposition Windows honors the window clipping (otherwise the title bar would show)
                "--disable-direct-composition",
                f"--host-rules={HOST_RULES}",
                "--window-position=940,90", "--window-size=960,600"])
        log.info("Web video: %s", url)
        threading.Thread(target=self._autoplay, args=(url,), daemon=True, name="webplayer-autoplay").start()
        threading.Thread(target=self._keep_docked, daemon=True, name="webplayer-dock").start()
        if not video_sites.steps(_host(url)):   # sites without a recipe (unknown ones) tend to open ads
            threading.Thread(target=self._kill_popups, daemon=True, name="webplayer-popups").start()

    # ---------- inside the panel ----------
    def _hwnd(self) -> int | None:
        """Main window of the video Edge (the largest visible one of its process)."""
        # once inside the panel it is no longer top-level (EnumWindows cannot see it): remember it
        if self._win and _user32.IsWindow(ctypes.c_void_p(self._win)) and _user32.GetParent(ctypes.c_void_p(self._win)):
            return self._win
        pid = self._proc.pid if self._proc and self._proc.poll() is None else None
        try:
            ver = httpx.get(f"http://127.0.0.1:{PORT}/json/version", timeout=1).json()
            info = self._call({"webSocketDebuggerUrl": ver["webSocketDebuggerUrl"]}, "SystemInfo.getProcessInfo")
            pid = next((p["id"] for p in info.get("processInfo", []) if p.get("type") == "browser"), pid)
        except Exception:
            pass
        if not pid:
            return None
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def each(h, _):
            owner = ctypes.c_ulong()
            _user32.GetWindowThreadProcessId(ctypes.c_void_p(h), ctypes.byref(owner))
            if owner.value == pid and _user32.IsWindowVisible(ctypes.c_void_p(h)):
                r = _RECT()
                _user32.GetWindowRect(ctypes.c_void_p(h), ctypes.byref(r))
                found.append(((r.right - r.left) * (r.bottom - r.top), h))
            return True

        _user32.EnumWindows(each, None)
        self._win = max(found)[1] if found else None
        return self._win

    def _page_metrics(self) -> tuple[float, float] | None:
        pages = [t for t in self._targets() if t.get("type") == "page"]
        if not pages:
            return None
        try:
            v = self._eval(pages[0], "[innerHeight * devicePixelRatio, innerWidth * devicePixelRatio]")
            return (float(v[0]), float(v[1])) if v else None
        except Exception:
            return None

    def _panel_hwnd(self, video: int) -> int | None:
        """The panel window (Edge in app mode titled \"Cortex\"), other than the video one."""
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def each(h, _):
            buf, cls = ctypes.create_unicode_buffer(64), ctypes.create_unicode_buffer(64)
            _user32.GetWindowTextW(ctypes.c_void_p(h), buf, 64)
            _user32.GetClassNameW(ctypes.c_void_p(h), cls, 64)
            if h != video and buf.value == "Cortex" and cls.value.startswith("Chrome_WidgetWin") \
                    and _user32.IsWindowVisible(ctypes.c_void_p(h)):
                found.append(h)
            return True

        _user32.EnumWindows(each, None)
        return found[0] if found else None

    def _set_child(self, h: ctypes.c_void_p, parent: int | None) -> None:
        """Puts the video window inside the panel (parent) or releases it again (None)."""
        GWL_STYLE, WS_CHILD, WS_POPUP, WS_CAPTION, WS_THICKFRAME = -16, 0x40000000, 0x80000000, 0x00C00000, 0x00040000
        get = getattr(_user32, "GetWindowLongPtrW", _user32.GetWindowLongW)
        put = getattr(_user32, "SetWindowLongPtrW", _user32.SetWindowLongW)
        get.restype = put.restype = ctypes.c_ssize_t
        put.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        style = get(h, GWL_STYLE)
        current = _user32.GetParent(h)
        if parent:
            if current != parent:
                put(h, GWL_STYLE, (style | WS_CHILD) & ~(WS_POPUP | WS_CAPTION | WS_THICKFRAME))
                _user32.SetParent(h, ctypes.c_void_p(parent))
        elif current:
            _user32.SetParent(h, None)
            put(h, GWL_STYLE, (style & ~WS_CHILD) | WS_CAPTION | WS_THICKFRAME)

    def dock(self, rect: dict | None = None) -> bool:
        """Places the page exactly in the widget's box, inside the panel window and frameless."""
        if rect:
            self.rect = rect
        self.docked = True
        r = self.rect
        if not r or r.get("w", 0) < 80 or not self.active():
            return False
        _dpi_aware()
        hwnd = self._hwnd()
        m = self._page_metrics()
        if not hwnd or not m:
            return False
        h = ctypes.c_void_p(hwnd)
        panel = _user32.GetParent(h) or self._panel_hwnd(hwnd)
        if panel and _user32.IsIconic(ctypes.c_void_p(panel)):
            return False   # panel minimized: when it returns, the panel sends its box and the page is placed then
        if panel:
            self._set_child(h, panel)
        _user32.SetWindowRgn(h, None, False)   # measure without clipping
        wr, cr, org = _RECT(), _RECT(), _POINT(0, 0)
        _user32.GetWindowRect(h, ctypes.byref(wr))
        _user32.GetClientRect(h, ctypes.byref(cr))
        _user32.ClientToScreen(h, ctypes.byref(org))
        title = max(0, round(cr.bottom - m[0]))          # title bar (inside the client area)
        dx = org.x - wr.left                               # invisible left border
        dy = org.y - wr.top + title
        right = wr.right - (org.x + cr.right)
        bottom = wr.bottom - (org.y + cr.bottom)
        dy += 3   # the page's top border outside as well
        x, y, w, hh = int(r["x"]), int(r["y"]), int(r["w"]), int(r["h"])
        try:   # no rounded corners or Windows 11 border
            dwm = ctypes.windll.dwmapi
            dwm.DwmSetWindowAttribute(h, 33, ctypes.byref(ctypes.c_int(1)), 4)
            dwm.DwmSetWindowAttribute(h, 34, ctypes.byref(ctypes.c_uint(0xFFFFFFFE)), 4)
        except Exception:
            pass
        SWP_NOACTIVATE, SWP_SHOWWINDOW, SWP_NOZORDER, SWP_FRAMECHANGED = 0x0010, 0x0040, 0x0004, 0x0020
        pos = _POINT(x - dx, y - dy)
        if panel:   # inside the panel, coordinates are relative to it
            _user32.ScreenToClient(ctypes.c_void_p(panel), ctypes.byref(pos))
            _user32.SetWindowPos(h, ctypes.c_void_p(0), pos.x, pos.y, w + dx + right, hh + dy + bottom,
                                 SWP_NOACTIVATE | SWP_SHOWWINDOW | SWP_FRAMECHANGED)
        else:   # no panel in sight (should not happen): as before, on top
            _user32.SetWindowPos(h, ctypes.c_void_p(-1), pos.x, pos.y, w + dx + right, hh + dy + bottom,
                                 SWP_NOACTIVATE | SWP_SHOWWINDOW)
        rgn = _gdi32.CreateRectRgn(dx, dy, dx + w, dy + hh)
        _user32.SetWindowRgn(h, ctypes.c_void_p(rgn), True)   # Windows takes ownership of the region
        self._fill(True)
        return True

    def undock(self) -> bool:
        """Normal window, in front (to sign in or watch at leisure)."""
        self.docked = False
        _dpi_aware()
        hwnd = self._hwnd()
        if not hwnd:
            return False
        h = ctypes.c_void_p(hwnd)
        self._fill(False)
        self._set_child(h, None)
        _user32.SetWindowRgn(h, None, True)
        sw, sh = _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1)
        HWND_NOTOPMOST, SWP_SHOWWINDOW, SWP_FRAMECHANGED = -2, 0x0040, 0x0020
        _user32.SetWindowPos(h, ctypes.c_void_p(HWND_NOTOPMOST), int(sw * .2), int(sh * .1), int(sw * .6), int(sh * .75),
                             SWP_SHOWWINDOW | SWP_FRAMECHANGED)
        _user32.SetForegroundWindow(h)
        return True

    def hide(self) -> None:
        """Out of sight (e.g. with the Videos widget hidden): keeps playing, unseen."""
        hwnd = self._hwnd()
        if hwnd and self.docked:
            _user32.ShowWindow(ctypes.c_void_p(hwnd), 0)   # SW_HIDE; dock() shows it again

    def _fill(self, on: bool) -> None:
        for t in [t for t in self._targets() if t.get("type") in ("page", "iframe")]:
            try:
                self._eval(t, FILL_JS if on else UNFILL_JS)
            except Exception:
                pass

    def _keep_docked(self):
        """The window is slow to appear and sites change page: it is repositioned for a while after opening."""
        for i in range(20):
            time.sleep(1.5)
            if self.wall:
                self.undock()
                return
            if self.docked and self.rect and self.active():
                self.dock()

    # first search result of each site that needs a login -> URL that plays it
    PICK_JS = {
        "netflix": """(() => { const a = document.querySelector('[data-uia=gallery] [data-uia=standard-card], [data-uia=search-gallery-video-card] a');
            const id = a && (new URL(a.href, location.href).searchParams.get('jbv') || (a.href.match(/\\/(?:watch|title)\\/(\\d+)/) || [])[1]);
            return id ? 'https://www.netflix.com/watch/' + id : null; })()""",
        "prime": "(() => { const a = document.querySelector('a[href*=\"/detail/\"]'); return a ? a.href : null; })()",
        "disney": "(() => { const a = document.querySelector('a[href*=\"/browse/entity-\"]'); return a ? a.href : null; })()",
    }

    def pick_result(self, service: str, wait: float = 15) -> str | None:
        """With the search open, waits for the results and returns the URL to watch the first one."""
        js = self.PICK_JS.get(service)
        deadline = time.monotonic() + wait
        while js and time.monotonic() < deadline:
            time.sleep(1)
            if self.wall:
                return None
            pages = [t for t in self._targets() if t.get("type") == "page"]
            try:
                url = pages and self._eval(pages[0], js)
            except Exception:
                url = None
            if url:
                return url
        return None

    def _autoplay(self, url: str):
        """Up to 30 s: rejects the cookie notice, presses that site's play button (video_sites) or a generic one,
        and if a video is stopped, plays it with sound. Stops once it sees it playing twice in a row."""
        site = lambda u: ".".join(_host(u).split(".")[-2:])   # rtve.es, pluto.tv...
        recipe = video_sites.steps(_host(url))
        if "/watch/" in urlparse(url).path:
            recipe = []   # Netflix starts by itself; its in-player play button would pause it
        if urlparse(url).path.rstrip("/") in ("", "/browse", "/home", "/inicio", "/play"):
            return   # a service's home page: it opens and you choose (otherwise Netflix plays the first thing it sees)
        playing = clicked = stuck = 0
        for _ in range(30):
            time.sleep(1)
            pages = [t for t in self._targets() if t.get("type") == "page"]
            if not pages or site(pages[0].get("url", "")) != site(url):
                continue   # the previous page is still showing
            try:
                self._eval(pages[0], video_sites.REJECT_JS)
            except Exception:
                pass
            # only until it plays: if the user pauses it later, it stays paused
            state = self._on_video("if (!v.dataset.pepitoOn) { if (v.paused) v.play(); "
                                   "else { v.dataset.pepitoOn = 1; v.muted = false; } } " + STATE)
            if state and not state["paused"]:
                playing += 1
                if playing >= 2:
                    return
                continue
            playing, stuck = 0, stuck + 1
            if state is None and stuck >= 2 and self.try_login(pages[0], back_to=url):
                stuck = 0
                continue
            try:
                wall = state is None and stuck >= 3 and self._eval(pages[0], video_sites.WALL_JS)
            except Exception:
                wall = None
            if wall:   # asks for an account: the user does that, not Cortex
                log.info("Autoplay: %s asks to sign in (%s)", _host(url), wall)
                self.wall = _host(url)
                return
            # no video, or a video that does not start with play(): press the button (not together with play(), which would stop it)
            if clicked < 3 and (state is None or stuck % 3 == 0):
                try:
                    hit = ((recipe and self._eval(pages[0], video_sites.click_js(recipe)))
                           or (state is None and stuck >= 4 and video_sites.generic_ok(_host(url))
                               and self._eval(pages[0], CLICK_PLAY)))
                except Exception:
                    hit = None
                if hit:
                    log.info("Autoplay: pressing %s", hit)
                    clicked += 1

    def command(self, action: str, value: float | None = None) -> None:
        if action == "stop":
            return self.close()
        js = ACTIONS.get(action)
        if js is None:
            return
        if "%s" in js:
            js = js % float(value if value is not None else 10)
        self._on_video(js + " return true;")

    def status(self) -> dict:
        if not self.active():
            return {"playing": False}
        st = self._on_video(STATE)
        page = self._page()
        title = page.get("title", "") if page else ""
        if not isinstance(st, dict) or not st:   # odd sites return text, numbers... instead of the state
            return {"playing": True, "source": "web", "docked": self.docked, "title": title,
                    "paused": True, "pos": None, "duration": None}
        # the page title, not the player's ("Dailymotion Video Player")
        return {"playing": True, "source": "web", "docked": self.docked, **st, "title": title or st.get("title", "")}

    def close(self) -> None:
        try:
            ver = httpx.get(f"http://127.0.0.1:{PORT}/json/version", timeout=1).json()
            self._call({"webSocketDebuggerUrl": ver["webSocketDebuggerUrl"]}, "Browser.close")
        except Exception:
            if self._proc and self._proc.poll() is None:
                self._proc.kill()
        self._proc = None
