"""
snap_zones.py
Six snap zones across three monitors — left half and right half per monitor.

Zone IDs:  M1_LEFT  M1_RIGHT  M2_LEFT  M2_RIGHT  M3_LEFT  M3_RIGHT

Adjacency forms a strict linear chain so movement is always spatial:
  M1_LEFT ↔ M1_RIGHT ↔ M2_LEFT ↔ M2_RIGHT ↔ M3_LEFT ↔ M3_RIGHT
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

# ── Zone ID constants ─────────────────────────────────────────────────────────

M1_LEFT  = "M1_LEFT"
M1_RIGHT = "M1_RIGHT"
M2_LEFT  = "M2_LEFT"
M2_RIGHT = "M2_RIGHT"
M3_LEFT  = "M3_LEFT"
M3_RIGHT = "M3_RIGHT"

ALL_ZONE_IDS: list[str] = [
    M1_LEFT, M1_RIGHT,
    M2_LEFT, M2_RIGHT,
    M3_LEFT, M3_RIGHT,
]

# Linear chain — the only valid path between any two zones travels through
# every zone in between, enforcing the no-teleport constraint.
# Rebuilt by build_zones() for however many monitors are actually connected.
_ADJACENCY: dict[str, list[str]] = {}


# ── Zone dataclass ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Zone:
    """A single snap destination.  Coordinates are in pygame canvas space."""
    id:          str
    monitor_idx: int
    x: int
    y: int
    w: int
    h: int

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2

    @property
    def rect(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.w, self.h

    def neighbors(self) -> list[str]:
        return list(_ADJACENCY.get(self.id, []))


# ── Builder ───────────────────────────────────────────────────────────────────

def build_zones(monitor_rects: list[dict]) -> dict[str, Zone]:
    """
    Build 2 Zone objects per monitor (left + right half), for however many
    monitors are actually connected.

    monitor_rects: list of {"x","y","w","h"} in pygame canvas coords,
                   sorted left-to-right.
    """
    if not monitor_rects:
        raise ValueError("Need at least 1 monitor, got 0")

    zones: dict[str, Zone] = {}
    for i, mon in enumerate(monitor_rects):
        tag = f"M{i + 1}"
        mx, my, mw, mh = mon["x"], mon["y"], mon["w"], mon["h"]
        half = mw // 2
        zones[f"{tag}_LEFT"]  = Zone(f"{tag}_LEFT",  i, mx,        my, half,      mh)
        zones[f"{tag}_RIGHT"] = Zone(f"{tag}_RIGHT", i, mx + half, my, mw - half, mh)

    # Rebuild the adjacency chain to match this run's zone count, in
    # left-to-right order, so find_path()/neighbors() stay spatially correct.
    ids = list(zones.keys())
    _ADJACENCY.clear()
    for idx, zid in enumerate(ids):
        neighbors = []
        if idx > 0:
            neighbors.append(ids[idx - 1])
        if idx < len(ids) - 1:
            neighbors.append(ids[idx + 1])
        _ADJACENCY[zid] = neighbors

    return zones


# ── Path finding ──────────────────────────────────────────────────────────────

def find_path(start_id: str, end_id: str) -> list[str]:
    """
    BFS shortest spatially-continuous path between two zone IDs.

    Example:
        find_path("M1_LEFT", "M3_RIGHT")
        → ["M1_LEFT", "M1_RIGHT", "M2_LEFT", "M2_RIGHT", "M3_LEFT", "M3_RIGHT"]
    """
    if start_id == end_id:
        return [start_id]
    if start_id not in _ADJACENCY or end_id not in _ADJACENCY:
        return [start_id, end_id]

    visited: set[str] = {start_id}
    queue: deque[list[str]] = deque([[start_id]])

    while queue:
        path = queue.popleft()
        for nb in _ADJACENCY[path[-1]]:
            if nb == end_id:
                return path + [nb]
            if nb not in visited:
                visited.add(nb)
                queue.append(path + [nb])

    return [start_id, end_id]


# ── Nearest zone lookup ───────────────────────────────────────────────────────

def nearest_zone_id(canvas_x: int, canvas_y: int, zones: dict[str, Zone]) -> str:
    """Zone ID whose centre is geometrically closest to (canvas_x, canvas_y)."""
    return min(
        zones,
        key=lambda zid: math.hypot(
            canvas_x - zones[zid].center[0],
            canvas_y - zones[zid].center[1],
        ),
    )
