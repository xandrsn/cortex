"""Camera: attention (is the user looking at the screen?) and hand gestures with MediaPipe.

A dedicated thread reads the webcam only while gestures are enabled:
- Face (FaceLandmarker, ~5 fps always): head yaw/pitch → "looking" → "engaged" with hysteresis.
- Hand (GestureRecognizer, ~15 fps only when engaged or looking is not required): static gestures
  (MediaPipe's own and the user's recorded ones, k-NN) and swipes from the trajectory
  of the palm centre.
It does not run actions: it reports via on_gesture(gesture, action) and on_presence(engaged) from its thread.
"""

import json
import logging
import math
import re
import threading
import time
import urllib.request
from collections import Counter, deque
from datetime import datetime
from typing import Callable

import cv2
import numpy as np

from .config import DEFAULTS, MODELS_DIR, ROOT, settings

# mediapipe is imported lazily (fast startup), but from one thread at a time: when the face thread and the
# model-loading thread imported it simultaneously, Python raised _DeadlockError and the camera went blind until restart
_MP_LOCK = threading.Lock()


def _mediapipe():
    with _MP_LOCK:
        import mediapipe as mp
        from mediapipe.tasks.python import vision as mpv
        from mediapipe.tasks.python.core.base_options import BaseOptions
    return mp, mpv, BaseOptions

log = logging.getLogger("vision")

MP_DIR = MODELS_DIR / "mediapipe"
MODEL_URLS = {
    "gesture_recognizer.task": "https://storage.googleapis.com/mediapipe-models/gesture_recognizer/"
                               "gesture_recognizer/float16/latest/gesture_recognizer.task",
    "face_landmarker.task": "https://storage.googleapis.com/mediapipe-models/face_landmarker/"
                            "face_landmarker/float16/latest/face_landmarker.task",
}
CUSTOM_FILE = ROOT / "gestures" / "custom.json"

W, H = 640, 480
FACE_PERIOD = 0.2            # ~5 fps
MAX_CAM_FPS = 32            # camera fps cap (requested by the owner)
HAND_PERIOD = 1 / 30         # hand in view: whatever the CPU allows (~15-20 fps)
SEARCH_PERIOD = 1 / 12       # searching for a hand (palm detection is the most expensive part)
CANNED = {"Thumb_Up": "thumb_up", "Thumb_Down": "thumb_down", "Open_Palm": "open_palm",
          "Closed_Fist": "closed_fist", "Victory": "victory", "Pointing_Up": "pointing_up",
          "ILoveYou": "i_love_you"}
EXTRA_POSES = ["ok", "shaka", "rock", "three", "shush"]
MOTIONS = ["swipe_left", "swipe_right", "swipe_up", "swipe_down", "flick_right", "flick_left", "wag",
           "circle_cw", "circle_ccw", "wave", "push"]
TWO_HANDS = ["both_palms", "both_thumbs_up", "both_thumbs_down", "both_fists", "hands_apart", "hands_together"]
GESTURES = list(CANNED.values()) + EXTRA_POSES + MOTIONS + TWO_HANDS
PALM = [0, 5, 9, 13, 17]
REPEATABLE = {"volume_up", "volume_down", "media:volume_up", "media:volume_down"}   # repeat while the gesture is held
STATIC_AFTER_SWIPE = 1.2     # s without static gestures after a swipe (the hand is still open)
EXTRA_HOLD = {"open_palm": 0.25}   # an open palm usually precedes a swipe
HAND_LINKS = [(0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8), (5, 9), (9, 10), (10, 11),
              (11, 12), (9, 13), (13, 14), (14, 15), (15, 16), (13, 17), (0, 17), (17, 18), (18, 19), (19, 20)]


def cfg(key):
    """Live setting; if the process started with an old config.py, the default value."""
    try:
        return settings[key]
    except KeyError:
        return DEFAULTS[key]


# ── Pure parts (tested in tools/vision_unit.py) ─────────────────────────────────────────────

def normalize(points, left: bool = False) -> np.ndarray | None:
    """21 points (x, y in proportional units, e.g. pixels) → 42-vector invariant to position,
    scale, in-plane rotation and handedness (left hands are mirrored to right hands)."""
    p = np.asarray(points, dtype=float)[:21, :2].copy()
    if left:
        p[:, 0] = -p[:, 0]
    z = (p[:, 0] - p[0, 0]) + 1j * (p[:, 1] - p[0, 1])     # relative to the wrist
    ref = z[9]                                               # wrist → middle knuckle
    s = abs(ref)
    if s < 1e-6:
        return None
    z = z * (-1j / (ref / s)) / s                            # point up (negative y) and have length 1
    return np.stack([z.real, z.imag], axis=1).ravel()


def hand_distance(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Mean per-point distance (in palm widths) between a (42,) and b (n, 42)."""
    d = np.asarray(b).reshape(-1, 21, 2) - np.asarray(a).reshape(1, 21, 2)
    return np.linalg.norm(d, axis=2).mean(axis=1)


class CustomClassifier:
    """k-NN over normalized poses with a distance threshold so that not every hand matches."""

    def __init__(self, k: int = 5):
        self.k = k
        self.names: list[str] = []
        self.X = np.zeros((0, 42))

    def fit(self, gestures: dict[str, list]) -> None:
        names, rows = [], []
        for name, samples in gestures.items():
            for v in samples:
                names.append(name)
                rows.append(np.asarray(v, dtype=float))
        self.names = names
        self.X = np.array(rows) if rows else np.zeros((0, 42))

    def classify(self, vec, threshold: float) -> tuple[str | None, float]:
        if vec is None or not self.names:
            return None, math.inf
        d = hand_distance(vec, self.X)
        idx = np.argsort(d)[: self.k]
        votes = Counter(self.names[i] for i in idx)
        best = min(votes, key=lambda n: (-votes[n], np.mean([d[i] for i in idx if self.names[i] == n])))
        dist = float(np.mean([d[i] for i in idx if self.names[i] == best]))
        if votes[best] * 2 < len(idx) and len(idx) > 1:    # no clear majority
            return None, dist
        return (best, dist) if dist <= threshold else (None, dist)


class SwipeDetector:
    """Swipes from the palm-centre trajectory (x already mirrored, 0..1)."""

    def __init__(self, min_dist=0.22, min_speed=0.6, min_dt=0.1, max_dt=0.6, axis_ratio=0.5,
                 straightness=0.75, gap=0.25, refractory=1.2, move_speed=0.5):
        self.min_dist, self.min_speed = min_dist, min_speed
        self.min_dt, self.max_dt = min_dt, max_dt
        self.axis_ratio, self.straightness = axis_ratio, straightness
        self.gap, self.refractory, self.move_speed = gap, refractory, move_speed
        self.hist: deque = deque()
        self.last_swipe = -math.inf

    def reset(self):
        self.hist.clear()

    def update(self, t: float, x: float | None = None, y: float | None = None) -> str | None:
        if self.hist and t - self.hist[-1][0] > self.gap:      # hand lost: new trajectory
            self.hist.clear()
        if x is None:
            return None
        self.hist.append((t, x, y))
        while self.hist and t - self.hist[0][0] > self.max_dt + 0.1:
            self.hist.popleft()
        if t - self.last_swipe < self.refractory:
            return None
        pts = list(self.hist)
        for i, (t0, x0, y0) in enumerate(pts):
            dt = t - t0
            if dt > self.max_dt:
                continue
            if dt < self.min_dt:
                break
            dx, dy = x - x0, y - y0
            horiz = abs(dx) >= abs(dy)
            major, minor = (dx, dy) if horiz else (dy, dx)
            if abs(major) < self.min_dist or abs(major) / dt < self.min_speed:
                continue
            if abs(minor) > self.axis_ratio * abs(major):
                continue
            k = 1 if horiz else 2
            steps = [abs(pts[j + 1][k] - pts[j][k]) for j in range(i, len(pts) - 1)]
            if len(steps) < 2 or max(steps) > 0.6 * abs(major):   # jumps (detection switched hands)
                continue
            if abs(major) < self.straightness * sum(steps):         # back and forth, tremor
                continue
            self.last_swipe = t
            self.hist.clear()
            if horiz:
                return "swipe_right" if dx > 0 else "swipe_left"
            return "swipe_down" if dy > 0 else "swipe_up"
        return None

    def speed(self, t: float, window: float = 0.2) -> float:
        pts = [p for p in self.hist if t - p[0] <= window]
        if len(pts) < 2 or pts[-1][0] - pts[0][0] < 1e-3:
            return 0.0
        return math.hypot(pts[-1][1] - pts[0][1], pts[-1][2] - pts[0][2]) / (pts[-1][0] - pts[0][0])

    def moving(self, t: float) -> bool:
        return self.speed(t) >= self.move_speed


class Hold:
    """A static gesture counts once it has been held for a while."""

    def __init__(self):
        self.label: str | None = None
        self.since = 0.0

    def update(self, label: str | None, t: float, hold_s: float) -> str | None:
        if label != self.label:
            self.label, self.since = label, t
        if label and t - self.since >= hold_s + EXTRA_HOLD.get(label, 0.0):
            return label
        return None


class Gate:
    """Cooldown: nothing for `cooldown` after a gesture; the same gesture does not repeat until the hand
    changes pose (≥ change_s) or, if it is repeatable (volume), `repeat_s` pass while holding it."""

    def __init__(self, cooldown=1.0, repeat_s=1.5, change_s=0.2):
        self.cooldown, self.repeat_s, self.change_s = cooldown, repeat_s, change_s
        self.last_gid: str | None = None
        self.last_t = -math.inf
        self.changed = True
        self._diff_since: float | None = None

    def observe(self, label: str | None, t: float) -> None:
        if self.last_gid is None or self.changed:
            return
        if label != self.last_gid:
            if self._diff_since is None:
                self._diff_since = t
            if t - self._diff_since >= self.change_s:
                self.changed = True
        else:
            self._diff_since = None

    def allow(self, gid: str, t: float, repeat: bool = True) -> bool:
        if t - self.last_t < self.cooldown:
            return False
        if gid != self.last_gid or self.changed:
            return True
        # holding the palm must not pause and resume every 1.5 s; the thumb does keep raising the volume
        return repeat and t - self.last_t >= self.repeat_s

    def fire(self, gid: str, t: float) -> None:
        self.last_gid, self.last_t = gid, t
        self.changed, self._diff_since = False, None


class Attention:
    """"Engaged" after looking for engage_s in a row; stops being engaged after release_s without looking."""

    def __init__(self, engage_s=0.4, release_s=2.0):
        self.engage_s, self.release_s = engage_s, release_s
        self.engaged = False
        self._since: float | None = None
        self._last = -math.inf

    def update(self, looking: bool, t: float) -> bool | None:
        if looking:
            if self._since is None:
                self._since = t
            self._last = t
            if not self.engaged and t - self._since >= self.engage_s:
                self.engaged = True
                return True
        else:
            self._since = None
            if self.engaged and t - self._last >= self.release_s:
                self.engaged = False
                return False
        return None


# ── Geometric poses (in addition to MediaPipe's) ──
TIPS, PIPS = (8, 12, 16, 20), (6, 10, 14, 18)


def fingers(pts) -> dict:
    """Which fingers are extended? pts: 21 points (x, y) in pixels. Also the thumb-index pinch."""
    p = np.asarray(pts, dtype=float)[:21, :2]
    palm = np.linalg.norm(p[9] - p[0]) + 1e-6
    ext = {}
    for name, tip, pip in zip(("index", "middle", "ring", "pinky"), TIPS, PIPS):
        ext[name] = np.linalg.norm(p[tip] - p[0]) > 1.15 * np.linalg.norm(p[pip] - p[0])
    ext["thumb"] = np.linalg.norm(p[4] - p[9]) / palm > 0.75 and np.linalg.norm(p[4] - p[5]) / palm > 0.45
    ext["pinch"] = np.linalg.norm(p[4] - p[8]) / palm < 0.28
    ext["scale"] = palm
    return ext


def pose(f: dict) -> str | None:
    """Extra poses from fingers(): ok, shaka, rock, three, pointing (internal), open (internal)."""
    i, m, r, k, t = f["index"], f["middle"], f["ring"], f["pinky"], f["thumb"]
    if f["pinch"] and m and r and k:
        return "ok"
    if t and k and not (i or m or r):
        return "shaka"
    if i and k and not (m or r or t):
        return "rock"
    if i and m and r and not k:
        return "three"
    if i and not (m or r or k):
        return "pointing"
    if i and m and r and k:
        return "open"
    return None


def shush_pose(pts, mouth, face_w: float) -> bool:
    """Vertical index finger across the lips (the "shush" gesture). pts in pixels; mouth (x, y) in pixels."""
    p = np.asarray(pts, dtype=float)[:21, :2]
    tip, pip = p[8], p[6]
    if pip[1] - tip[1] < abs(tip[0] - pip[0]):           # the finger points up
        return False
    seg = [p[6], p[7], p[8], (p[7] + p[8]) / 2, (p[6] + p[7]) / 2]
    return min(np.linalg.norm(q - np.asarray(mouth)) for q in seg) < 0.3 * face_w


def reversals(values, amp: float) -> int:
    """How many times a series reverses direction, counting only back-and-forth moves of at least `amp`."""
    count, direction, ext = 0, 0, None
    for v in values:
        if ext is None:
            ext = v
            continue
        if direction >= 0 and v < ext - amp:
            count += direction > 0
            direction, ext = -1, v
        elif direction <= 0 and v > ext + amp:
            count += direction < 0
            direction, ext = 1, v
        elif (direction > 0 and v > ext) or (direction < 0 and v < ext):
            ext = v
    return count


class MotionDetector:
    """Motion gestures of one hand (0..1 coordinates as the user sees them, x mirrored):
    - flick_right / flick_left: quick index-finger flick to one side ("next / previous")
    - wag: index finger side to side ("no")
    - circle_cw / circle_ccw: circle with the index finger (clockwise / counter-clockwise)
    - wave: open hand waving
    - push: open hand pushing toward the camera
    """

    def __init__(self, refractory: float = 1.0):
        self.hist: deque = deque()   # (t, tip_x, tip_y, palm_x, palm_y, scale, pose)
        self.refractory = refractory
        self.last = -math.inf

    def reset(self):
        self.hist.clear()

    def update(self, t, tip, palm, scale, kind) -> str | None:
        if self.hist and t - self.hist[-1][0] > 0.3:
            self.hist.clear()
        self.hist.append((t, tip[0], tip[1], palm[0], palm[1], scale, kind))
        while self.hist and t - self.hist[0][0] > 1.6:
            self.hist.popleft()
        if t - self.last < self.refractory or len(self.hist) < 4:
            return None
        g = self._detect(t)
        if g:
            self.last = t
            self.hist.clear()
        return g

    def _share(self, pts, kind) -> float:
        return sum(p[6] == kind for p in pts) / max(len(pts), 1)

    def _detect(self, t) -> str | None:
        pts = list(self.hist)
        recent = [p for p in pts if t - p[0] <= 0.3]
        # index flick: the finger moves fast to one side and the palm much less
        if len(recent) >= 3 and self._share(recent, "pointing") >= 0.6:
            dx = recent[-1][1] - recent[0][1]
            dy = recent[-1][2] - recent[0][2]
            pdx = recent[-1][3] - recent[0][3]
            dt = recent[-1][0] - recent[0][0]
            if abs(dx) >= 0.09 and abs(dx) > 2 * abs(dy) and dt > 0.05 and abs(dx) / dt >= 0.3 \
                    and abs(pdx) < 0.8 * abs(dx) + 0.04:
                xs = [p[1] for p in recent]
                if reversals(xs, 0.025) == 0:
                    return "flick_right" if dx > 0 else "flick_left"
        window = [p for p in pts if t - p[0] <= 1.3]
        if len(window) >= 8 and self._share(window, "pointing") >= 0.6:
            xs = [p[1] for p in window]
            if reversals(xs, 0.03) >= 3:
                return "wag"
            c = self._circle(window)
            if c:
                return c
        if len(window) >= 8 and self._share(window, "open") >= 0.6:
            if reversals([p[3] for p in window], 0.05) >= 3:
                return "wave"
        short = [p for p in pts if t - p[0] <= 0.6]
        if len(short) >= 4 and self._share(short, "open") >= 0.7:
            s0 = min(p[5] for p in short[: len(short) // 2 + 1])
            moved = math.hypot(short[-1][3] - short[0][3], short[-1][4] - short[0][4])
            if short[-1][5] >= 1.35 * s0 and moved < 0.1:
                return "push"
        return None

    @staticmethod
    def _circle(window) -> str | None:
        xy = np.array([(p[1], p[2]) for p in window])
        c = xy.mean(axis=0)
        r = np.linalg.norm(xy - c, axis=1)
        if r.mean() < 0.03 or r.std() > 0.5 * r.mean():
            return None
        ang = np.unwrap(np.arctan2(xy[:, 1] - c[1], xy[:, 0] - c[0]))
        total = ang[-1] - ang[0]
        if abs(total) < 1.6 * math.pi:
            return None
        # y points down: increasing angle = clockwise on screen
        return "circle_cw" if total > 0 else "circle_ccw"


class PairDetector:
    """Two-hand gestures: moving open hands apart / together."""

    def __init__(self, refractory: float = 1.2):
        self.hist: deque = deque()
        self.refractory = refractory
        self.last = -math.inf

    def reset(self):
        self.hist.clear()

    def update(self, t, a, b, both_open: bool) -> str | None:
        if self.hist and t - self.hist[-1][0] > 0.3:
            self.hist.clear()
        self.hist.append((t, math.hypot(a[0] - b[0], a[1] - b[1]), both_open))
        while self.hist and t - self.hist[0][0] > 0.8:
            self.hist.popleft()
        if t - self.last < self.refractory or len(self.hist) < 4:
            return None
        pts = list(self.hist)
        if sum(p[2] for p in pts) < 0.7 * len(pts):
            return None
        d = pts[-1][1] - pts[0][1]
        if abs(d) >= 0.18:
            self.last = t
            self.hist.clear()
            return "hands_apart" if d > 0 else "hands_together"
        return None


def pair_pose(a: str | None, b: str | None) -> str | None:
    """Pose of both hands at once (a and b: each hand's label)."""
    pair = {a, b}
    if pair == {"open_palm"}:
        return "both_palms"
    if pair == {"thumb_up"}:
        return "both_thumbs_up"
    if pair == {"thumb_down"}:
        return "both_thumbs_down"
    if pair == {"closed_fist"}:
        return "both_fists"
    return None


def head_angles(matrix) -> tuple[float, float]:
    """Facial transformation matrix 4×4 → (yaw, pitch) in degrees.
    Yaw + = toward the user's right; pitch + = looking down."""
    n = np.asarray(matrix, dtype=float)[:3, 2]          # where the nose points (camera: y up)
    yaw = -math.degrees(math.atan2(n[0], n[2]))
    pitch = -math.degrees(math.atan2(n[1], math.hypot(n[0], n[2])))
    return yaw, pitch


def ensure_models() -> None:
    MP_DIR.mkdir(parents=True, exist_ok=True)
    for name, url in MODEL_URLS.items():
        dest = MP_DIR / name
        if dest.exists() and dest.stat().st_size > 100_000:
            continue
        log.info("Downloading %s…", name)
        tmp = dest.with_suffix(".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(dest)


# ── Engine ────────────────────────────────────────────────────────────────────────────────────

class Grabber:
    """Reads the camera continuously in its own thread (as fast as it goes, capped at MAX_CAM_FPS) and keeps the latest frame.

    This way the analysis always gets a fresh frame (without the driver buffer lag) and the preview
    stays smooth even if the analysis is slower.
    """

    def __init__(self, cap, on_frame: Callable | None = None):
        self.cap = cap
        self.on_frame = on_frame
        self.fails = 0
        self.fps = 0.0
        self._last_t = None
        self._frame = None
        self._seq = 0
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                ok, frame = self.cap.read()
            except Exception:
                ok, frame = False, None
            if not ok or frame is None:
                self.fails += 1
                self._stop.wait(0.05)
                continue
            self.fails = 0
            now = time.monotonic()
            # fps cap: frames arriving too early are read (so the buffer does not clog) and dropped
            if self._last_t is not None and now - self._last_t < 1 / MAX_CAM_FPS - 0.002:
                continue
            if self._last_t is not None:
                self.fps = 0.9 * self.fps + 0.1 / max(now - self._last_t, 1e-3) if self.fps else 1 / max(now - self._last_t, 1e-3)
            self._last_t = now
            with self._cond:
                self._frame, self._seq = frame, self._seq + 1
                self._cond.notify_all()
            if self.on_frame:
                try:
                    self.on_frame(frame)
                except Exception:
                    log.exception("Preview error")

    def wait(self, after: int, timeout: float = 1.0):
        """The first frame after `after`: (frame, number), or (None, after) if none arrives in time."""
        with self._cond:
            self._cond.wait_for(lambda: self._seq > after or self._stop.is_set(), timeout)
            if self._seq > after:
                return self._frame, self._seq
            return None, after

    def close(self):
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        self._thread.join(timeout=3)
        try:
            self.cap.release()
        except Exception:
            pass


class Vision:
    def __init__(self, on_gesture: Callable[[str, str], None] | None = None,
                 on_presence: Callable[[bool], None] | None = None,
                 capture_factory: Callable[[int], object] | None = None):
        self.on_gesture = on_gesture
        self.on_presence = on_presence
        self.preview_clients = 0
        self._capture_factory = capture_factory or self._open_camera
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._cap = None
        self._grabber: Grabber | None = None
        self._seq = 0
        self._last_preview = 0.0
        self._cap_index = None
        self._prev_small = None
        self._stuck = 0
        self._slow_since = None
        self._slow_reopens = 0   # if still slow after reopening 3 times (dark room), give up
        self._face = self._gest = None
        self._t0 = time.monotonic()
        self._ts = {"face": -1, "gest": -1}
        # estado
        self.enabled = False
        self.camera_ok = False
        self.error: str | None = None
        self.looking = False
        self.yaw = self.pitch = None
        self.hand = False
        self.label: str | None = None
        self.score = 0.0
        self.last_event: dict | None = None
        self.fps = 0.0
        self._face_lms = None
        self._hand_pts = None
        self.measure = {"brightness": None, "hand_size": None, "side": None, "pinch": None}   # for calibration
        self._jpeg: bytes | None = None
        self._last_face_t = -math.inf
        self._last_hand_t = -math.inf
        self._last_tick = None
        # logic
        self.attention = Attention()
        self._trackers: dict[str, dict] = {}   # per hand: swipes and motions
        self.pair = PairDetector()
        self.hands = 0
        self._hands_pts: list = []
        self.hold = Hold()
        self.gate = Gate()
        self.yield_cpu = False   # set by Cortex while it transcribes
        self.classifier = CustomClassifier()
        self._custom: dict[str, dict] = {}
        self._rec: dict | None = None
        self._rec_result: dict | None = None
        self._load_custom()

    # ── API ──
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="vision", daemon=True)
        self._thread.start()
        self._face_thread = threading.Thread(target=self._face_loop, name="vision-face", daemon=True)
        self._face_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if getattr(self, "_face_thread", None):
            self._face_thread.join(timeout=5)
        self._release()
        for task in (self._face, self._gest):
            try:
                task and task.close()
            except Exception:
                pass
        self._face = self._gest = None

    @property
    def cam_fps(self) -> float:
        return self._grabber.fps if self._grabber else 0.0

    def frame_jpeg(self) -> bytes | None:
        return self._jpeg if self.preview_clients > 0 else None

    def state(self) -> dict:
        rec = self._rec
        return {
            "enabled": self.enabled,
            "camera_ok": self.camera_ok,
            "error": self.error,
            "looking": self.looking,
            "engaged": self.attention.engaged,
            "yaw": None if self.yaw is None else round(self.yaw, 1),
            "pitch": None if self.pitch is None else round(self.pitch, 1),
            "hand": self.hand,
            "hands": self.hands,
            "label": self.label,
            "score": round(float(self.score), 2),
            "last_event": self.last_event,
            "fps": round(self.fps, 1),
            "cam_fps": round(self.cam_fps, 1),
            "recording": rec["name"] if rec else None,
            "progress": round(self._rec_progress(), 2) if rec else 0.0,
            "record_result": self._rec_result,
            "custom": self.list_custom(),
            **self.measure,
        }

    def record_custom(self, name: str, seconds: float = 3.0) -> bool:
        """Start recording the current pose (non-blocking; progress is reported in state())."""
        name = re.sub(r"\s+", " ", str(name)).strip()[:40]
        if not name or ":" in name or self._rec or not self.enabled:
            return False
        self._rec_result = None
        self._rec = {"name": name, "seconds": max(0.5, float(seconds)), "start": time.monotonic(), "frames": []}
        log.info("Recording custom gesture \"%s\" (%.1f s)", name, seconds)
        return True

    def delete_custom(self, name: str) -> bool:
        with self._lock:
            if name not in self._custom:
                return False
            del self._custom[name]
            self._refit()
        self._save_custom()
        return True

    def list_custom(self) -> list[dict]:
        with self._lock:
            return [{"name": n, "id": f"custom:{n}", "samples": len(g["samples"]), "created": g.get("created")}
                    for n, g in self._custom.items()]

    # ── custom gestures ──
    def _load_custom(self):
        try:
            if CUSTOM_FILE.exists():
                data = json.loads(CUSTOM_FILE.read_text(encoding="utf-8"))
                self._custom = data.get("gestures", {})
        except Exception as e:
            log.warning("Could not read %s: %s", CUSTOM_FILE, e)
            self._custom = {}
        self._refit()

    def _refit(self):
        self.classifier.fit({n: g["samples"] for n, g in self._custom.items()})

    def _save_custom(self):
        with self._lock:
            data = {"version": 1, "gestures": self._custom}
            CUSTOM_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = CUSTOM_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(CUSTOM_FILE)

    def _rec_progress(self) -> float:
        rec = self._rec
        return min(1.0, (time.monotonic() - rec["start"]) / rec["seconds"]) if rec else 0.0

    def _finish_recording(self):
        rec, self._rec = self._rec, None
        frames = rec["frames"]
        if len(frames) < 5:
            self._rec_result = {"name": rec["name"], "ok": False, "frames": len(frames),
                                "error": "hand not seen"}
            log.info("Recording of \"%s\" failed: %d frames with a hand", rec["name"], len(frames))
            return
        with self._lock:
            self._custom[rec["name"]] = {"created": datetime.now().isoformat(timespec="seconds"),
                                         "samples": [[round(float(v), 4) for v in f] for f in frames]}
            self._refit()
        self._save_custom()
        self._rec_result = {"name": rec["name"], "ok": True, "frames": len(frames), "error": None}
        log.info("Custom gesture \"%s\" saved (%d samples)", rec["name"], len(frames))

    # ── camera and models ──
    @staticmethod
    def _open_camera(index: int):
        cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap.release()
            return None
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, W)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, H)
        return cap

    def _release(self):
        if self._grabber is not None:
            self._grabber.close()   # also closes the camera, after stopping its thread
            self._grabber = None
        elif self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
        if self._cap is not None:
            self._cap = None
            log.info("Camera released")
        self._seq = 0
        self.camera_ok = False

    def _load_models(self):
        if self._face and self._gest:
            return
        ensure_models()
        _, mpv, BaseOptions = _mediapipe()

        self._face = mpv.FaceLandmarker.create_from_options(mpv.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(MP_DIR / "face_landmarker.task")),
            running_mode=mpv.RunningMode.VIDEO, num_faces=1,
            output_facial_transformation_matrixes=True))
        self._gest = mpv.GestureRecognizer.create_from_options(mpv.GestureRecognizerOptions(
            base_options=BaseOptions(model_asset_path=str(MP_DIR / "gesture_recognizer.task")),
            running_mode=mpv.RunningMode.VIDEO, num_hands=2,
            min_hand_detection_confidence=0.6, min_hand_presence_confidence=0.6))
        log.info("Vision models loaded")

    def _stamp(self, task: str, now: float) -> int:
        ms = int((now - self._t0) * 1000)
        if ms <= self._ts[task]:
            ms = self._ts[task] + 1
        self._ts[task] = ms
        return ms

    # ── loop ──
    def _run(self):
        log.info("Vision started")
        fails = 0
        while not self._stop.is_set():
            try:
                if not cfg("gestures_enabled"):
                    if self.enabled or self._cap is not None:
                        self._disable()
                    self._stop.wait(0.5)
                    continue
                self.enabled = True
                if not (self._face and self._gest):
                    try:
                        self._load_models()
                    except Exception as e:
                        self.error = f"models: {e}"
                        log.warning("Could not load the vision models: %s", e)
                        self._stop.wait(30)
                        continue
                index = int(cfg("camera_index"))
                if self._cap is not None and index != self._cap_index:
                    self._release()
                if self._cap is None:
                    self._cap = self._capture_factory(index)
                    self._cap_index = index
                    if self._cap is None:
                        self.camera_ok = False
                        self.error = f"could not open camera {index}"
                        self._stop.wait(3)
                        continue
                    log.info("Camera %d opened", index)
                    self._grabber = Grabber(self._cap, self._preview_frame)
                    self._seq = 0
                self._wait_tick()
                frame, self._seq = self._grabber.wait(self._seq, timeout=1.0)
                if frame is None:
                    if self._grabber.fails >= 20 or not self.camera_ok:
                        fails += 1
                    if fails >= 3:
                        self.error = "the camera gives no image"
                        log.warning("The camera gives no image; retrying")
                        fails = 0
                        self._release()
                        self._stop.wait(3)
                    continue
                fails = 0
                frame = frame.copy()   # the driver may reuse its buffer
                # black or frozen image for a while: the connection hung (happens after hours
                # on or when resuming from suspend); reopen the camera
                small = cv2.resize(frame, (32, 24)).astype(np.int16)
                stuck = small.mean() < 12 or (self._prev_small is not None
                                              and not np.any(small != self._prev_small))
                self._prev_small = small
                self._stuck = self._stuck + 1 if stuck else 0
                # or ~1 fps with noise: the driver got confused (seen on 27-10); also reopen
                slow = self._grabber.fps and self._grabber.fps < 3
                self._slow_since = (self._slow_since or time.monotonic()) if slow else None
                if not slow and self._grabber.fps:
                    self._slow_reopens = 0
                too_slow = slow and time.monotonic() - self._slow_since > 20 and self._slow_reopens < 3
                if too_slow or self._stuck >= 30:
                    if too_slow:
                        self._slow_reopens += 1
                        log.warning("The camera runs at %.1f fps; reopening it", self._grabber.fps)
                    else:
                        log.warning("The camera image is black or frozen; reopening it")
                    self._stuck, self._prev_small, self._slow_since = 0, None, None
                    self._release()
                    self._stop.wait(1)
                    continue
                self.camera_ok = True
                self.error = None
                self._process(frame)
            except Exception as e:
                self.error = str(e)
                log.exception("Error in the vision loop")
                self._stop.wait(1)
        log.info("Vision stopped")

    def _disable(self):
        self._release()
        self.enabled = False
        self._rec = None
        self._jpeg = None
        self._last_tick, self.fps = None, 0.0
        self._reset_hand()
        self.looking, self.yaw, self.pitch, self._face_lms = False, None, None, None
        if self.attention.engaged:
            self.attention = Attention()
            self._presence(False)
        else:
            self.attention = Attention()

    def _hands_active(self) -> bool:
        if self.yield_cpu and not self._rec:   # Cortex is transcribing: leave the CPU to it (~25 % faster)
            return False
        return bool(self._rec) or not cfg("gestures_require_look") or self.attention.engaged

    def _wait_tick(self):
        now = time.monotonic()
        if not self._hands_active():
            period = FACE_PERIOD
        elif self._rec or now - self._last_hand_t < 1.5:
            period = HAND_PERIOD
        else:
            period = SEARCH_PERIOD
        if self._last_tick is not None:
            delay = self._last_tick + period - now
            if delay > 0:
                self._stop.wait(delay)
            dt = time.monotonic() - self._last_tick
            self.fps = 0.8 * self.fps + 0.2 / max(dt, 1e-3) if self.fps else 1 / max(dt, 1e-3)
        self._last_tick = time.monotonic()

    def _process(self, frame):
        mp = _mediapipe()[0]

        now = time.monotonic()
        h, w = frame.shape[:2]
        self.measure["brightness"] = round(float(self._prev_small.mean()), 1) if self._prev_small is not None else None

        if self._hands_active():
            image = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            self._hands(image, now, w, h)
        else:
            self._reset_hand()

        if self._rec and self._rec_progress() >= 1.0:
            self._finish_recording()

        if self.preview_clients <= 0:
            self._jpeg = None

    def _face_loop(self):
        """Separate thread for the face (~5 fps) so it does not slow down hand analysis. If something fails it is logged and
        the loop continues (a stray error used to kill the thread silently and the camera stopped seeing the face until restart)."""
        while not self._stop.is_set():
            try:
                self._face_loop_inner()
            except Exception:
                log.exception("Error in the face thread; resuming")
                self._stop.wait(1)

    def _face_loop_inner(self):
        mp = _mediapipe()[0]

        grabber, seq = None, 0
        while not self._stop.is_set():
            g = self._grabber
            if g is None or self._face is None or not self.enabled:
                grabber = None
                self._stop.wait(0.2)
                continue
            if g is not grabber:
                grabber, seq = g, 0
            frame, seq = g.wait(seq, timeout=1.0)
            if frame is None:
                if g._stop.is_set():
                    self._stop.wait(0.2)   # the camera is closing: do not spin idly
                continue
            now = time.monotonic()
            try:
                image = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                res = self._face.detect_for_video(image, self._stamp("face", now))
            except Exception:
                log.exception("Error analysing the face")
                self._stop.wait(1)
                continue
            if res.face_landmarks and res.facial_transformation_matrixes:
                self._face_lms = res.face_landmarks[0]
                self.yaw, self.pitch = head_angles(res.facial_transformation_matrixes[0])
                lim = float(cfg("look_max_angle"))
                self.looking = abs(self.yaw) <= lim and abs(self.pitch - float(cfg("look_pitch_offset"))) <= lim
            else:
                self._face_lms, self.yaw, self.pitch, self.looking = None, None, None, False
            change = self.attention.update(self.looking, now)
            if change is not None:
                log.info("Attention: %s", "engaged" if change else "no longer looking")
                self._presence(change)
            self._stop.wait(max(0.0, now + FACE_PERIOD - time.monotonic()))

    def _preview_frame(self, frame):
        """From the camera thread: smooth preview with the latest detections (max 25 fps)."""
        if self.preview_clients <= 0:
            return
        now = time.monotonic()
        if now - self._last_preview < 0.04:
            return
        self._last_preview = now
        self._jpeg = self._annotate(frame)

    def _reset_hand(self):
        self.hand, self.label, self.score, self._hand_pts = False, None, 0.0, None
        self.hands = 0
        self._hands_pts = []
        for tr in self._trackers.values():
            tr["swipes"].reset()
            tr["motion"].reset()
        self.pair.reset()
        self.hold.update(None, time.monotonic(), 0)

    def _tracker(self, key: str) -> dict:
        if key not in self._trackers:
            self._trackers[key] = {"swipes": SwipeDetector(), "motion": MotionDetector()}
        return self._trackers[key]

    def _classify(self, res, i: int, pts, left: bool):
        """Label of one hand: custom gesture > MediaPipe gesture > geometric pose.
        Returns (label, confidence, geometric pose, normalized vector)."""
        vec = normalize(pts, left)
        kind = pose(fingers(pts))
        thr = float(cfg("custom_gesture_threshold"))
        name, dist = self.classifier.classify(vec, thr)
        cat = res.gestures[i][0] if len(res.gestures) > i and res.gestures[i] else None
        if name:
            return f"custom:{name}", max(0.0, 1.0 - 0.4 * dist / thr), kind, vec
        if cat and cat.category_name in CANNED and cat.score >= float(cfg("gesture_min_score")):
            return CANNED[cat.category_name], cat.score, kind, vec
        if kind in EXTRA_POSES:
            return kind, 0.8, kind, vec
        return None, 0.0, kind, vec

    def _near_mouth(self, pts, w, h) -> bool:
        lms = self._face_lms
        if not lms:
            return False
        mouth = ((lms[13].x + lms[14].x) / 2 * w, (lms[13].y + lms[14].y) / 2 * h)
        face_w = abs(lms[454].x - lms[234].x) * w
        return face_w > 10 and shush_pose(pts, mouth, face_w)

    def _hands(self, image, now, w, h):
        res = self._gest.recognize_for_video(image, self._stamp("gest", now))
        found = []
        for i, lms in enumerate(res.hand_landmarks or []):
            pts = np.array([(p.x * w, p.y * h) for p in lms])
            side = res.handedness[i][0].category_name if len(res.handedness) > i and res.handedness[i] else "?"
            found.append((i, pts, side))
        # left to right as the user sees it; if both report the same side, tell them apart by position
        found.sort(key=lambda f: -f[1][0, 0])
        sides = [f[2] for f in found]
        keys = [side if sides.count(side) == 1 else f"{side}{n}" for n, side in enumerate(sides)]

        labels, motions, palms, kinds = [], [], [], []
        self._hands_pts = [f[1] for f in found]
        self._hand_pts = found[0][1] if found else None
        if found:   # wrist → middle knuckle as a fraction of the frame height: grows as the hand gets closer
            self.measure["hand_size"] = round(float(np.hypot(*(found[0][1][9] - found[0][1][0])) / h), 3)
            self.measure["side"] = found[0][2]
            self.measure["pinch"] = [round(float(v), 4) for v in (found[0][1][4] - found[0][1][8]) / (w, h)]
        for key, (i, pts, side) in zip(keys, found):
            tr = self._tracker(key)
            tr["seen"] = now
            label, score, kind, vec = self._classify(res, i, pts, side == "Left")
            if kind == "pointing" and self._near_mouth(pts, w, h):
                label, score = "shush", 0.9   # takes precedence over MediaPipe's "index up"
            palm = pts[PALM].mean(axis=0)
            palm_u = (1.0 - palm[0] / w, palm[1] / h)            # x mirrored (user's view)
            tip_u = (1.0 - pts[8][0] / w, pts[8][1] / h)
            tr["swipes"].min_dist = float(cfg("swipe_min_distance"))
            # with the index finger extended, finger flicks take precedence over palm swipes
            if kind == "pointing":
                tr["swipes"].reset()
                swipe = None
            else:
                swipe = tr["swipes"].update(now, *palm_u)
            motion = tr["motion"].update(now, tip_u, palm_u, np.linalg.norm(pts[9] - pts[0]) / w, kind)
            if self._rec is not None and vec is not None and not labels:
                self._rec["frames"].append(vec)
            labels.append((label, score))
            motions.append(motion or swipe)
            palms.append(palm_u)
            kinds.append(kind)
        for key in [k for k, tr in self._trackers.items() if now - tr.get("seen", 0) > 0.5]:
            del self._trackers[key]

        self.hands = len(found)
        self.hand = bool(found)
        pair_motion = pair_label = None
        if len(found) == 2:
            pair_motion = self.pair.update(now, palms[0], palms[1], all(k == "open" for k in kinds))
            pair_label = pair_pose(labels[0][0], labels[1][0])
        else:
            self.pair.reset()
        if pair_label:
            self.label, self.score = pair_label, min(labels[0][1], labels[1][1])
        elif labels:
            self.label, self.score = labels[0]
        else:
            self.label, self.score = None, 0.0

        if self._rec is not None:            # recording: nothing fires
            self.hold.update(None, now, 0)
            return
        self.gate.cooldown = float(cfg("gesture_cooldown"))
        self.gate.observe(self.label, now)
        for m in [pair_motion] + motions:
            if m and self.gate.allow(m, now):
                self._fire(m, now)
                return
        trackers = list(self._trackers.values())
        moving = any(tr["swipes"].moving(now) for tr in trackers)
        last_move = max([tr["swipes"].last_swipe for tr in trackers] + [tr["motion"].last for tr in trackers]
                        + [-math.inf])
        still = not moving and now - last_move >= STATIC_AFTER_SWIPE
        # with two hands only pair poses count (otherwise each hand's palm would fire on its own)
        static = pair_label if len(found) == 2 else (labels[0][0] if labels else None)
        held = self.hold.update(static if still else None, now, float(cfg("gesture_hold_seconds")))
        if held and self.gate.allow(held, now, repeat=self._action(held) in REPEATABLE):
            self._fire(held, now)

    @staticmethod
    def _action(gid: str) -> str:
        return {**DEFAULTS["gesture_actions"], **(cfg("gesture_actions") or {})}.get(gid) or "none"

    def _fire(self, gid: str, now: float):
        self.gate.fire(gid, now)
        action = self._action(gid)
        self.last_event = {"gesture": gid, "action": action, "active": action != "none", "time": time.time()}
        log.info("Gesture %s → %s", gid, action)
        if self.on_gesture:
            try:
                self.on_gesture(gid, action)
            except Exception:
                log.exception("Error in on_gesture")

    def _presence(self, engaged: bool):
        if self.on_presence:
            try:
                self.on_presence(engaged)
            except Exception:
                log.exception("Error in on_presence")

    # ── preview ──
    def _annotate(self, frame) -> bytes | None:
        img = cv2.flip(frame, 1)
        h, w = img.shape[:2]

        def pt(x, y):                       # normalized coordinates → pixel in the mirrored view
            return int((1 - x) * w), int(y * h)

        color = (60, 200, 60) if self.looking else (60, 60, 230)
        if self._face_lms:
            FC = _mediapipe()[1].FaceLandmarksConnections
            lms = self._face_lms
            for c in FC.FACE_LANDMARKS_FACE_OVAL:
                cv2.line(img, pt(lms[c.start].x, lms[c.start].y), pt(lms[c.end].x, lms[c.end].y), color, 1, cv2.LINE_AA)
            xs = [w - p.x * w for p in lms]
            ys = [p.y * h for p in lms]
            cv2.rectangle(img, (int(min(xs)), int(min(ys))), (int(max(xs)), int(max(ys))), color, 2)
            cv2.circle(img, pt(lms[1].x, lms[1].y), 3, color, -1)
        for n, pts in enumerate(list(self._hands_pts)):
            hp = [(int(w - x), int(y)) for x, y in pts]
            col = (255, 200, 0) if n == 0 else (255, 120, 220)
            for a, b in HAND_LINKS:
                cv2.line(img, hp[a], hp[b], col, 2, cv2.LINE_AA)
            for p in hp:
                cv2.circle(img, p, 3, (255, 255, 255), -1)
        for tr in list(self._trackers.values()):
            hist = list(tr["motion"].hist)
            palm_trail = [(int(p[3] * w), int(p[4] * h)) for p in hist]
            tip_trail = [(int(p[1] * w), int(p[2] * h)) for p in hist]
            for a, b in zip(palm_trail, palm_trail[1:]):
                cv2.line(img, a, b, (0, 220, 255), 2, cv2.LINE_AA)
            for a, b in zip(tip_trail, tip_trail[1:]):
                cv2.line(img, a, b, (80, 255, 120), 2, cv2.LINE_AA)

        yaw = "--" if self.yaw is None else f"{self.yaw:+.0f}"
        pitch = "--" if self.pitch is None else f"{self.pitch:+.0f}"
        lines = [
            (f"yaw {yaw}  pitch {pitch}  {'LOOKING' if self.looking else 'not looking'}", color),
            (f"engaged: {'YES' if self.attention.engaged else 'no'}   camera {self.cam_fps:.0f} fps  analysis {self.fps:.0f} fps",
             (60, 200, 60) if self.attention.engaged else (200, 200, 200)),
            (f"hands {self.hands}  gesture: {self.label or '-'}  {self.score:.2f}" if self.hand else "gesture: (no hand)",
             (255, 255, 255)),
        ]
        ev = self.last_event
        if ev and time.time() - ev["time"] < 5:
            lines.append((f"last: {ev['gesture']} -> {ev['action']}", (0, 220, 255)))
        if self._rec:
            lines.append((f"RECORDING {self._rec['name']} {self._rec_progress() * 100:.0f}%", (60, 60, 255)))
        band = img[: 12 + 22 * len(lines)]
        band[:] = (band * 0.4).astype(np.uint8)              # translucent dark background for the text
        for i, (text, c) in enumerate(lines):
            cv2.putText(img, text, (8, 24 + 22 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.55, c, 1, cv2.LINE_AA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 75])
        return buf.tobytes() if ok else None
