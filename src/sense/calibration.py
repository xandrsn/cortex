"""Calibration: Sense measures your camera, your light and your hand, and adapts the thresholds.

The web wizard has three steps; each one stores samples of Vision.state():
  look   → looking at the screen, still: light, fps and the head pitch that counts as "looking" in your setup
  hand   → open hand in view: hand size (distance to the camera) and dominant hand
  pinch  → bring thumb and index together and apart: your closed and open pinch distances
suggest() es pura (tests/test_sense.py): samples → new settings + report.
"""

from statistics import median

REF_HAND = 0.16   # wrist → middle knuckle / frame height, hand ~60 cm from a 4:3 webcam (estimated)
STEPS = ("look", "hand", "pinch")


def _vals(samples, key):
    return [s[key] for s in samples if s.get(key) is not None]


def suggest(samples: dict[str, list[dict]], desktop: tuple[int, int], defaults: dict) -> tuple[dict, dict]:
    """(settings changes, report). If a step has no useful samples, its settings are left alone."""
    changes, report, notes = {}, {}, []
    look = samples.get("look", [])
    pitch = _vals(look, "pitch")
    if len(pitch) >= 5:
        report["pitch"] = round(median(pitch), 1)
        changes["look_pitch_offset"] = round(report["pitch"])
    else:
        notes.append("Could not see your face: \"only when I look at the screen\" may fail.")
    light = _vals(look, "brightness")
    if light:
        report["brightness"] = round(median(light))
        if report["brightness"] < 50:
            notes.append("Low light: gestures will fail more often. Put a light in front of you.")
    fps = _vals(look, "cam_fps")
    if fps:
        report["fps"] = round(median(fps), 1)
        if report["fps"] < 15:
            notes.append("The camera is slow (<15 fps): fast swipes may not register.")

    hand = [s for s in samples.get("hand", []) if s.get("hand") and s.get("hand_size")]
    if len(hand) >= 5:
        size = median(_vals(hand, "hand_size"))
        report["hand_size"] = round(size, 3)
        scale = min(1.4, max(0.6, size / REF_HAND))   # hand far away → shorter movements in the frame
        changes["swipe_min_distance"] = round(defaults["swipe_min_distance"] * scale, 3)
        sides = _vals(hand, "side")
        if sides:
            report["side"] = max(set(sides), key=sides.count)
    else:
        notes.append("Could not see your hand well: the swipe distance stays as it was.")

    pinch = [s["pinch"] for s in samples.get("pinch", []) if s.get("hand") and s.get("pinch")]
    w, h = desktop
    d = sorted(((dx * w) ** 2 + (dy * h) ** 2) ** 0.5 for dx, dy in pinch)
    if len(d) >= 8 and d[-1 - len(d) // 10] - d[len(d) // 10] > 30:
        closed, opened = d[len(d) // 10], d[-1 - len(d) // 10]   # 10th and 90th percentiles: no stray spikes
        changes["pinch_grab"] = round(closed + 0.35 * (opened - closed))
        changes["pinch_release"] = round(closed + 0.6 * (opened - closed))
        report["pinch"] = [round(closed), round(opened)]
    else:
        notes.append("Did not see the pinch open and close: the window-grab thresholds stay as they were.")
    report["notes"] = notes
    return changes, report
