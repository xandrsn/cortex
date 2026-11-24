"""A gesture's action → what happens on the PC.

Types: a Hub action (config.HUB_ACTIONS), "media:<key>", "keys:ctrl+shift+m", "run:<program or URL>",
"window:<minimize|maximize|close|desktop|switch>" or "none". Without Hub connected, the music ones go to the Windows
media keys (they work with Spotify, the browser, etc.).
"""

import logging
import os
import sys

from .config import HUB_ACTIONS

log = logging.getLogger("sense")

MEDIA = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2,
         "volume_up": 0xAF, "volume_down": 0xAE, "mute": 0xAD}
HUB_FALLBACK = {"next": "media:next", "previous": "media:previous", "toggle": "media:play_pause",
                "volume_up": "media:volume_up", "volume_down": "media:volume_down"}
NAMED = {"ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "enter": 0x0D, "esc": 0x1B,
         "escape": 0x1B, "tab": 0x09, "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
         "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "home": 0x24, "end": 0x23,
         "pageup": 0x21, "pagedown": 0x22, "printscreen": 0x2C}
EXTENDED = {0x2E, 0x25, 0x26, 0x27, 0x28, 0x24, 0x23, 0x21, 0x22, 0x5B} | set(MEDIA.values())
WINDOW = ["minimize", "maximize", "close", "desktop", "switch"]


def parse_keys(combo: str) -> list[int]:
    """"ctrl+shift+m" → Windows virtual-key codes. ValueError if any key does not exist."""
    vks = []
    for part in combo.lower().replace(" ", "").split("+"):
        if part in NAMED:
            vks.append(NAMED[part])
        elif len(part) == 1 and part.isalnum():
            vks.append(ord(part.upper()))
        elif part[:1] == "f" and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            vks.append(0x6F + int(part[1:]))
        else:
            raise ValueError(f"unknown key: {part!r}")
    return vks


def validate(action: str) -> str | None:
    """None if the action is valid; otherwise why not. The server checks it before saving a setting."""
    action = str(action)
    kind, _, arg = action.partition(":")
    if action in ("none", "") or action in HUB_ACTIONS:
        return None
    if kind == "media":
        return None if arg in MEDIA else f"unknown media key: {arg}"
    if kind == "window":
        return None if arg in WINDOW else f"unknown window action: {arg}"
    if kind == "run":
        return None if arg.strip() else "missing program"
    if kind == "keys":
        try:
            parse_keys(arg)
            return None
        except ValueError as e:
            return str(e)
    return f"unknown action: {action}"


def route(action: str, hub_connected: bool) -> str | None:
    """What Sense does with the action: "hub" (sends it to Hub), a local action, or None (nothing)."""
    if not action or action == "none":
        return None
    if action in HUB_ACTIONS:
        return "hub" if hub_connected else HUB_FALLBACK.get(action)
    return action


# ── Windows ──
def _press(vks: list[int]) -> None:
    import ctypes
    kb = ctypes.windll.user32.keybd_event
    for vk in vks:
        kb(vk, 0, 1 if vk in EXTENDED else 0, 0)
    for vk in reversed(vks):
        kb(vk, 0, (1 if vk in EXTENDED else 0) | 2, 0)   # 2 = KEYEVENTF_KEYUP


def _window(op: str) -> None:
    import ctypes
    u = ctypes.windll.user32
    if op == "desktop":
        return _press([0x5B, ord("D")])
    if op == "switch":
        return _press([0x12, 0x09])
    hwnd = u.GetForegroundWindow()
    if not hwnd:
        return
    if op == "close":
        u.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE: the app can still ask to save
    else:
        u.ShowWindow(hwnd, 6 if op == "minimize" else 3)   # SW_MINIMIZE / SW_MAXIMIZE


def run(action: str) -> bool:
    """Run a local action. False if it is not local or not valid."""
    if validate(action) or action in HUB_ACTIONS or action in ("none", ""):
        return False
    if sys.platform != "win32":
        log.warning("Local actions only work on Windows: %s", action)
        return False
    kind, _, arg = action.partition(":")
    if kind == "media":
        _press([MEDIA[arg]])
    elif kind == "keys":
        _press(parse_keys(arg))
    elif kind == "window":
        _window(arg)
    elif kind == "run":
        os.startfile(arg.strip())   # program, file or URL, like Windows Run
    log.info("Action %s", action)
    return True
