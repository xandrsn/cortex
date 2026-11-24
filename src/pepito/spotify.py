"""Spotify con la cuenta personal del usuario (Web API, modo desarrollo, Premium).

Login por PKCE: solo hace falta el Client ID de la app de Spotify (en .env, SPOTIFY_CLIENT_ID); el token
se guarda en C:la carpeta del programa\\secrets, nunca en Drive. Por defecto suena en el Spotify de escritorio del
portátil (sale por el altavoz de Windows); se puede elegir otro dispositivo Connect en Ajustes.
"""

import base64
import difflib
import hashlib
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .config import ROOT, settings

log = logging.getLogger("spotify")

SECRETS_DIR = ROOT / "secrets"
TOKEN_FILE = SECRETS_DIR / "spotify_token.json"
API = "https://api.spotify.com/v1"
SCOPES = ("user-read-playback-state user-modify-playback-state user-read-currently-playing "
          "playlist-read-private playlist-read-collaborative user-library-read")
SPOTIFY_EXE = Path(os.environ.get("APPDATA", "")) / "Spotify" / "Spotify.exe"
LIKED = {"me gusta", "tus me gusta", "canciones que me gustan", "mis me gusta", "liked songs", "liked", "favoritas",
         "mis favoritas"}


class SpotifyError(Exception):
    """Error con mensaje para decir en voz alta."""


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text)).strip()


class Spotify:
    def __init__(self, port: int):
        self.redirect_uri = f"http://127.0.0.1:{port}/spotify/callback"
        self._token: dict = {}
        self._verifier = ""
        self._state = ""
        self._lock = threading.Lock()
        # un cliente para todo: reutiliza la conexión y no crea el contexto TLS en cada consulta (cada 5 s)
        self._http = httpx.Client(timeout=10)
        self._playlists: list[dict] = []
        self._playlists_at = 0.0
        self._user_id = ""
        self._ducked_volume: int | None = None
        self._restored: tuple[int, int, float] | None = None   # (volumen devuelto, el bajado, cuándo)
        self.last_state: dict = {}
        if TOKEN_FILE.exists():
            self._token = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))

    # ---------- login ----------
    @property
    def client_id(self) -> str:
        return os.environ.get("SPOTIFY_CLIENT_ID", "").strip()

    @property
    def connected(self) -> bool:
        return bool(self._token.get("refresh_token"))

    def login_url(self) -> str:
        if not self.client_id:
            raise SpotifyError("Falta el SPOTIFY_CLIENT_ID en el .env")
        self._verifier = secrets.token_urlsafe(64)
        self._state = secrets.token_urlsafe(16)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self._verifier.encode()).digest()).rstrip(b"=").decode()
        return "https://accounts.spotify.com/authorize?" + urlencode({
            "response_type": "code", "client_id": self.client_id, "scope": SCOPES, "redirect_uri": self.redirect_uri,
            "code_challenge_method": "S256", "code_challenge": challenge, "state": self._state,
        })

    def finish_login(self, code: str, state: str) -> None:
        if not self._state or state != self._state:
            raise SpotifyError("La respuesta de Spotify no coincide con el login que empecé")
        self._token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
                             "client_id": self.client_id, "code_verifier": self._verifier})
        self._state = ""
        log.info("Spotify conectado")

    def logout(self) -> None:
        self._token = {}
        TOKEN_FILE.unlink(missing_ok=True)

    def _token_request(self, data: dict) -> None:
        try:
            r = self._http.post("https://accounts.spotify.com/api/token", data=data)
        except httpx.HTTPError as e:
            raise SpotifyError("No llego a Spotify, ¿hay internet?") from e
        if r.status_code != 200:
            raise SpotifyError(f"Spotify no me deja entrar ({r.status_code})")
        token = r.json()
        token["expires_at"] = time.time() + token.get("expires_in", 3600) - 60
        if "refresh_token" not in token:
            token["refresh_token"] = self._token.get("refresh_token")
        self._token = token
        SECRETS_DIR.mkdir(exist_ok=True)
        TOKEN_FILE.write_text(json.dumps(token), encoding="utf-8")

    def _access_token(self) -> str:
        if not self.connected:
            raise SpotifyError("Spotify no está conectado. Conéctalo en Ajustes.")
        if time.time() > self._token.get("expires_at", 0):
            self._token_request({"grant_type": "refresh_token", "refresh_token": self._token["refresh_token"],
                                 "client_id": self.client_id})
        return self._token["access_token"]

    # ---------- API ----------
    def _api(self, method: str, path: str, **kwargs):
        with self._lock:
            headers = {"Authorization": f"Bearer {self._access_token()}"}
        try:
            r = self._http.request(method, API + path, headers=headers, **kwargs)
            if r.status_code == 401:
                self._token["expires_at"] = 0
                with self._lock:
                    headers = {"Authorization": f"Bearer {self._access_token()}"}
                r = self._http.request(method, API + path, headers=headers, **kwargs)
        except httpx.HTTPError as e:
            raise SpotifyError("No llego a Spotify, ¿hay internet?") from e
        if r.status_code == 204 or not r.content:
            return None
        if r.status_code == 404 and "NO_ACTIVE_DEVICE" in r.text:
            raise SpotifyError("No hay ningún dispositivo de Spotify activo.")
        if r.status_code == 403 and "PREMIUM" in r.text.upper():
            raise SpotifyError("Spotify dice que eso necesita Premium.")
        if r.status_code >= 400:
            log.warning("Spotify %s %s → %s %s", method, path, r.status_code, r.text[:200])
            raise SpotifyError("Spotify no ha querido hacerlo.")
        try:
            return r.json()
        except ValueError:
            return None   # pausa/reanudar a veces responden 200 con un cuerpo que no es JSON

    def devices(self) -> list[dict]:
        return (self._api("GET", "/me/player/devices") or {}).get("devices", [])

    @staticmethod
    def _is_laptop(name: str) -> bool:
        host = socket.gethostname().lower()
        return name.lower() in (host, host[:15], os.environ.get("COMPUTERNAME", "").lower())

    def _find_device(self, wanted: str = "") -> str:
        """Id del dispositivo pedido (vacío = el portátil); si es el portátil y Spotify no está abierto, lo abre."""
        laptop = not wanted
        for attempt in range(12):
            devices = self.devices()
            for d in devices:
                if (laptop and self._is_laptop(d["name"])) or (not laptop and d["name"].lower() == wanted.lower()):
                    return d["id"]
            if not laptop and attempt == 0:
                names = {norm(d["name"]): d for d in devices}
                close = [d for n, d in names.items() if norm(wanted) in n]
                close = close or [names[n] for n in difflib.get_close_matches(norm(wanted), list(names), n=1, cutoff=0.5)]
                if close:
                    return close[0]["id"]
                raise SpotifyError(f"No encuentro el dispositivo {wanted} en Spotify.")
            if attempt == 0 and SPOTIFY_EXE.exists():
                log.info("Abriendo Spotify de escritorio")
                subprocess.Popen([str(SPOTIFY_EXE), "--minimized"], creationflags=subprocess.CREATE_NO_WINDOW)
            time.sleep(1.5)
        raise SpotifyError("Spotify no arranca en el portátil. Ábrelo a mano.")

    def _target_device(self) -> str:
        """Dónde poner música nueva: donde ya esté sonando, o si no el dispositivo de Ajustes (o el portátil)."""
        state = self._api("GET", "/me/player") or {}
        if state.get("is_playing") and state.get("device", {}).get("id"):
            return state["device"]["id"]
        return self._find_device(settings["spotify_device"])

    def move_to(self, device: str) -> None:
        """Pasa la reproducción a ese aparato. Los Echo a veces aceptan el cambio y no arrancan: se insiste."""
        self._api("PUT", "/me/player", json={"device_ids": [device], "play": True})
        for _ in range(3):
            time.sleep(1.5)
            a = self._api("GET", "/me/player") or {}
            if (a.get("device") or {}).get("id") == device and a.get("is_playing"):
                time.sleep(1.5)
                b = self._api("GET", "/me/player") or {}
                if (b.get("progress_ms") or 0) > (a.get("progress_ms") or 0):
                    return
            log.info("El aparato no arranca; insisto")
            self._api("PUT", f"/me/player/play?device_id={device}")

    def transfer(self, where: str) -> str:
        """Pasa la música a otro dispositivo («aquí», «el echo», «el pc»…). Devuelve su nombre."""
        w = norm(where)
        if re.fullmatch(r"(este (dispositivo|ordenador|portatil)|aqui|el portatil|portatil|ordenador|this (device|"
                        r"computer)|here|laptop)", w):
            target = ""
        elif re.search(r"echo|alexa|altavoz", w):
            echos = [d["name"] for d in self.devices() if "echo" in d["name"].lower()]
            target = echos[0] if echos else "echo"
        else:
            target = {"cocina": "kitchen"}.get(w, where)
        device = self._find_device(target)
        self.move_to(device)
        name = next((d["name"] for d in self.devices() if d["id"] == device), where)
        return "el portátil" if self._is_laptop(name) else name

    def _play(self, body: dict) -> None:
        device = self._target_device()
        self._ducked_volume = None
        self._api("PUT", f"/me/player/play?device_id={device}", json=body)

    # ---------- órdenes ----------
    def playlists(self) -> list[dict]:
        if time.time() - self._playlists_at > 600:
            items, url = [], "/me/playlists?limit=50"
            while url and len(items) < 500:
                page = self._api("GET", url)
                items += [p for p in page.get("items", []) if p]
                url = page.get("next", "").replace(API, "") if page.get("next") else ""
            self._playlists, self._playlists_at = items, time.time()
        return self._playlists

    def play_playlist(self, name: str) -> str:
        wanted = norm(name)
        if wanted in LIKED:
            if not self._user_id:
                self._user_id = self._api("GET", "/me")["id"]
            self._play({"context_uri": f"spotify:user:{self._user_id}:collection"})
            return "tus Me gusta"
        lists = self.playlists()
        names = {norm(p["name"]): p for p in lists}
        match = names.get(wanted)
        if not match:
            contains = [p for n, p in names.items() if wanted in n or (n and n in wanted)]
            close = difflib.get_close_matches(wanted, list(names), n=1, cutoff=0.55)
            match = contains[0] if contains else names[close[0]] if close else None
        if not match:
            raise SpotifyError(f"No encuentro ninguna playlist tuya que se llame {name}.")
        self._play({"context_uri": match["uri"]})
        return match["name"]

    def play_query(self, query: str, kind: str = "any") -> str:
        """Busca y pone: artista si coincide claramente (o si se pidió «música de»), si no la canción."""
        data = self._api("GET", "/search", params={"q": query, "type": "artist,track", "limit": 5})
        artists = data.get("artists", {}).get("items", [])
        tracks = data.get("tracks", {}).get("items", [])
        if artists and (kind == "artist" or norm(artists[0]["name"]) == norm(query)):
            self._play({"context_uri": artists[0]["uri"]})
            return artists[0]["name"]
        if tracks:
            t = tracks[0]
            self._play({"uris": [t["uri"]]})
            return f"{t['name']}, de {t['artists'][0]['name']}"
        if artists:
            self._play({"context_uri": artists[0]["uri"]})
            return artists[0]["name"]
        raise SpotifyError(f"No encuentro nada de {query} en Spotify.")

    def pause(self) -> None:
        self._api("PUT", "/me/player/pause")

    def resume(self) -> None:
        state = self.state()
        if state.get("device"):
            self._api("PUT", "/me/player/play")
        else:
            self._play({})

    def next(self) -> None:
        self._api("POST", "/me/player/next")

    def previous(self) -> None:
        self._api("POST", "/me/player/previous")

    def set_volume(self, percent: int) -> int:
        self._restored = None
        percent = max(0, min(100, int(percent)))
        self._ducked_volume = None
        self._api("PUT", f"/me/player/volume?volume_percent={percent}")
        return percent

    def change_volume(self, delta: int) -> int:
        current = (self.state().get("device") or {}).get("volume_percent")
        if current is None:
            raise SpotifyError("No sé a qué volumen está Spotify ahora.")
        return self.set_volume(current + delta)

    def state(self) -> dict:
        data = self._api("GET", "/me/player") or {}
        self.last_state = data
        return data

    def now_playing(self) -> str:
        item = self.state().get("item")
        if not item:
            return ""
        return f"{item['name']}, de {', '.join(a['name'] for a in item.get('artists', []))}"

    # ---------- bajar la música mientras habla Cortex ----------
    def duck(self) -> None:
        if self._ducked_volume is not None:
            return
        state = self.state()
        device = state.get("device") or {}
        volume = device.get("volume_percent")
        if not state.get("is_playing") or volume is None or not device.get("supports_volume", True):
            return
        # algunos altavoces (Alexa) tardan en informar del volumen nuevo: si aún dice el bajado de hace un
        # momento, el de verdad es el que se devolvió (si no, cada aviso hundiría la música un poco más)
        if self._restored and volume == self._restored[1] and time.monotonic() - self._restored[2] < 180:
            volume = self._restored[0]
        self._ducked_volume = volume
        self._api("PUT", f"/me/player/volume?volume_percent={max(10, volume // 2)}")   # a la mitad: que se oiga a Cortex sin hundir la música

    def unduck(self) -> None:
        volume, self._ducked_volume = self._ducked_volume, None
        if volume is None:
            return
        for attempt in range(3):   # si falla, la música se quedaría a la mitad hasta que alguien la suba
            try:
                self._api("PUT", f"/me/player/volume?volume_percent={volume}")
                self._restored = (volume, max(10, volume // 2), time.monotonic())
                return
            except SpotifyError:
                if attempt == 2:
                    raise
                time.sleep(1)

    def summary(self) -> dict:
        """Lo que necesita el widget del panel."""
        s = self.last_state
        item = s.get("item") or {}
        images = (item.get("album") or {}).get("images") or []
        return {
            "connected": self.connected,
            "client_id": bool(self.client_id),
            "playing": bool(s.get("is_playing")),
            "title": item.get("name", ""),
            "artist": ", ".join(a["name"] for a in item.get("artists", [])),
            "image": images[-2]["url"] if len(images) > 1 else images[0]["url"] if images else "",
            "device": (s.get("device") or {}).get("name", ""),
            "volume": self._ducked_volume if self._ducked_volume is not None else (s.get("device") or {}).get("volume_percent"),
            "progress": s.get("progress_ms") or 0,
            "duration": item.get("duration_ms") or 0,
        }
