"""Reproductor de webs de vídeo (RTVE Play, YouTube, Netflix, Pluto TV…).

Pepito abre la página en una ventana de Edge con su propio perfil (las sesiones que Charlie inicie se quedan
guardadas) y la controla por el protocolo de depuración (CDP): en cualquier página busca el <video> principal
—también dentro de iframes— y lo maneja igual en todas. La URL la saca video_sites (con la búsqueda de cada web)
o Claude. Vale cualquier web (salvo un servicio desactivado en Ajustes o una de la lista de bloqueadas); en las
que no tienen receta, search_site busca en la propia página como haría una persona.
Para arrancar: rechaza las cookies y pulsa el play de esa web (video_sites.steps) o uno genérico.

Dentro del panel: la ventana pasa a ser hija de la del panel (SetParent), colocada en el recuadro del widget
Vídeos y recortada (SetWindowRgn) para que solo se vea la página, sin barra de título. Así va con el panel: no
flota encima de otras aplicaciones, se minimiza y se mueve con él. Es la misma ventana y el mismo perfil, así que
las sesiones (Netflix…) siguen ahí. Para iniciar sesión se suelta como ventana normal («Ventana» en el widget).
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

# redes de anuncios y ventanas emergentes: en el Edge de vídeos no llegan a cargar (como un bloqueador sencillo).
# Los anuncios metidos dentro del propio vídeo (los de la tele, p. ej.) no se pueden quitar así.
AD_HOSTS = ["doubleclick.net", "googlesyndication.com", "googleadservices.com", "adservice.google.com",
            "imasdk.googleapis.com", "pubads.g.doubleclick.net", "adnxs.com", "taboola.com", "outbrain.com",
            "popads.net", "popcash.net", "propellerads.com", "adsterra.com", "exoclick.com", "juicyads.com",
            "trafficjunky.net", "onclickads.net", "onclckads.com", "hilltopads.net", "a-ads.com", "adcash.com",
            "clickadu.com", "mgid.com", "revcontent.com", "zedo.com", "criteo.com", "amazon-adsystem.com",
            "smartadserver.com", "rubiconproject.com", "pubmatic.com", "openx.net", "teads.tv", "yieldmo.com"]
HOST_RULES = ", ".join(f"MAP {h} 0.0.0.0, MAP *.{h} 0.0.0.0" for h in AD_HOSTS)

# el <video> que importa: el más grande de la página (o el que ya tenga contenido)
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
# si aún no hay vídeo, se pulsa lo que parezca un botón de reproducir; nunca un enlace a otra página
# (en RTVE el logo «RTVE Play» es un enlace a la portada: pulsarlo era lo que dejaba la portada parada)
CLICK_PLAY = """(() => {
  const sel = ['[aria-label*="reproducir" i]', '[aria-label*="play" i]', '[title*="reproducir" i]', '[title*="play" i]',
               '[class*="play-button" i]', '[class*="playbutton" i]', '[data-testid*="play" i]', 'button[class*="play" i]'];
  const nav = e => { const a = e.closest('a[href]'); return a && !/^(#|javascript:)/.test(a.getAttribute('href')); };
  for (const s of sel) {
    const b = [...document.querySelectorAll(s)].find(e => e.offsetWidth > 0 && !nav(e) && !/logo/i.test(e.className) &&
      !/playlist|lista|rtve ?play|pluto ?tv|youtube/i.test(e.textContent + (e.getAttribute('aria-label') || '')) &&
      !/player/i.test((e.getAttribute('aria-label') || '') + (e.getAttribute('title') || '')));   // «video player»: es el marco, no el botón
    if (b) { b.click(); return s + ' «' + (b.getAttribute('aria-label') || b.textContent).trim().slice(0, 40) + '»'; }
  }
  return null;
})()"""


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def check(url: str) -> str | None:
    """None si se puede abrir; si no, el motivo (en español)."""
    host = _host(url)
    if not host or urlparse(url).scheme not in ("http", "https"):
        return "Esa dirección no es una web."
    if any(b in host for b in settings["blocked_sites"]):
        return "Esa web está en tu lista de bloqueadas (Ajustes)."
    for s in settings["video_services"]:
        if any(host == d or host.endswith("." + d) for d in s["domains"]):
            return None if s.get("enabled", True) else f"{s['name']} está desactivado en Ajustes."
    return None


def site(url: str) -> str:
    """El dominio base (cuevana.biz, rtve.es…): los enlaces a otros dominios se tratan como anuncios."""
    return ".".join(_host(url).split(".")[-2:])


# ---------- iniciar sesión con la cuenta guardada (webaccounts) ----------
# rellena usuario y/o contraseña (también en los inicios de sesión de dos pasos: primero el correo, luego la
# contraseña) y pulsa entrar. Devuelve "password", "user" o null si en la página no hay nada que rellenar.
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


# ---------- buscar en cualquier web ----------
# escribe en la caja de búsqueda (si está escondida tras una lupa, pulsa la lupa); devuelve qué hizo
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

# envía la búsqueda: el formulario de la caja, o Enter
SUBMIT_JS = r"""(() => {
  const box = document.activeElement;
  if (!box || box.tagName !== 'INPUT') return false;
  const f = box.form;
  if (f) { (f.requestSubmit ? f.requestSubmit() : f.submit()); return true; }
  for (const t of ['keydown', 'keypress', 'keyup'])
    box.dispatchEvent(new KeyboardEvent(t, {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
  return true;
})()"""

# el primer resultado (en el orden de la página) que se parece a lo buscado, del mismo dominio y sin pinta de anuncio
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
    if (u.hostname.split('.').slice(-2).join('.') !== base) continue;   // a otro dominio: anuncio
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
    if (score === 1) break;   // el primero completo: ese
  }
  return bestScore >= 0.5 ? best : null;
})(%s, %s)"""


# el vídeo llena la página (o su iframe) cuando está dentro del panel
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
  // si ya ocupa casi toda la ventana (Netflix, Prime…) se deja como está: tocarlo descoloca su reproductor
  if (!best.hasAttribute('data-pepito-fill') && area > innerWidth * innerHeight * .8) return 'ya lleno';
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
    """Coordenadas en píxeles reales de pantalla (como las manda el panel)."""
    try:   # va por hilo, y cada orden llega en uno distinto
        _user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))   # por monitor v2
    except Exception:
        pass


def _listening(port: int) -> bool:
    try:
        return any(c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN
                   for c in psutil.net_connections("tcp4"))
    except psutil.Error:
        return True   # sin poder mirarlo: se intenta como antes


class WebPlayer:
    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._id = 0
        self._win: int | None = None   # la ventana de vídeo
        self._logins = 0   # intentos de iniciar sesión en la web abierta (para no insistir si la contraseña falla)
        self.wall: str | None = None   # web que ha pedido iniciar sesión al abrirla
        self.rect: dict | None = None   # recuadro del widget en pantalla (píxeles reales)
        self.docked = True              # dentro del panel; False = ventana normal (para iniciar sesión)

    # ---------- CDP ----------
    def _targets(self) -> list[dict]:
        # sin la ventana abierta, Windows tarda 1 s en rechazar la conexión: antes se mira si hay alguien
        # escuchando (3 ms). Esto frenaba 1 s cada orden de voz (se pregunta si hay un vídeo web puesto)
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
        """Ejecuta `body` sobre el vídeo principal (página o iframes). None si no hay vídeo."""
        targets = [t for t in self._targets() if t.get("type") in ("page", "iframe")]
        targets.sort(key=lambda t: t.get("type") != "page")
        for t in targets:
            try:
                value = self._eval(t, FIND % body)
            except Exception:
                continue
            if value is not None and not isinstance(value, str):   # alguna web (Pluto) devuelve texto raro
                return value
        # el reproductor en un marco de otra web (Dailymotion…): no sale en /json, se llega por la página
        for page in [t for t in targets if t.get("type") == "page"][:1]:
            try:
                for value in self._eval_subframes(page, FIND % body):
                    if value is not None and not isinstance(value, str):
                        return value
            except Exception:
                pass
        return None

    def _eval_subframes(self, page: dict, expression: str) -> list:
        """Evalúa en los marcos de otras webs de la página (CDP auto-attach), que /json no lista."""
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

            # marcos de otro dominio en el mismo proceso (geo.dailymotion.com dentro de www.): un «mundo aislado» en
            # cada uno comparte el DOM, así que se puede tocar su <video>; y los de otro proceso, por su sesión
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

    # ---------- órdenes ----------
    def _page(self) -> dict | None:
        pages = [t for t in self._targets() if t.get("type") == "page"]
        return pages[0] if pages else None

    def _kill_popups(self, seconds: float = 40):
        """Cierra las pestañas o ventanas nuevas que abra la web (anuncios al pulsar): se queda solo la original."""
        end = time.monotonic() + seconds
        first = None
        while time.monotonic() < end:
            pages = [t for t in self._targets() if t.get("type") == "page"]
            if pages and first is None:
                first = pages[-1]["id"]   # /json da primero la más reciente: la original es la última
            for t in pages:
                if first and t["id"] != first:
                    log.info("Cierro ventana emergente: %s", t.get("url", "")[:100])
                    try:
                        httpx.get(f"http://127.0.0.1:{PORT}/json/close/{t['id']}", timeout=1)
                    except httpx.HTTPError:
                        pass
            time.sleep(0.5)

    def search_site(self, url: str, query: str, wait: float = 30) -> str | None:
        """En una web cualquiera, con su portada ya abierta: busca «query» (su caja de búsqueda o, si no hay, las direcciones
        de búsqueda típicas) y devuelve la URL del primer resultado que no sea anuncio. None si no encuentra nada."""
        from urllib.parse import quote_plus
        base = site(url)
        root = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        q = quote_plus(query)
        guesses = [f"{root}/search?q={q}", f"{root}/?s={q}", f"{root}/buscar?q={q}", f"{root}/search/{q}"]
        # la portada ya la ha abierto quien llama (open)
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
                if box_done:   # ya se buscó: ¿hay resultados?
                    found = self._eval(page, PICK_JS % (enc(query), enc(base)))
                    if found:
                        return found
                    misses += 1
                    if misses < 3:   # los resultados de algunas webs tardan en pintarse
                        continue
                    misses = 0
                else:
                    did = self._eval(page, SEARCH_JS % enc(query))
                    if did == "icon" and icons < 2:
                        icons += 1
                        continue   # la lupa despliega la caja: se escribe en la siguiente vuelta
                    box_done = True
                    if did == "box":
                        time.sleep(0.3)
                        # Enter de verdad (por CDP): las webs hechas con React no hacen caso a uno simulado en JS
                        for kind in ("keyDown", "keyUp"):
                            self._call(page, "Input.dispatchKeyEvent", {"type": kind, "key": "Enter", "code": "Enter",
                                                                        "windowsVirtualKeyCode": 13,
                                                                        **({"text": "\r"} if kind == "keyDown" else {})})
                        time.sleep(1)
                        if site(self._page().get("url", "")) == base:
                            self._eval(page, SUBMIT_JS)   # por si el Enter no hizo nada: el formulario
                        time.sleep(1.5)
                        continue
                if guesses:   # sin caja, o la caja no dio nada: las direcciones de búsqueda más típicas
                    nxt = guesses.pop(0)
                    log.info("Busco en %s probando %s", base, nxt)
                    self._call(page, "Page.navigate", {"url": nxt})
                    time.sleep(2)
            except Exception as e:
                log.info("Buscar en %s: %s", base, e)
        return None

    def try_login(self, page: dict, back_to: str | None = None) -> bool:
        """Si la web pide cuenta y hay una guardada para ella, la rellena y entra (máx. 4 pasos por web abierta).
        Con back_to vuelve a esa dirección al acabar (muchas webs te dejan en su portada tras entrar)."""
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
        log.info("Inicio sesión en %s (%s)", host, "contraseña" if did == "password" else "usuario")
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
        """Plan B: «site:dominio búsqueda» en un buscador, dentro de la propia ventana, y el primer resultado."""
        from urllib.parse import quote_plus
        domain = site(url)
        page = self._page()
        if not page:
            return None
        for engine in video_sites.ENGINES:
            target = engine.format(q=quote_plus(f"site:{domain} {query}"))
            log.info("Busco en el buscador: %s", target)
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
            try:   # algunas webs tardan en soltar la página anterior: la navegación sigue aunque no conteste
                self._call(pages[0], "Page.navigate", {"url": url})
                self._call(pages[0], "Page.bringToFront")
            except TimeoutError:
                log.info("Edge tarda en cambiar de página")
        else:
            if EDGE is None:
                raise RuntimeError("No encuentro Edge")
            PROFILE.mkdir(parents=True, exist_ok=True)
            # en ventana (mitad derecha), no a pantalla completa: eso se pide aparte
            self._proc = subprocess.Popen([
                str(EDGE), f"--app={url}", f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}",
                "--no-first-run", "--autoplay-policy=no-user-gesture-required",
                # tapada por otras ventanas sigue «visible»: si no, RTVE deja de cargar el vídeo
                "--disable-features=Translate,CalculateNativeWinOcclusion", "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding", "--disable-background-timer-throttling",
                # sin DirectComposition Windows respeta el recorte de la ventana (si no, se vería la barra de título)
                "--disable-direct-composition",
                f"--host-rules={HOST_RULES}",
                "--window-position=940,90", "--window-size=960,600"])
        log.info("Web de vídeo: %s", url)
        threading.Thread(target=self._autoplay, args=(url,), daemon=True, name="webplayer-autoplay").start()
        threading.Thread(target=self._keep_docked, daemon=True, name="webplayer-dock").start()
        if not video_sites.steps(_host(url)):   # las webs sin receta (las desconocidas) suelen abrir anuncios
            threading.Thread(target=self._kill_popups, daemon=True, name="webplayer-popups").start()

    # ---------- dentro del panel ----------
    def _hwnd(self) -> int | None:
        """Ventana principal del Edge de vídeo (la más grande visible de su proceso)."""
        # metida en el panel ya no es de primer nivel (EnumWindows no la ve): se recuerda
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
        """La ventana del panel (Edge en modo app con título «Pepito»), que no sea la de vídeo."""
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def each(h, _):
            buf, cls = ctypes.create_unicode_buffer(64), ctypes.create_unicode_buffer(64)
            _user32.GetWindowTextW(ctypes.c_void_p(h), buf, 64)
            _user32.GetClassNameW(ctypes.c_void_p(h), cls, 64)
            if h != video and buf.value == "Pepito" and cls.value.startswith("Chrome_WidgetWin") \
                    and _user32.IsWindowVisible(ctypes.c_void_p(h)):
                found.append(h)
            return True

        _user32.EnumWindows(each, None)
        return found[0] if found else None

    def _set_child(self, h: ctypes.c_void_p, parent: int | None) -> None:
        """Mete la ventana de vídeo dentro del panel (parent) o la vuelve a soltar (None)."""
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
        """Coloca la página justo en el recuadro del widget, dentro de la ventana del panel y sin marco."""
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
            return False   # panel minimizado: al volver, el panel manda su recuadro y se coloca entonces
        if panel:
            self._set_child(h, panel)
        _user32.SetWindowRgn(h, None, False)   # medir sin recorte
        wr, cr, org = _RECT(), _RECT(), _POINT(0, 0)
        _user32.GetWindowRect(h, ctypes.byref(wr))
        _user32.GetClientRect(h, ctypes.byref(cr))
        _user32.ClientToScreen(h, ctypes.byref(org))
        title = max(0, round(cr.bottom - m[0]))          # barra de título (dentro del área de cliente)
        dx = org.x - wr.left                               # borde invisible de la izquierda
        dy = org.y - wr.top + title
        right = wr.right - (org.x + cr.right)
        bottom = wr.bottom - (org.y + cr.bottom)
        dy += 3   # el borde de arriba de la página también fuera
        x, y, w, hh = int(r["x"]), int(r["y"]), int(r["w"]), int(r["h"])
        try:   # sin esquinas redondeadas ni borde de Windows 11
            dwm = ctypes.windll.dwmapi
            dwm.DwmSetWindowAttribute(h, 33, ctypes.byref(ctypes.c_int(1)), 4)
            dwm.DwmSetWindowAttribute(h, 34, ctypes.byref(ctypes.c_uint(0xFFFFFFFE)), 4)
        except Exception:
            pass
        SWP_NOACTIVATE, SWP_SHOWWINDOW, SWP_NOZORDER, SWP_FRAMECHANGED = 0x0010, 0x0040, 0x0004, 0x0020
        pos = _POINT(x - dx, y - dy)
        if panel:   # dentro del panel las coordenadas van respecto a él
            _user32.ScreenToClient(ctypes.c_void_p(panel), ctypes.byref(pos))
            _user32.SetWindowPos(h, ctypes.c_void_p(0), pos.x, pos.y, w + dx + right, hh + dy + bottom,
                                 SWP_NOACTIVATE | SWP_SHOWWINDOW | SWP_FRAMECHANGED)
        else:   # sin panel a la vista (no debería pasar): como antes, encima
            _user32.SetWindowPos(h, ctypes.c_void_p(-1), pos.x, pos.y, w + dx + right, hh + dy + bottom,
                                 SWP_NOACTIVATE | SWP_SHOWWINDOW)
        rgn = _gdi32.CreateRectRgn(dx, dy, dx + w, dy + hh)
        _user32.SetWindowRgn(h, ctypes.c_void_p(rgn), True)   # Windows se queda con la región
        self._fill(True)
        return True

    def undock(self) -> bool:
        """Ventana normal y delante (para iniciar sesión o verlo a tu aire)."""
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
        """Fuera de la vista (p. ej. con el widget de Vídeos oculto): se queda sonando, sin verse."""
        hwnd = self._hwnd()
        if hwnd and self.docked:
            _user32.ShowWindow(ctypes.c_void_p(hwnd), 0)   # SW_HIDE; dock() la vuelve a enseñar

    def _fill(self, on: bool) -> None:
        for t in [t for t in self._targets() if t.get("type") in ("page", "iframe")]:
            try:
                self._eval(t, FILL_JS if on else UNFILL_JS)
            except Exception:
                pass

    def _keep_docked(self):
        """La ventana tarda en salir y las webs cambian de página: se recoloca un rato tras abrir."""
        for i in range(20):
            time.sleep(1.5)
            if self.wall:
                self.undock()
                return
            if self.docked and self.rect and self.active():
                self.dock()

    # primer resultado de la búsqueda de cada web con sesión → URL que lo reproduce
    PICK_JS = {
        "netflix": """(() => { const a = document.querySelector('[data-uia=gallery] [data-uia=standard-card], [data-uia=search-gallery-video-card] a');
            const id = a && (new URL(a.href, location.href).searchParams.get('jbv') || (a.href.match(/\\/(?:watch|title)\\/(\\d+)/) || [])[1]);
            return id ? 'https://www.netflix.com/watch/' + id : null; })()""",
        "prime": "(() => { const a = document.querySelector('a[href*=\"/detail/\"]'); return a ? a.href : null; })()",
        "disney": "(() => { const a = document.querySelector('a[href*=\"/browse/entity-\"]'); return a ? a.href : null; })()",
    }

    def pick_result(self, service: str, wait: float = 15) -> str | None:
        """Con la búsqueda abierta, espera a los resultados y devuelve la URL para ver el primero."""
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
        """Hasta 30 s: rechaza el aviso de cookies, pulsa el play de esa web (video_sites) o uno genérico, y
        si hay vídeo parado le da al play con sonido. Para cuando lo ve sonando dos veces seguidas."""
        site = lambda u: ".".join(_host(u).split(".")[-2:])   # rtve.es, pluto.tv…
        recipe = video_sites.steps(_host(url))
        if "/watch/" in urlparse(url).path:
            recipe = []   # Netflix arranca solo; su botón de play en el reproductor lo pausaría
        if urlparse(url).path.rstrip("/") in ("", "/browse", "/home", "/inicio", "/play"):
            return   # la portada de un servicio: se abre y eliges tú (si no, Netflix pone lo primero que vea)
        playing = clicked = stuck = 0
        for _ in range(30):
            time.sleep(1)
            pages = [t for t in self._targets() if t.get("type") == "page"]
            if not pages or site(pages[0].get("url", "")) != site(url):
                continue   # aún está la página anterior
            try:
                self._eval(pages[0], video_sites.REJECT_JS)
            except Exception:
                pass
            # solo hasta que suene: si luego Charlie lo pausa, se queda pausado
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
            if wall:   # pide cuenta: eso lo hace Charlie, no Pepito
                log.info("Autoplay: %s pide iniciar sesión (%s)", _host(url), wall)
                self.wall = _host(url)
                return
            # sin vídeo, o con vídeo que no arranca con play(): se pulsa el botón (no a la vez que play(), que lo pararía)
            if clicked < 3 and (state is None or stuck % 3 == 0):
                try:
                    hit = ((recipe and self._eval(pages[0], video_sites.click_js(recipe)))
                           or (state is None and stuck >= 4 and video_sites.generic_ok(_host(url))
                               and self._eval(pages[0], CLICK_PLAY)))
                except Exception:
                    hit = None
                if hit:
                    log.info("Autoplay: pulso %s", hit)
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
        if not isinstance(st, dict) or not st:   # webs raras devuelven texto, números… en vez del estado
            return {"playing": True, "source": "web", "docked": self.docked, "title": title,
                    "paused": True, "pos": None, "duration": None}
        # el título de la página, no el del reproductor («Dailymotion Video Player»)
        return {"playing": True, "source": "web", "docked": self.docked, **st, "title": title or st.get("title", "")}

    def close(self) -> None:
        try:
            ver = httpx.get(f"http://127.0.0.1:{PORT}/json/version", timeout=1).json()
            self._call({"webSocketDebuggerUrl": ver["webSocketDebuggerUrl"]}, "Browser.close")
        except Exception:
            if self._proc and self._proc.poll() is None:
                self._proc.kill()
        self._proc = None
