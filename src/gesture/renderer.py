"""
renderer.py
Pygame-based renderer.  Handles multi-monitor spanning, circle drawing,
cursor, webcam preview, and a HUD showing FPS / pinch / fist / grow states.

Press D to toggle the monitor-boundary debug overlay.
"""

import os
import ctypes
from ctypes import wintypes

import cv2
import pygame

_BG_COLOR           = ( 18,  18,  28)
_CURSOR_IDLE        = (200, 200, 200)
_CURSOR_PINCH       = (255, 210,  30)
_CURSOR_FIST        = (255, 120,  40)
_HUD_DIM            = (100, 100, 110)
_FIST_COLOR         = (255, 140,  30)
_GROW_FLASH_COLOR   = ( 80, 255, 120)
_GROW_FLASH_FRAMES  = 28

_WEBCAM_PW = 240
_WEBCAM_PH = 135

# Monitor-overlay colours cycling per monitor
_MON_COLORS = [
    (80, 160, 255),   # blue
    (80, 255, 140),   # green
    (255, 180,  80),  # orange
    (220,  80, 220),  # purple
]


# ──────────────────────────────────────────────────────────────────────────────
# Monitor detection
# ──────────────────────────────────────────────────────────────────────────────

def _enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def detect_virtual_desktop() -> tuple[int, int, int, int]:
    """
    Returns (left, top, width, height) of the full Windows virtual desktop.
    Falls back to 1920×1080 on failure.
    """
    _enable_dpi_awareness()
    try:
        u32 = ctypes.windll.user32
        left   = u32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
        top    = u32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
        width  = u32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
        height = u32.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN
        if width > 0 and height > 0:
            print(f"Virtual desktop: {width}×{height}  origin ({left},{top})")
            return left, top, width, height
    except Exception as exc:
        print(f"Virtual desktop query failed ({exc}), using fallback 1920×1080")
    return 0, 0, 1920, 1080


def get_monitor_rects_in_canvas(virt_x: int, virt_y: int) -> list[dict]:
    """
    Enumerate physical monitors and return their rectangles converted into
    pygame canvas space (i.e. relative to the virtual-desktop origin).

    Each entry: { 'x', 'y', 'w', 'h', 'label' }
    """
    _enable_dpi_awareness()
    raw: list[tuple[int, int, int, int]] = []  # (left, top, right, bottom) in screen coords

    try:
        u32 = ctypes.windll.user32
        MonProc = ctypes.WINFUNCTYPE(
            ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.POINTER(wintypes.RECT), ctypes.c_double,
        )

        def _cb(hmon, hdc, rect, data):
            r = rect.contents
            raw.append((r.left, r.top, r.right, r.bottom))
            return 1

        u32.EnumDisplayMonitors(None, None, MonProc(_cb), 0)
    except Exception as exc:
        print(f"Monitor enumeration failed: {exc}")

    result = []
    for i, (l, t, r, b) in enumerate(sorted(raw, key=lambda m: m[0])):
        # Convert from absolute screen coords to pygame canvas coords
        cx = l - virt_x
        cy = t - virt_y
        cw = r - l
        ch = b - t
        label = f"Monitor {i}  {cw}×{ch}"
        result.append(dict(x=cx, y=cy, w=cw, h=ch, label=label))
        print(f"  {label}  canvas pos ({cx},{cy})")

    return result


# ──────────────────────────────────────────────────────────────────────────────
# Renderer
# ──────────────────────────────────────────────────────────────────────────────

class Renderer:
    def __init__(
        self,
        width: int,
        height: int,
        virt_x: int = 0,
        virt_y: int = 0,
        monitor_rects: list | None = None,
    ):
        # SDL must know the target position before pygame.init()
        os.environ["SDL_VIDEO_WINDOW_POS"] = f"{virt_x},{virt_y}"

        pygame.init()
        self.width  = width
        self.height = height

        # NOFRAME = borderless window that tiles across monitors seamlessly
        self.screen = pygame.display.set_mode((width, height), pygame.NOFRAME)
        pygame.display.set_caption("Hand Gesture Drag & Drop")

        self.clock    = pygame.time.Clock()
        self._font_sm = pygame.font.SysFont("consolas", 18)
        self._font_md = pygame.font.SysFont("consolas", 24, bold=True)
        self._font_lg = pygame.font.SysFont("consolas", 32, bold=True)

        self._grow_flash   = 0
        self._show_monitors = True          # toggle with D
        self._monitor_rects = monitor_rects or []

    # ── Main draw call ────────────────────────────────────────────────

    def draw_frame(
        self,
        circles,
        cursor_pos,
        is_pinching: bool,
        is_fist: bool,
        just_grew: bool,
        hand_detected: bool,
        webcam_frame=None,
    ) -> float:
        if just_grew:
            self._grow_flash = _GROW_FLASH_FRAMES

        self.screen.fill(_BG_COLOR)

        if self._show_monitors:
            self._draw_monitor_overlay()

        self._draw_circles(circles)
        self._draw_cursor(cursor_pos, is_pinching, is_fist)
        self._draw_hud(cursor_pos, is_pinching, is_fist, hand_detected)

        if webcam_frame is not None:
            self._draw_webcam_preview(webcam_frame)

        if self._grow_flash > 0:
            self._draw_grow_flash()
            self._grow_flash -= 1

        pygame.display.flip()
        self.clock.tick(60)
        return self.clock.get_fps()

    # ── Events ───────────────────────────────────────────────────────

    def handle_events(self) -> bool:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return False
            if event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_q, pygame.K_ESCAPE):
                    return False
                if event.key == pygame.K_d:
                    self._show_monitors = not self._show_monitors
        return True

    def quit(self):
        pygame.quit()

    # ── Monitor boundary overlay ──────────────────────────────────────

    def _draw_monitor_overlay(self):
        for i, mon in enumerate(self._monitor_rects):
            color = _MON_COLORS[i % len(_MON_COLORS)]
            mx, my, mw, mh = mon["x"], mon["y"], mon["w"], mon["h"]

            # Faint fill
            fill_surf = pygame.Surface((mw, mh), pygame.SRCALPHA)
            fill_surf.fill((*color, 18))
            self.screen.blit(fill_surf, (mx, my))

            # Border
            pygame.draw.rect(self.screen, (*color, 160), (mx, my, mw, mh), 2)

            # Vertical separator line (skip for last monitor)
            if i < len(self._monitor_rects) - 1:
                rx = mx + mw  # right edge of this monitor
                pygame.draw.line(self.screen, (*color, 100),
                                 (rx, 0), (rx, self.height), 1)

            # Label at top of each monitor area
            label_surf = self._font_sm.render(mon["label"], True, color)
            self.screen.blit(label_surf, (mx + 8, my + 8))

        # Hint text
        hint = self._font_sm.render("D = hide monitor outlines", True, _HUD_DIM)
        self.screen.blit(hint, (self.width // 2 - hint.get_width() // 2, 10))

    # ── Circle drawing ────────────────────────────────────────────────

    def _draw_circles(self, circles):
        for circle in circles:
            cx = int(circle.x)
            cy = int(circle.y)
            r  = max(1, int(circle.radius))
            base = circle.color

            if circle.is_dragged:
                glow_r = r + 22
                glow_surf = pygame.Surface((glow_r * 2, glow_r * 2), pygame.SRCALPHA)
                gc = (*[min(255, int(c * 1.2)) for c in base], 65)
                pygame.draw.circle(glow_surf, gc, (glow_r, glow_r), glow_r)
                self.screen.blit(glow_surf, (cx - glow_r, cy - glow_r))
                fill   = tuple(min(255, int(c * 1.35)) for c in base)
                border, bw = (255, 255, 255), 3
            else:
                fill   = base
                border, bw = (160, 160, 160), 2

            pygame.draw.circle(self.screen, fill,   (cx, cy), r)
            pygame.draw.circle(self.screen, border, (cx, cy), r, bw)

            # Specular dot
            hr = max(4, r // 5)
            pygame.draw.circle(self.screen, (255, 255, 255),
                               (cx - r // 3, cy - r // 3), hr)

    # ── Cursor ────────────────────────────────────────────────────────

    def _draw_cursor(self, cursor_pos, is_pinching: bool, is_fist: bool):
        if cursor_pos is None:
            return
        cx, cy = int(cursor_pos[0]), int(cursor_pos[1])

        if is_fist:
            color, ring_r = _CURSOR_FIST, 20
        elif is_pinching:
            color, ring_r = _CURSOR_PINCH, 18
        else:
            color, ring_r = _CURSOR_IDLE, 14

        pygame.draw.circle(self.screen, color, (cx, cy), ring_r, 2)
        pygame.draw.circle(self.screen, color, (cx, cy), 4)

        gap, arm = ring_r + 3, 10
        pygame.draw.line(self.screen, color, (cx - gap - arm, cy), (cx - gap, cy), 2)
        pygame.draw.line(self.screen, color, (cx + gap,       cy), (cx + gap + arm, cy), 2)
        pygame.draw.line(self.screen, color, (cx, cy - gap - arm), (cx, cy - gap), 2)
        pygame.draw.line(self.screen, color, (cx, cy + gap),       (cx, cy + gap + arm), 2)

        if is_fist:
            pygame.draw.circle(self.screen, _CURSOR_FIST, (cx, cy), ring_r + 8, 1)

    # ── HUD ───────────────────────────────────────────────────────────

    def _draw_hud(self, cursor_pos, is_pinching, is_fist, hand_detected):
        fps = self.clock.get_fps()
        fps_color = (80, 220, 80) if fps >= 20 else (220, 80, 80)
        self._blit(f"FPS: {fps:5.1f}", self._font_sm, fps_color, (10, 30))

        y = 54
        if not hand_detected:
            self._blit("No hand detected", self._font_md, (180, 60, 60), (10, y))
        elif is_fist:
            self._blit("FIST — open to GROW", self._font_md, _FIST_COLOR, (10, y))
        elif is_pinching:
            self._blit("PINCH: ON  (grab)", self._font_md, _CURSOR_PINCH, (10, y))
        else:
            self._blit("PINCH: OFF", self._font_md, _HUD_DIM, (10, y))

        if cursor_pos:
            self._blit(
                f"Cursor ({int(cursor_pos[0]):5d}, {int(cursor_pos[1]):4d})",
                self._font_sm, _HUD_DIM, (10, 82),
            )

        bx, by = 10, self.height - 50
        self._blit("Pinch=grab/drop  |  Fist then open=grow  |  D=toggle monitor view",
                   self._font_sm, _HUD_DIM, (bx, by))
        self._blit("Q / ESC to quit",
                   self._font_sm, _HUD_DIM, (bx, by + 22))

    def _draw_grow_flash(self):
        alpha = int(255 * self._grow_flash / _GROW_FLASH_FRAMES)
        text  = self._font_lg.render("GROW!", True, _GROW_FLASH_COLOR)
        text.set_alpha(alpha)
        rect  = text.get_rect(center=(self.width // 2, self.height // 2 - 60))
        self.screen.blit(text, rect)

    def _draw_webcam_preview(self, bgr_frame):
        pw, ph = _WEBCAM_PW, _WEBCAM_PH
        px = self.width - pw - 10
        py = 50
        flipped = cv2.flip(bgr_frame, 1)
        small   = cv2.resize(flipped, (pw, ph))
        rgb     = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
        surf    = pygame.surfarray.make_surface(rgb.transpose(1, 0, 2))
        self.screen.blit(surf, (px, py))
        pygame.draw.rect(self.screen, (80, 80, 80), (px, py, pw, ph), 1)
        self._blit("CAM", self._font_sm, _HUD_DIM, (px + 4, py + ph + 2))

    def _blit(self, text: str, font, color, pos):
        self.screen.blit(font.render(text, True, color), pos)
