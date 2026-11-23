"""
win_api.py
Win32 helpers for enumerating and controlling real desktop windows.
"""

import ctypes
import ctypes.wintypes as wt
from dataclasses import dataclass

_u32     = ctypes.windll.user32
_dwmapi  = ctypes.windll.dwmapi

_DWMWA_EXTENDED_FRAME_BOUNDS = 9

_SW_MINIMIZE = 6
_SW_RESTORE  = 9
_SWP_FLAGS   = 0x0004 | 0x0010   # SWP_NOZORDER | SWP_NOACTIVATE
_GW_OWNER    = 4

_SKIP_TITLES = frozenset({
    "Program Manager",
    "Windows Shell Experience Host",
    "Windows Input Experience",
    "Microsoft Text Input Application",
    "Default IME",
    "MSCTFIME UI",
    "Task Switching",
    "Task View",
})

_WINFUNCTYPE = ctypes.WINFUNCTYPE


@dataclass
class WinInfo:
    hwnd:  int
    title: str
    x:     int
    y:     int
    w:     int
    h:     int


def enumerate_windows(skip_hwnd: int = 0) -> list[WinInfo]:
    """Return visible, non-minimized top-level windows in Z-order (topmost first)."""
    results: list[WinInfo] = []

    def _cb(hwnd, _):
        if hwnd == skip_hwnd:
            return True
        if not _u32.IsWindowVisible(hwnd):
            return True
        if _u32.IsIconic(hwnd):
            return True
        if _u32.GetWindow(hwnd, _GW_OWNER):
            return True
        buf = ctypes.create_unicode_buffer(256)
        _u32.GetWindowTextW(hwnd, buf, 256)
        title = buf.value.strip()
        if not title or title in _SKIP_TITLES:
            return True
        r = wt.RECT()
        _u32.GetWindowRect(hwnd, ctypes.byref(r))
        w = r.right  - r.left
        h = r.bottom - r.top
        if w < 80 or h < 80:
            return True
        results.append(WinInfo(hwnd, title, r.left, r.top, w, h))
        return True

    _cb_type = _WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    _u32.EnumWindows(_cb_type(_cb), 0)
    return results


def get_rect(hwnd: int) -> tuple[int, int, int, int]:
    r = wt.RECT()
    _u32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def set_rect(hwnd: int, x: int, y: int, w: int, h: int) -> None:
    """Move and resize; restores first if maximized."""
    if _u32.IsZoomed(hwnd):
        _u32.ShowWindow(hwnd, _SW_RESTORE)
    _u32.SetWindowPos(hwnd, 0, x, y, w, h, _SWP_FLAGS)


def minimize(hwnd: int) -> None:
    _u32.ShowWindow(hwnd, _SW_MINIMIZE)


def is_maximized(hwnd: int) -> bool:
    return bool(_u32.IsZoomed(hwnd))


def is_valid(hwnd: int) -> bool:
    return bool(_u32.IsWindow(hwnd))


def bring_to_front(hwnd: int) -> None:
    try:
        _u32.SetForegroundWindow(hwnd)
    except Exception:
        pass


# ── Work-area query (excludes taskbar) ───────────────────────────────────────

class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize",    wt.DWORD),
        ("rcMonitor", wt.RECT),
        ("rcWork",    wt.RECT),
        ("dwFlags",   wt.DWORD),
    ]


def get_invisible_border(hwnd: int) -> tuple[int, int, int, int]:
    """
    Returns (left, top, right, bottom) invisible border sizes in pixels.
    Modern Win32 apps have ~7px transparent resize borders on left/right/bottom.
    Using DWM extended frame bounds vs. GetWindowRect finds the difference.
    """
    wr = wt.RECT()
    _u32.GetWindowRect(hwnd, ctypes.byref(wr))
    ext = wt.RECT()
    try:
        _dwmapi.DwmGetWindowAttribute(
            hwnd, _DWMWA_EXTENDED_FRAME_BOUNDS,
            ctypes.byref(ext), ctypes.sizeof(ext))
        return (
            max(0, ext.left  - wr.left),
            max(0, ext.top   - wr.top),
            max(0, wr.right  - ext.right),
            max(0, wr.bottom - ext.bottom),
        )
    except Exception:
        return (0, 0, 0, 0)


def get_monitor_work_rects() -> list[dict]:
    """
    Return {x, y, w, h} work-area rects (excluding taskbar) for each monitor,
    in screen coordinates, sorted left-to-right.
    """
    result: list[dict] = []

    def _cb(hmon, hdc, prect, lparam):
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        _u32.GetMonitorInfoW(hmon, ctypes.byref(info))
        r = info.rcWork
        result.append({
            "x": r.left,
            "y": r.top,
            "w": r.right  - r.left,
            "h": r.bottom - r.top,
        })
        return True

    _MonitorEnumProc = _WINFUNCTYPE(
        ctypes.c_bool, wt.HMONITOR, wt.HDC,
        ctypes.POINTER(wt.RECT), wt.LPARAM,
    )
    _u32.EnumDisplayMonitors(None, None, _MonitorEnumProc(_cb), 0)
    result.sort(key=lambda m: m["x"])
    return result
