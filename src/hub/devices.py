"""Devices on the home network (mDNS): they are only discovered and shown; nothing is changed on them.

Music is sent to those that appear in Spotify Connect (the Echo, the PC...); that goes through spotify.py.
"""

import logging
import re
import threading
import time

from zeroconf import ServiceBrowser, Zeroconf

log = logging.getLogger("devices")

KINDS = {
    "_amzn-wplay._tcp.local.": ("Echo / Alexa", "speaker"),
    "_googlecast._tcp.local.": ("Chromecast / Google", "tv"),
    "_airplay._tcp.local.": ("AirPlay", "tv"),
    "_raop._tcp.local.": ("AirPlay", "speaker"),
    "_spotify-connect._tcp.local.": ("Spotify", "speaker"),
    "_hue._tcp.local.": ("Philips Hue", "light"),
    "_matterd._udp.local.": ("Matter (smart home)", "home"),
    "_matter._tcp.local.": ("Matter (smart home)", "home"),
    "_hap._tcp.local.": ("HomeKit", "home"),
    "_dosvc._tcp.local.": ("Windows PC", "pc"),
    "_mi-connect._udp.local.": ("Xiaomi", "phone"),
    "_sonos._tcp.local.": ("Sonos", "speaker"),
    "_printer._tcp.local.": ("Printer", "printer"),
    "_ipp._tcp.local.": ("Printer", "printer"),
}
SCAN_SECONDS = 8
EVERY = 600


class Devices:
    def __init__(self):
        self._lock = threading.Lock()
        self._found: dict[str, dict] = {}   # by IP
        self.scanned_at = 0.0
        threading.Thread(target=self._loop, daemon=True, name="devices").start()

    def _loop(self):
        while True:
            try:
                self.scan()
            except Exception:
                log.exception("Could not scan for devices on the network")
            time.sleep(EVERY)

    def scan(self) -> None:
        found: dict[str, dict] = {}
        zc = Zeroconf()

        class Listener:
            def add_service(self, z, kind, name):
                info = z.get_service_info(kind, name, timeout=2000)
                ips = [a for a in (info.parsed_addresses() if info else []) if "." in a]
                if not ips:
                    return
                label, icon = KINDS[kind]
                server = (info.server or "").removesuffix(".local.").removesuffix(".")
                short = name.removesuffix("." + kind)
                nice = short if len(short) < 32 and ":" not in short else (server if len(server) < 32 else label)
                if re.fullmatch(r"[0-9A-Fa-f-]{12,}", nice):   # unnamed identifiers (Matter, Amazon...)
                    nice = label
                d = found.setdefault(ips[0], {"ip": ips[0], "name": nice, "kinds": [], "icon": icon})
                if label not in d["kinds"]:
                    d["kinds"].append(label)
                if icon in ("speaker", "tv") and d["icon"] in ("home", "pc", "phone"):
                    d["icon"] = icon
                    d["name"] = nice if nice != short or ":" not in short else label

            def update_service(self, *a):
                pass

            def remove_service(self, *a):
                pass

        try:
            browsers = [ServiceBrowser(zc, kind, Listener()) for kind in KINDS]
            time.sleep(SCAN_SECONDS)
            for b in browsers:
                b.cancel()
        finally:
            zc.close()
        with self._lock:
            self._found = found
            self.scanned_at = time.time()
        log.info("Devices on the network: %s", ", ".join(f"{d['name']} ({d['ip']})" for d in found.values()) or "none")

    def list(self) -> list[dict]:
        with self._lock:
            return sorted(self._found.values(), key=lambda d: d["ip"])
