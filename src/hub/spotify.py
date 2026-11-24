"""Spotify with the user's personal account (Web API, development mode, Premium).

PKCE login: only the Spotify app's Client ID is needed (in .env, SPOTIFY_CLIENT_ID); the token is
stored in the program folder's \\secrets, never on Drive. By default it plays on the laptop's desktop Spotify
(through the Windows speaker); another Connect device can be chosen in Settings.
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
    """Error with a message to say out loud."""


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
        # one client for everything: reuses the connection and does not create the TLS context on every query (every 5 s)
        self._http = httpx.Client(timeout=10)
        self._playlists: list[dict] = []
        self._playlists_at = 0.0
        self._user_id = ""
        self._ducked_volume: int | None = None
        self._restored: tuple[int, int, float] | None = None   # (restored volume, the lowered one, when)
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
            raise SpotifyError("SPOTIFY_CLIENT_ID is missing from the .env")
        self._verifier = secrets.token_urlsafe(64)
        self._state = secrets.token_urlsafe(16)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self._verifier.encode()).digest()).rstrip(b"=").decode()
        return "https://accounts.spotify.com/authorize?" + urlencode({
            "response_type": "code", "client_id": self.client_id, "scope": SCOPES, "redirect_uri": self.redirect_uri,
            "code_challenge_method": "S256", "code_challenge": challenge, "state": self._state,
        })

    def finish_login(self, code: str, state: str) -> None:
        if not self._state or state != self._state:
            raise SpotifyError("Spotify's response does not match the login I started")
        self._token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
                             "client_id": self.client_id, "code_verifier": self._verifier})
        self._state = ""
        log.info("Spotify connected")

    def logout(self) -> None:
        self._token = {}
        TOKEN_FILE.unlink(missing_ok=True)

    def _token_request(self, data: dict) -> None:
        try:
            r = self._http.post("https://accounts.spotify.com/api/token", data=data)
        except httpx.HTTPError as e:
            raise SpotifyError("I cannot reach Spotify. Is there internet?") from e
        if r.status_code != 200:
            raise SpotifyError(f"Spotify will not let me in ({r.status_code})")
        token = r.json()
        token["expires_at"] = time.time() + token.get("expires_in", 3600) - 60
        if "refresh_token" not in token:
            token["refresh_token"] = self._token.get("refresh_token")
        self._token = token
        SECRETS_DIR.mkdir(exist_ok=True)
        TOKEN_FILE.write_text(json.dumps(token), encoding="utf-8")

    def _access_token(self) -> str:
        if not self.connected:
            raise SpotifyError("Spotify is not connected. Connect it in Settings.")
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
            raise SpotifyError("I cannot reach Spotify. Is there internet?") from e
        if r.status_code == 204 or not r.content:
            return None
        if r.status_code == 404 and "NO_ACTIVE_DEVICE" in r.text:
            raise SpotifyError("There is no active Spotify device.")
        if r.status_code == 403 and "PREMIUM" in r.text.upper():
            raise SpotifyError("Spotify says that needs Premium.")
        if r.status_code >= 400:
            log.warning("Spotify %s %s → %s %s", method, path, r.status_code, r.text[:200])
            raise SpotifyError("Spotify would not do it.")
        try:
            return r.json()
        except ValueError:
            return None   # pause/resume sometimes answer 200 with a non-JSON body

    def devices(self) -> list[dict]:
        return (self._api("GET", "/me/player/devices") or {}).get("devices", [])

    @staticmethod
    def _is_laptop(name: str) -> bool:
        host = socket.gethostname().lower()
        return name.lower() in (host, host[:15], os.environ.get("COMPUTERNAME", "").lower())

    def _find_device(self, wanted: str = "") -> str:
        """Id of the requested device (empty = the laptop); if it is the laptop and Spotify is not open, opens it."""
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
                raise SpotifyError(f"I cannot find the device {wanted} in Spotify.")
            if attempt == 0 and SPOTIFY_EXE.exists():
                log.info("Opening desktop Spotify")
                subprocess.Popen([str(SPOTIFY_EXE), "--minimized"], creationflags=subprocess.CREATE_NO_WINDOW)
            time.sleep(1.5)
        raise SpotifyError("Spotify will not start on the laptop. Open it by hand.")

    def _target_device(self) -> str:
        """Where to play new music: where it is already playing, otherwise the Settings device (or the laptop)."""
        state = self._api("GET", "/me/player") or {}
        if state.get("is_playing") and state.get("device", {}).get("id"):
            return state["device"]["id"]
        return self._find_device(settings["spotify_device"])

    def move_to(self, device: str) -> None:
        """Moves playback to that device. Echos sometimes accept the switch and do not start: keep trying."""
        self._api("PUT", "/me/player", json={"device_ids": [device], "play": True})
        for _ in range(3):
            time.sleep(1.5)
            a = self._api("GET", "/me/player") or {}
            if (a.get("device") or {}).get("id") == device and a.get("is_playing"):
                time.sleep(1.5)
                b = self._api("GET", "/me/player") or {}
                if (b.get("progress_ms") or 0) > (a.get("progress_ms") or 0):
                    return
            log.info("The device does not start; trying again")
            self._api("PUT", f"/me/player/play?device_id={device}")

    def transfer(self, where: str) -> str:
        """Moves the music to another device ("aquí", "el echo", "el pc"...). Returns its name."""
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
        return "the laptop" if self._is_laptop(name) else name

    def _play(self, body: dict) -> None:
        device = self._target_device()
        self._ducked_volume = None
        self._api("PUT", f"/me/player/play?device_id={device}", json=body)

    # ---------- commands ----------
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
            return "your Liked Songs"
        lists = self.playlists()
        names = {norm(p["name"]): p for p in lists}
        match = names.get(wanted)
        if not match:
            contains = [p for n, p in names.items() if wanted in n or (n and n in wanted)]
            close = difflib.get_close_matches(wanted, list(names), n=1, cutoff=0.55)
            match = contains[0] if contains else names[close[0]] if close else None
        if not match:
            raise SpotifyError(f"I cannot find any playlist of yours called {name}.")
        self._play({"context_uri": match["uri"]})
        return match["name"]

    def play_query(self, query: str, kind: str = "any") -> str:
        """Searches and plays: the artist if it clearly matches (or if \"música de\" was asked for), otherwise the song."""
        data = self._api("GET", "/search", params={"q": query, "type": "artist,track", "limit": 5})
        artists = data.get("artists", {}).get("items", [])
        tracks = data.get("tracks", {}).get("items", [])
        if artists and (kind == "artist" or norm(artists[0]["name"]) == norm(query)):
            self._play({"context_uri": artists[0]["uri"]})
            return artists[0]["name"]
        if tracks:
            t = tracks[0]
            self._play({"uris": [t["uri"]]})
            return f"{t['name']}, by {t['artists'][0]['name']}"
        if artists:
            self._play({"context_uri": artists[0]["uri"]})
            return artists[0]["name"]
        raise SpotifyError(f"I cannot find anything for {query} on Spotify.")

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
            raise SpotifyError("I do not know what volume Spotify is at right now.")
        return self.set_volume(current + delta)

    def state(self) -> dict:
        data = self._api("GET", "/me/player") or {}
        self.last_state = data
        return data

    def now_playing(self) -> str:
        item = self.state().get("item")
        if not item:
            return ""
        return f"{item['name']}, by {', '.join(a['name'] for a in item.get('artists', []))}"

    # ---------- lower the music while Cortex speaks ----------
    def duck(self) -> None:
        if self._ducked_volume is not None:
            return
        state = self.state()
        device = state.get("device") or {}
        volume = device.get("volume_percent")
        if not state.get("is_playing") or volume is None or not device.get("supports_volume", True):
            return
        # some speakers (Alexa) are slow to report the new volume: if it still shows the lowered one from a
        # moment ago, the real one is the one that was restored (otherwise each notice would sink the music a bit more)
        if self._restored and volume == self._restored[1] and time.monotonic() - self._restored[2] < 180:
            volume = self._restored[0]
        self._ducked_volume = volume
        self._api("PUT", f"/me/player/volume?volume_percent={max(10, volume // 2)}")   # halve it: so Cortex is heard without sinking the music

    def unduck(self) -> None:
        volume, self._ducked_volume = self._ducked_volume, None
        if volume is None:
            return
        for attempt in range(3):   # if it fails, the music would stay at half until someone raises it
            try:
                self._api("PUT", f"/me/player/volume?volume_percent={volume}")
                self._restored = (volume, max(10, volume // 2), time.monotonic())
                return
            except SpotifyError:
                if attempt == 2:
                    raise
                time.sleep(1)

    def summary(self) -> dict:
        """What the panel widget needs."""
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
