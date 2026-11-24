"""
gesture_detector.py
Converts normalized hand landmark positions into:
  • A smoothed, sensitivity-amplified cursor position
  • Pinch state (grab / release with hysteresis)
  • Fist state and a one-frame "just_opened" grow trigger
  • Peace-sign (✌) detection for system toggle

Fist → open-hand gesture:
  The cursor is frozen the moment a fist is detected so the user can aim
  before committing the gesture.  Opening the hand emits just_opened=True
  for exactly one frame using the frozen cursor position.
"""

import math
from . import config
from .hand_tracker import THUMB_TIP, INDEX_TIP

_FINGER_TIPS = [8,  12, 16, 20]
_FINGER_MCPS = [5,   9, 13, 17]
_WRIST = 0

_PALM_LANDMARKS = [0, 5, 9, 13, 17]

_FIST_RATIO = 1.25
_OPEN_RATIO = 1.50


class GestureDetector:
    def __init__(self):
        self._sensitivity    = config.SENSITIVITY
        self._alpha          = config.SMOOTH_ALPHA
        self._grab_thresh    = config.GRAB_THRESHOLD
        self._release_thresh = config.RELEASE_THRESHOLD

        self._is_pinching = False
        self._smooth_x: float | None = None
        self._smooth_y: float | None = None
        self._fist_state = "NEUTRAL"
        self._frozen_cursor: tuple[int, int] | None = None

    def update(
        self,
        positions: dict | None,
        win_w: int,
        win_h: int,
    ) -> tuple:
        """
        Returns:
            (cursor_pos, is_pinching, is_fist, just_opened, is_peace)
        """
        if positions is None:
            self._fist_state    = "NEUTRAL"
            self._frozen_cursor = None
            return None, False, False, False, False

        thumb = positions.get(THUMB_TIP)
        index = positions.get(INDEX_TIP)
        if thumb is None or index is None:
            return None, False, False, False, False

        # Raw pixel coords
        ix_raw = index[0] * win_w
        iy_raw = index[1] * win_h
        tx_raw = thumb[0] * win_w
        ty_raw = thumb[1] * win_h

        # Palm center — stable across all finger poses
        palm_pts = [positions[lm] for lm in _PALM_LANDMARKS if lm in positions]
        if palm_pts:
            px_raw = sum(p[0] for p in palm_pts) / len(palm_pts) * win_w
            py_raw = sum(p[1] for p in palm_pts) / len(palm_pts) * win_h
        else:
            px_raw, py_raw = ix_raw, iy_raw

        # Sensitivity-amplified cursor
        cx = win_w / 2.0
        cy = win_h / 2.0
        ix_amp = max(0.0, min(win_w - 1.0, cx + (px_raw - cx) * self._sensitivity))
        iy_amp = max(0.0, min(win_h - 1.0, cy + (py_raw - cy) * self._sensitivity))

        # EMA smoothing
        if self._smooth_x is None:
            self._smooth_x, self._smooth_y = ix_amp, iy_amp
        else:
            a = self._alpha
            self._smooth_x = a * ix_amp + (1 - a) * self._smooth_x
            self._smooth_y = a * iy_amp + (1 - a) * self._smooth_y

        cursor_pos = (int(self._smooth_x), int(self._smooth_y))

        # Pinch detection
        distance = math.hypot(ix_raw - tx_raw, iy_raw - ty_raw)
        if not self._is_pinching and distance < self._grab_thresh:
            self._is_pinching = True
        elif self._is_pinching and distance > self._release_thresh:
            self._is_pinching = False

        # Fist / open-hand state machine
        just_opened = False
        if not self._is_pinching:
            fist_now = self._detect_fist(positions)
            open_now = self._detect_open(positions)

            if self._fist_state == "NEUTRAL" and fist_now:
                self._fist_state    = "FIST"
                self._frozen_cursor = cursor_pos
            elif self._fist_state == "FIST" and open_now:
                just_opened         = True
                self._fist_state    = "NEUTRAL"
                cursor_pos          = self._frozen_cursor
                self._frozen_cursor = None
        else:
            self._fist_state    = "NEUTRAL"
            self._frozen_cursor = None

        display_cursor = (
            self._frozen_cursor
            if self._fist_state == "FIST" and self._frozen_cursor is not None
            else cursor_pos
        )

        # Peace sign — only when not pinching or fisting
        is_peace = (
            self._detect_peace(positions)
            if not self._is_pinching and self._fist_state == "NEUTRAL"
            else False
        )

        return display_cursor, self._is_pinching, self._fist_state == "FIST", just_opened, is_peace

    def reset(self):
        self._is_pinching   = False
        self._smooth_x      = None
        self._smooth_y      = None
        self._fist_state    = "NEUTRAL"
        self._frozen_cursor = None

    # ── Private helpers ───────────────────────────────────────────────────────

    def _curl_ratio(self, positions: dict, tip_idx: int, mcp_idx: int) -> float:
        wx, wy = positions[_WRIST]
        tx, ty = positions[tip_idx]
        mx, my = positions[mcp_idx]
        mcp_d  = math.hypot(mx - wx, my - wy)
        if mcp_d < 1e-6:
            return 1.0
        return math.hypot(tx - wx, ty - wy) / mcp_d

    def _detect_fist(self, positions: dict) -> bool:
        if _WRIST not in positions:
            return False
        return all(
            tip in positions and mcp in positions
            and self._curl_ratio(positions, tip, mcp) < _FIST_RATIO
            for tip, mcp in zip(_FINGER_TIPS, _FINGER_MCPS)
        )

    def _detect_open(self, positions: dict) -> bool:
        if _WRIST not in positions:
            return False
        extended = sum(
            1 for tip, mcp in zip(_FINGER_TIPS, _FINGER_MCPS)
            if tip in positions and mcp in positions
            and self._curl_ratio(positions, tip, mcp) > _OPEN_RATIO
        )
        return extended >= 3

    def _detect_peace(self, positions: dict) -> bool:
        """✌ — index + middle extended, ring + pinky curled."""
        if _WRIST not in positions:
            return False
        required = [8, 12, 16, 20, 5, 9, 13, 17]
        if any(k not in positions for k in required):
            return False
        index_ext  = self._curl_ratio(positions,  8,  5) > _OPEN_RATIO
        middle_ext = self._curl_ratio(positions, 12,  9) > _OPEN_RATIO
        ring_curl  = self._curl_ratio(positions, 16, 13) < _FIST_RATIO
        pinky_curl = self._curl_ratio(positions, 20, 17) < _FIST_RATIO
        return index_ext and middle_ext and ring_curl and pinky_curl
