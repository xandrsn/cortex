"""Vídeos de YouTube con mpv + yt-dlp: sin anuncios, a pantalla completa y controlables por voz y gestos.

mpv se lanza como proceso aparte con un servidor IPC (tubería con nombre); cada orden abre la tubería,
manda una línea JSON y lee su respuesta.
"""

import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("video")

MPV = Path(r"C:\Program Files\MPV Player\mpv.exe")
YTDLP = Path(sys.executable).with_name("yt-dlp.exe")
PIPE = r"\\.\pipe\pepito-mpv"
# 1080p como mucho y sin AV1 (esta gráfica no lo decodifica: iría a tirones)
FORMAT = "bestvideo[height<=1080][vcodec!^=av01]+bestaudio/best[height<=1080]/best"


class VideoPlayer:
    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._req = 0
        self.title = ""
        self.video_id = ""
        self.rect: dict | None = None

    def _geometry(self) -> str:
        r = self.rect
        if not r or r.get("w", 0) < 80:
            return "50%-30-120"
        return f"{int(r['w'])}x{int(r['h'])}+{int(r['x'])}+{int(r['y'])}"

    def set_rect(self, rect: dict) -> None:
        """El recuadro del widget se ha movido o cambiado de tamaño: la ventana va detrás."""
        self.rect = rect
        if self.playing():
            self._send(["set_property", "geometry", self._geometry()])

    @property
    def available(self) -> bool:
        return MPV.exists()

    def playing(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def play(self, video_id: str, title: str = "", rect: dict | None = None) -> None:
        if not self.available:
            raise RuntimeError("Falta el reproductor mpv")
        self.stop()
        # sin bordes y encima del recuadro del widget (rect en píxeles de pantalla); si no hay, abajo a la derecha.
        # Pantalla completa con el botón o «ponlo en grande»
        self.rect = rect or self.rect
        args = [str(MPV), f"--geometry={self._geometry()}", "--ontop", "--no-border", "--force-window=immediate", "--keep-open=no", "--hwdec=auto-safe",
                f"--input-ipc-server={PIPE}", f"--ytdl-format={FORMAT}", "--volume=90", "--osd-level=1",
                f"--script-opts=ytdl_hook-ytdl_path={YTDLP}", f"--force-media-title={title or video_id}",
                f"https://www.youtube.com/watch?v={video_id}"]
        self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      creationflags=subprocess.CREATE_NO_WINDOW)
        self.title, self.video_id = title, video_id
        log.info("Vídeo: %s (%s)", title, video_id)

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            try:
                self._send(["quit"])
                proc.wait(2)
            except Exception:
                proc.kill()
        self.title = self.video_id = ""

    def _send(self, command: list):
        """Manda una orden a mpv y devuelve su `data` (None si falla)."""
        with self._lock:
            self._req += 1
            req = self._req
            try:
                with open(PIPE, "r+b", buffering=0) as pipe:
                    pipe.write((json.dumps({"command": command, "request_id": req}) + "\n").encode())
                    buf = b""
                    deadline = time.monotonic() + 1.5
                    while time.monotonic() < deadline:
                        chunk = pipe.read(4096)
                        if not chunk:
                            break
                        buf += chunk
                        for line in buf.split(b"\n"):
                            try:
                                msg = json.loads(line)
                            except ValueError:
                                continue
                            if msg.get("request_id") == req:
                                return msg.get("data")
            except OSError:
                return None
        return None

    def command(self, action: str, value: float | None = None) -> None:
        if not self.playing():
            return
        if action == "toggle":
            self._send(["cycle", "pause"])
        elif action == "pause":
            self._send(["set_property", "pause", True])
        elif action == "resume":
            self._send(["set_property", "pause", False])
        elif action == "seek":
            self._send(["seek", value or 10, "relative"])
        elif action == "volume":
            self._send(["add", "volume", value or 10])
        elif action == "fullscreen":
            self._send(["cycle", "fullscreen"] if value is None else ["set_property", "fullscreen", bool(value)])
        elif action == "stop":
            self.stop()

    def status(self) -> dict:
        if not self.playing():
            if self._proc is not None:   # se ha cerrado (fin del vídeo o la X de mpv)
                self._proc = None
                self.title = self.video_id = ""
            return {"playing": False}
        return {"playing": True, "title": self.title, "id": self.video_id,
                "paused": bool(self._send(["get_property", "pause"])),
                "fullscreen": bool(self._send(["get_property", "fullscreen"])),
                "pos": self._send(["get_property", "time-pos"]), "duration": self._send(["get_property", "duration"])}
