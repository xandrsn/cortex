"""
config.py
Central settings file — edit here, no other file needs touching.
"""

# ── Monitor / Window ─────────────────────────────────────────────────────────
# None = auto-detect the full virtual desktop spanning all connected monitors.
# Override with explicit pixel values to constrain to a specific region.
WINDOW_WIDTH  = None   # e.g. 5760 for three 1920-wide monitors
WINDOW_HEIGHT = None   # e.g. 1080

# ── Cursor sensitivity ───────────────────────────────────────────────────────
# Amplifies cursor movement around the centre of the screen.
# 1.0 → full webcam range maps 1:1 to full screen width
# 1.5 → only the centre 67 % of webcam range reaches screen edges (recommended)
# 2.0 → only the centre 50 % is needed
# 3.0 → only the centre 33 % is needed
SENSITIVITY = 1.5

# ── Pinch detection (window pixels, raw coordinate space) ────────────────────
GRAB_THRESHOLD    = 40   # thumb-index distance that triggers grab
RELEASE_THRESHOLD = 60   # thumb-index distance that triggers release

# ── Cursor smoothing ─────────────────────────────────────────────────────────
# EMA weight applied to the cursor position every frame.
# Higher = more responsive but jittery.  Lower = smoother but laggy.
SMOOTH_ALPHA = 0.35

# ── Circles ──────────────────────────────────────────────────────────────────
NUM_CIRCLES        = 5
CIRCLE_MIN_RADIUS  = 65.0    # starting size range
CIRCLE_MAX_RADIUS  = 95.0
CIRCLE_GROW_AMOUNT = 55.0    # pixels added to radius per grow event
CIRCLE_MAX_SIZE    = 320.0   # hard cap
CIRCLE_LERP_SPEED  = 0.12    # fraction of radius gap closed per frame (grow animation)

# ── Webcam ───────────────────────────────────────────────────────────────────
WEBCAM_INDEX      = 0
WEBCAM_WIDTH      = 640
WEBCAM_HEIGHT     = 480
SHOW_CAM_PREVIEW  = True     # corner thumbnail
