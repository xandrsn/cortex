"""
main.py — Always-on gesture window manager.

A transparent overlay covers all monitors.  It is completely invisible until
you make the peace sign (✌) and hold it for 0.6 s, which activates gesture
control.  Hold peace again to deactivate.

When ACTIVE:
  Pinch over a window            grab and drag it
  Release pinch                  snap to nearest half-zone (debounced 0.38 s)
  Pinch + quick swipe down       minimize (requires 0.5 s of grab first)
  Fist then open hand            fill the monitor (fullscreen, no taskbar overlap)
  Peace sign held 0.6 s          deactivate

When INACTIVE:
  Peace sign held 0.6 s          activate
  Everything else                invisible; no interaction

Controls
--------
  Q / ESC   quit
  D         (active) toggle zone-boundary overlay
"""

import os
import sys
import math
import time
import ctypes
import threading
from collections import deque

import cv2
import pygame

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from . import config
from . import win_api
from .hand_tracker     import HandTracker
from .gesture_detector import GestureDetector
from .renderer         import detect_virtual_desktop, get_monitor_rects_in_canvas
from .snap_zones       import build_zones, nearest_zone_id


# ── Overlay setup ─────────────────────────────────────────────────────────────

_CHROMA      = (255, 0, 255)
_CHROMA_CREF = (255 << 16) | 255   # 0xFF00FF  COLORREF = 0x00BBGGRR

_GWL_EXSTYLE        = -20
_WS_EX_LAYERED      = 0x00080000
_WS_EX_TRANSPARENT  = 0x00000020
_WS_EX_NOACTIVATE   = 0x08000000   # prevents overlay from stealing keyboard focus
_HWND_TOPMOST       = ctypes.c_void_p(-1)
_SWP_NOMOVE         = 0x0002
_SWP_NOSIZE         = 0x0001
_LWA_COLORKEY       = 0x1


def _make_overlay(hwnd: int) -> None:
    u32   = ctypes.windll.user32
    style = u32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
    u32.SetWindowLongW(hwnd, _GWL_EXSTYLE,
                       style | _WS_EX_LAYERED | _WS_EX_TRANSPARENT | _WS_EX_NOACTIVATE)
    u32.SetWindowPos(hwnd, _HWND_TOPMOST, 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE)
    u32.SetLayeredWindowAttributes(hwnd, _CHROMA_CREF, 0, _LWA_COLORKEY)


# ── Tuning ────────────────────────────────────────────────────────────────────

_RELEASE_DELAY   = 0.38   # s — pinch absent → drop & snap
_SWIPE_WINDOW    = 0.35   # s — velocity window sampled at pinch release
_SWIPE_THRESHOLD = 180    # px downward in _SWIPE_WINDOW at release → minimize
_MIN_GRAB_TIME   = 0.25   # s — must hold grab before throw-minimize can fire
_PEACE_HOLD      = 0.6    # s — hold peace sign to toggle
_PEACE_COOLDOWN  = 1.5    # s — minimum gap between toggles

_MON_COLORS = [(80, 160, 255), (80, 255, 140), (255, 180, 80)]


# ── Background threads ────────────────────────────────────────────────────────

_threads_alive = True
_cam_frame     = None
_cam_lock      = threading.Lock()
_win_list: list[win_api.WinInfo] = []
_win_lock      = threading.Lock()


def _cam_worker(cap: cv2.VideoCapture) -> None:
    global _cam_frame
    while _threads_alive:
        ret, frame = cap.read()
        if ret:
            with _cam_lock:
                _cam_frame = frame


def _win_worker(overlay_hwnd: int) -> None:
    global _win_list
    while _threads_alive:
        fresh = win_api.enumerate_windows(skip_hwnd=overlay_hwnd)
        with _win_lock:
            _win_list = fresh
        deadline = time.monotonic() + 0.4
        while _threads_alive and time.monotonic() < deadline:
            time.sleep(0.02)


# ── Drawing ───────────────────────────────────────────────────────────────────

def _draw_zones(screen: pygame.Surface, zones: dict, mon_rects: list,
                font: pygame.font.Font) -> None:
    for i, mon in enumerate(mon_rects[:3]):
        c  = _MON_COLORS[i % 3]
        mx, my, mw, mh = mon["x"], mon["y"], mon["w"], mon["h"]
        pygame.draw.rect(screen, c, (mx, my, mw, mh), 1)
        pygame.draw.line(screen, c, (mx + mw // 2, my), (mx + mw // 2, my + mh), 1)
    for zid, zone in zones.items():
        c = _MON_COLORS[zone.monitor_idx % 3]
        zcx, zcy = zone.center
        lbl = font.render(zid, True, c)
        screen.blit(lbl, (zcx - lbl.get_width() // 2, zcy - lbl.get_height() // 2))


def _draw_grabbed_highlight(screen: pygame.Surface,
                            windows: list[win_api.WinInfo],
                            grabbed_hwnd: int,
                            vx: int, vy: int,
                            font: pygame.font.Font) -> None:
    for win in windows:
        if win.hwnd == grabbed_hwnd:
            cx = win.x - vx
            cy = win.y - vy
            pygame.draw.rect(screen, (255, 210, 30), (cx, cy, win.w, win.h), 2)
            lbl = font.render(win.title[:44], True, (255, 210, 30))
            screen.blit(lbl, (cx + 5, cy + 5))
            break


def _draw_hand(screen: pygame.Surface, cx: int, cy: int, state: str) -> None:
    """state: 'idle' | 'pinch' | 'fist' | 'peace'"""
    if state == "fist":
        c = (255, 120, 40)
        pygame.draw.rect(screen, c, (cx - 14, cy - 8, 28, 8))
        pygame.draw.rect(screen, c, (cx - 14, cy - 8, 28, 20), 2)
        pygame.draw.line(screen, c, (cx - 14, cy), (cx - 22, cy - 8), 3)

    elif state == "pinch":
        c = (255, 210, 30)
        pygame.draw.circle(screen, c, (cx, cy + 4), 12, 2)
        pygame.draw.line(screen, c, (cx - 3, cy - 8), (cx - 7, cy - 28), 3)
        pygame.draw.line(screen, c, (cx + 3, cy - 8), (cx + 7, cy - 28), 3)
        pygame.draw.circle(screen, c, (cx - 7, cy - 28), 4, 2)
        pygame.draw.circle(screen, c, (cx + 7, cy - 28), 4, 2)

    elif state == "peace":
        c = (120, 220, 255)
        pygame.draw.ellipse(screen, c, (cx - 11, cy, 22, 20), 2)
        pygame.draw.line(screen, c, (cx - 4, cy),     (cx - 6, cy - 26), 2)
        pygame.draw.line(screen, c, (cx + 4, cy),     (cx + 6, cy - 26), 2)
        pygame.draw.line(screen, c, (cx + 8, cy + 2), (cx + 12, cy - 8), 2)
        pygame.draw.line(screen, c, (cx + 11, cy + 4),(cx + 16, cy - 4), 2)
        pygame.draw.line(screen, c, (cx - 11, cy + 8),(cx - 20, cy),     2)

    else:  # idle / open
        c = (220, 220, 220)
        pygame.draw.ellipse(screen, c, (cx - 11, cy - 2, 22, 22), 2)
        for dx, length in ((-8, 20), (-3, 24), (3, 24), (8, 20)):
            pygame.draw.line(screen, c, (cx + dx, cy - 2), (cx + dx, cy - 2 - length), 2)
        pygame.draw.line(screen, c, (cx - 11, cy + 6), (cx - 24, cy - 4), 2)


def _draw_peace_progress(screen: pygame.Surface, progress: float,
                         activating: bool) -> None:
    """Small arc in top-left corner while peace sign is being held."""
    r    = 18
    px, py = 28, 28
    c    = (120, 220, 255) if activating else (255, 140, 80)
    pygame.draw.circle(screen, (60, 60, 60), (px, py), r, 2)
    if progress > 0.02:
        angle = progress * 2 * math.pi
        rect  = pygame.Rect(px - r, py - r, r * 2, r * 2)
        pygame.draw.arc(screen, c, rect, math.pi / 2 - angle, math.pi / 2, 3)


_STATUS_SIZE = 16
_STATUS_MARGIN = 18


def _draw_status_square(screen: pygame.Surface, active: bool) -> None:
    """Always-visible square, top-right corner: red = inactive, green = active."""
    color = (40, 210, 70) if active else (220, 40, 40)
    x = screen.get_width() - _STATUS_MARGIN - _STATUS_SIZE
    y = _STATUS_MARGIN
    pygame.draw.rect(screen, color, (x, y, _STATUS_SIZE, _STATUS_SIZE))


# ── Camera debug window ───────────────────────────────────────────────────────

_DEBUG_WIN   = "Cortex - Hand Tracking"
_FINGERTIPS  = {4: (255, 0, 255), 8: (0, 255, 255), 12: (255, 255, 0)}


def _draw_debug_frame(frame, lm: dict | None, state_label: str, active: bool):
    """Mirrored camera frame with landmark dots + status text, for cv2.imshow."""
    dbg  = cv2.flip(frame, 1)
    h, w = dbg.shape[:2]
    if lm:
        for idx, (nx, ny) in lm.items():
            px, py = int(nx * w), int(ny * h)
            cv2.circle(dbg, (px, py), 3, (0, 255, 0), -1)
        for tip_idx, color in _FINGERTIPS.items():
            if tip_idx in lm:
                px, py = int(lm[tip_idx][0] * w), int(lm[tip_idx][1] * h)
                cv2.circle(dbg, (px, py), 9, color, 2)
    status_color = (0, 200, 0) if active else (0, 0, 220)
    cv2.putText(dbg, f"{'ACTIVE' if active else 'INACTIVE'} | {state_label}",
                (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.65, status_color, 2)
    return dbg


# ── Snap helpers ──────────────────────────────────────────────────────────────

def _snap_window(hwnd: int, zone_id: str,
                 zones: dict, work_rects: list[dict]) -> None:
    zone    = zones[zone_id]
    mon_idx = zone.monitor_idx
    if mon_idx >= len(work_rects):
        return
    wr   = work_rects[mon_idx]
    half = wr["w"] // 2
    if "LEFT" in zone_id:
        sx, sy, sw, sh = wr["x"], wr["y"], half, wr["h"]
    else:
        sx, sy, sw, sh = wr["x"] + half, wr["y"], wr["w"] - half, wr["h"]
    # Expand by invisible border so the visible area matches Win+Arrow exactly
    try:
        bl, bt, br, bb = win_api.get_invisible_border(hwnd)
    except Exception:
        bl, bt, br, bb = 0, 0, 0, 0
    win_api.set_rect(hwnd, sx - bl, sy - bt, sw + bl + br, sh + bt + bb)


def _fill_monitor(hwnd: int,
                  canvas_x: int, canvas_y: int,
                  mon_rects: list[dict],
                  work_rects: list[dict]) -> None:
    best_idx = min(
        range(len(mon_rects)),
        key=lambda i: math.hypot(
            canvas_x - (mon_rects[i]["x"] + mon_rects[i]["w"] // 2),
            canvas_y - (mon_rects[i]["y"] + mon_rects[i]["h"] // 2),
        ),
    )
    if best_idx < len(work_rects):
        wr = work_rects[best_idx]
        try:
            bl, bt, br, bb = win_api.get_invisible_border(hwnd)
        except Exception:
            bl, bt, br, bb = 0, 0, 0, 0
        win_api.set_rect(hwnd, wr["x"] - bl, wr["y"] - bt,
                         wr["w"] + bl + br, wr["h"] + bt + bb)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    global _threads_alive

    vx, vy, vw, vh = detect_virtual_desktop()
    win_w = config.WINDOW_WIDTH  or vw
    win_h = config.WINDOW_HEIGHT or vh
    mon_rects  = get_monitor_rects_in_canvas(vx, vy)
    zones      = build_zones(mon_rects)
    work_rects = win_api.get_monitor_work_rects()
    print(f"Desktop {win_w}×{win_h} at ({vx},{vy})")
    print(f"Work areas: {work_rects}")

    # Webcam
    cap = cv2.VideoCapture(config.WEBCAM_INDEX, cv2.CAP_DSHOW)
    if not cap.isOpened():
        cap = cv2.VideoCapture(config.WEBCAM_INDEX)
    if not cap.isOpened():
        sys.exit("ERROR: cannot open webcam.")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  config.WEBCAM_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.WEBCAM_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, 30)

    # Pygame overlay
    os.environ["SDL_VIDEO_WINDOW_POS"] = f"{vx},{vy}"
    pygame.init()
    pygame.mouse.set_visible(False)
    screen = pygame.display.set_mode((win_w, win_h), pygame.NOFRAME)
    pygame.display.set_caption("Cortex")
    clock = pygame.time.Clock()

    overlay_hwnd = pygame.display.get_wm_info()["window"]
    _make_overlay(overlay_hwnd)
    print(f"Overlay HWND {overlay_hwnd} — make ✌ and hold to activate.  Q=quit.")

    # Pre-create fonts once — SysFont() is slow; never call it in the render loop
    font_sm = pygame.font.SysFont("consolas", 15)

    # Hand tracking
    tracker = HandTracker(max_hands=1)
    gesture = GestureDetector()

    # Start background threads
    threading.Thread(target=_cam_worker, args=(cap,), daemon=True).start()
    threading.Thread(target=_win_worker, args=(overlay_hwnd,), daemon=True).start()

    # State
    active             = False
    peace_hold_start: float | None = None
    last_toggle_t      = 0.0
    show_zones         = False

    grabbed_hwnd: int           = 0
    grab_offset: tuple[int,int] = (0, 0)
    grab_start_t: float         = 0.0
    last_pinch_t: float         = 0.0
    drag_prev_x: int            = 0    # deadband: only call SetWindowPos when moved
    drag_prev_y: int            = 0

    vel_hist: deque[tuple[float, float]] = deque()
    prev_is_pinching:     bool = False
    release_vel_snapshot: list = []   # vel_hist snapshot taken at the frame pinch releases

    show_camera_debug = True
    print("Press C to toggle the camera debug window.")

    running = True
    while running:
        now = time.monotonic()

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_q, pygame.K_ESCAPE):
                    running = False
                elif event.key == pygame.K_d and active:
                    show_zones = not show_zones
                elif event.key == pygame.K_c:
                    show_camera_debug = not show_camera_debug
                    if not show_camera_debug:
                        try:
                            cv2.destroyWindow(_DEBUG_WIN)
                        except cv2.error:
                            pass

        # Latest camera frame (non-blocking)
        with _cam_lock:
            frame = _cam_frame

        # Gesture update
        raw  = tracker.process(frame) if frame is not None else None
        lm   = tracker.get_landmark_positions(raw)
        hand_detected = lm is not None

        cursor_pos, is_pinching, is_fist, just_opened, is_peace = gesture.update(
            lm, win_w, win_h)

        if show_camera_debug and frame is not None:
            if is_fist:
                state_label = "FIST"
            elif is_pinching:
                state_label = "PINCH"
            elif is_peace:
                state_label = "PEACE"
            elif hand_detected:
                state_label = "OPEN"
            else:
                state_label = "NO HAND"
            cv2.imshow(_DEBUG_WIN, _draw_debug_frame(frame, lm, state_label, active))
        if show_camera_debug:
            cv2.waitKey(1)

        if not hand_detected:
            gesture.reset()
            # If tracking dropped while grabbing, snap and release
            if grabbed_hwnd:
                try:
                    wx, wy, ww, wh = win_api.get_rect(grabbed_hwnd)
                    zone_id = nearest_zone_id(
                        wx + ww // 2 - vx, wy + wh // 2 - vy, zones)
                    _snap_window(grabbed_hwnd, zone_id, zones, work_rects)
                except Exception:
                    pass
                grabbed_hwnd         = 0
                vel_hist.clear()
                release_vel_snapshot = []

        if is_pinching and hand_detected:
            last_pinch_t = now

        # Peace sign toggle
        if is_peace and now - last_toggle_t > _PEACE_COOLDOWN:
            if peace_hold_start is None:
                peace_hold_start = now
            elif now - peace_hold_start >= _PEACE_HOLD:
                active        = not active
                last_toggle_t = now
                peace_hold_start = None
                if not active:
                    if grabbed_hwnd:
                        try:
                            wx, wy, ww, wh = win_api.get_rect(grabbed_hwnd)
                            zone_id = nearest_zone_id(
                                wx + ww // 2 - vx, wy + wh // 2 - vy, zones)
                            _snap_window(grabbed_hwnd, zone_id, zones, work_rects)
                        except Exception:
                            pass
                        grabbed_hwnd = 0
                    vel_hist.clear()
                    release_vel_snapshot = []
                    gesture.reset()
                    show_zones = False
                print("Cortex", "ACTIVE ✌" if active else "INACTIVE")
        else:
            if not is_peace:
                peace_hold_start = None

        # ── Window interaction (active only) ──────────────────────────────────
        if active:
            # Snapshot vel_hist the moment pinch releases while dragging a window.
            # This captures the throw velocity before the 0.38 s debounce elapses
            # and the hand potentially changes direction.
            if prev_is_pinching and not is_pinching and grabbed_hwnd:
                release_vel_snapshot = list(vel_hist)
            prev_is_pinching = is_pinching

            with _win_lock:
                windows = list(_win_list)

            if grabbed_hwnd and not win_api.is_valid(grabbed_hwnd):
                grabbed_hwnd = 0

            hovered_hwnd = 0
            if cursor_pos is not None:
                scx = cursor_pos[0] + vx
                scy = cursor_pos[1] + vy
                for win in windows:
                    if (win.x <= scx <= win.x + win.w
                            and win.y <= scy <= win.y + win.h):
                        hovered_hwnd = win.hwnd
                        break

            time_without_pinch = now - last_pinch_t

            # Grab
            if not grabbed_hwnd:
                if (is_pinching and hand_detected
                        and cursor_pos is not None and hovered_hwnd):
                    hwnd = hovered_hwnd
                    if win_api.is_maximized(hwnd):
                        zone_id = nearest_zone_id(
                            int(cursor_pos[0]), int(cursor_pos[1]), zones)
                        _snap_window(hwnd, zone_id, zones, work_rects)
                    try:
                        wx, wy, ww, wh = win_api.get_rect(hwnd)
                        grab_offset = (int(cursor_pos[0]) - (wx - vx),
                                       int(cursor_pos[1]) - (wy - vy))
                    except Exception:
                        grab_offset = (0, 0)
                    grabbed_hwnd         = hwnd
                    grab_start_t         = now
                    drag_prev_x          = 0
                    drag_prev_y          = 0
                    vel_hist.clear()
                    release_vel_snapshot = []
                    # No bring_to_front — avoids stealing keyboard focus from other apps

            else:
                # Drag
                if cursor_pos is not None and hand_detected:
                    new_x = int(cursor_pos[0]) - grab_offset[0]
                    new_y = int(cursor_pos[1]) - grab_offset[1]
                    # Only call SetWindowPos when position actually changed (reduces DWM pressure)
                    if new_x != drag_prev_x or new_y != drag_prev_y:
                        try:
                            wx, wy, ww, wh = win_api.get_rect(grabbed_hwnd)
                            win_api.set_rect(grabbed_hwnd,
                                             new_x + vx, new_y + vy, ww, wh)
                            drag_prev_x = new_x
                            drag_prev_y = new_y
                        except Exception:
                            grabbed_hwnd = 0

                    # Accumulate velocity history for throw-minimize detection
                    vel_hist.append((now, cursor_pos[1]))
                    while vel_hist and now - vel_hist[0][0] > _SWIPE_WINDOW:
                        vel_hist.popleft()

                # Release with debounce — check for throw-down minimize
                if grabbed_hwnd and time_without_pinch >= _RELEASE_DELAY:
                    minimized = False
                    if (len(release_vel_snapshot) >= 2
                            and now - grab_start_t >= _MIN_GRAB_TIME):
                        dy = release_vel_snapshot[-1][1] - release_vel_snapshot[0][1]
                        if dy > _SWIPE_THRESHOLD:
                            win_api.minimize(grabbed_hwnd)
                            minimized = True
                    if not minimized:
                        try:
                            wx, wy, ww, wh = win_api.get_rect(grabbed_hwnd)
                            zone_id = nearest_zone_id(
                                wx + ww // 2 - vx, wy + wh // 2 - vy, zones)
                            _snap_window(grabbed_hwnd, zone_id, zones, work_rects)
                        except Exception:
                            pass
                    grabbed_hwnd         = 0
                    grab_offset          = (0, 0)
                    vel_hist.clear()
                    release_vel_snapshot = []

            # Fist + open → fullscreen
            if just_opened and cursor_pos is not None and not grabbed_hwnd:
                if hovered_hwnd:
                    try:
                        _fill_monitor(hovered_hwnd,
                                      int(cursor_pos[0]), int(cursor_pos[1]),
                                      mon_rects, work_rects)
                    except Exception:
                        pass

        # ── Render ────────────────────────────────────────────────────────────
        screen.fill(_CHROMA)  # transparent when nothing else drawn

        if active:
            if show_zones:
                _draw_zones(screen, zones, mon_rects, font_sm)

            if grabbed_hwnd:
                with _win_lock:
                    _draw_grabbed_highlight(screen, list(_win_list), grabbed_hwnd, vx, vy, font_sm)

            if hand_detected and cursor_pos is not None:
                if is_fist:
                    state = "fist"
                elif grabbed_hwnd or is_pinching:
                    state = "pinch"
                elif is_peace:
                    state = "peace"
                else:
                    state = "idle"
                _draw_hand(screen, int(cursor_pos[0]), int(cursor_pos[1]), state)

        # Peace progress arc (shown in both active and inactive)
        if peace_hold_start is not None and now - last_toggle_t > _PEACE_COOLDOWN:
            progress = min(1.0, (now - peace_hold_start) / _PEACE_HOLD)
            _draw_peace_progress(screen, progress, not active)

        _draw_status_square(screen, active)

        pygame.display.flip()
        clock.tick(30 if active else 15)

    # Cleanup
    _threads_alive = False
    cap.release()
    tracker.close()
    cv2.destroyAllWindows()
    pygame.quit()
    print("Goodbye.")


if __name__ == "__main__":
    main()
