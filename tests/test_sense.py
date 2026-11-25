"""Cortex Sense and its link to Hub, without a webcam: commands, calibration, shared camera, protocol."""

import threading

import numpy as np
import pytest

from sense import calibration, commands
from sense.camera import SharedCamera
from sense.config import DEFAULTS


# ── commands ──
def test_parse_keys():
    assert commands.parse_keys("ctrl+shift+m") == [0x11, 0x10, ord("M")]
    assert commands.parse_keys("Win + D") == [0x5B, ord("D")]
    assert commands.parse_keys("alt+f4") == [0x12, 0x73]
    with pytest.raises(ValueError):
        commands.parse_keys("ctrl+banana")


@pytest.mark.parametrize("action,ok", [
    ("none", True), ("next", True), ("listen", True), ("media:play_pause", True), ("media:jump", False),
    ("keys:ctrl+c", True), ("keys:", False), ("run:notepad", True), ("run: ", False),
    ("window:minimize", True), ("window:minimize_all", True), ("window:lock", True), ("window:fly", False), ("rm -rf", False),
    ("script:", False), ("script:missing.ps1", False),
])
def test_validate(action, ok):
    assert (commands.validate(action) is None) == ok


def test_every_default_mapping_is_valid():
    assert all(commands.validate(a) is None for a in DEFAULTS["gesture_actions"].values())


def test_script_actions_need_a_real_script_file(tmp_path):
    ps1 = tmp_path / "my script.ps1"
    ps1.write_text("Write-Output hi")
    (tmp_path / "x.exe").write_text("")
    assert commands.validate(f'script:"{ps1}" -Fast') is None
    assert commands.script_args(f'"{ps1}" -Fast') == [str(ps1), "-Fast"]
    assert "ps1" in commands.validate(f"script:{tmp_path / 'x.exe'}")   # programs go in run:, scripts in script:


def test_route_sends_hub_actions_to_hub_or_falls_back_to_media_keys():
    assert commands.route("next", hub_connected=True) == "hub"
    assert commands.route("next", hub_connected=False) == "media:next"
    assert commands.route("toggle", hub_connected=False) == "media:play_pause"
    assert commands.route("listen", hub_connected=False) is None   # without Hub there is no assistant to listen
    assert commands.route("keys:ctrl+c", hub_connected=True) == "keys:ctrl+c"
    assert commands.route("none", hub_connected=True) is None


def test_run_refuses_invalid_and_hub_actions():
    assert commands.run("next") is False
    assert commands.run("media:jump") is False


# ── calibration ──
def _samples(pitch=8.0, size=0.12, pinch_px=(20, 200)):
    look = [{"pitch": pitch + (i % 3 - 1), "brightness": 90, "cam_fps": 30} for i in range(30)]
    hand = [{"hand": True, "hand_size": size, "side": "Right"} for _ in range(30)]
    lo, hi = pinch_px
    pinch = [{"hand": True, "pinch": [d / 1920, 0]} for d in np.linspace(lo, hi, 40)]
    return {"look": look, "hand": hand, "pinch": pinch}


def test_calibration_adapts_to_the_user():
    changes, report = calibration.suggest(_samples(), (1920, 1080), DEFAULTS)
    assert changes["look_pitch_offset"] == 8
    assert changes["swipe_min_distance"] < DEFAULTS["swipe_min_distance"]   # small hand (far away) → shorter travel
    assert changes["pinch_grab"] < changes["pinch_release"]
    assert 20 < changes["pinch_grab"] < 200
    assert report["side"] == "Right" and report["notes"] == []


def test_calibration_keeps_settings_without_data():
    changes, report = calibration.suggest({}, (1920, 1080), DEFAULTS)
    assert changes == {}
    assert len(report["notes"]) == 3


def test_calibration_warns_about_dark_rooms():
    s = _samples()
    for x in s["look"]:
        x["brightness"] = 20
    _, report = calibration.suggest(s, (1920, 1080), DEFAULTS)
    assert any("light" in n.lower() for n in report["notes"])


# ── shared camera ──
class FakeCap:
    def __init__(self):
        self.n, self.released = 0, False

    def read(self):
        self.n += 1
        threading.Event().wait(0.005)
        return True, np.full((4, 4, 3), self.n % 255, np.uint8)

    def release(self):
        self.released = True


def test_shared_camera_feeds_two_readers_and_releases_with_the_last():
    caps = []
    cam = SharedCamera(0, opener=lambda i: caps.append(FakeCap()) or caps[-1])
    a, b = cam.reader(), cam.reader()
    assert len(caps) == 1   # opened only once
    ok_a, fa = a.read()
    ok_b, fb = b.read()
    assert ok_a and ok_b and fa is not None and fb is not None
    a.release()
    assert not caps[0].released
    b.release()
    assert caps[0].released and not cam.opened


def test_shared_camera_reports_a_missing_camera():
    assert SharedCamera(3, opener=lambda i: None).reader() is None


# ── server: only the local panel may change things ──
def test_only_local_panels_may_change_settings():
    from sense.app import origin_ok
    assert origin_ok(None)   # Hub and local programs send no Origin
    assert origin_ok("http://127.0.0.1:8766") and origin_ok("http://localhost:8765")
    assert not origin_ok("https://evil.example") and not origin_ok("http://127.0.0.1:9999")


# ── Hub ↔ Sense ──
def test_sense_link_forwards_only_hub_gestures():
    from hub.sense_link import SenseLink
    got = []
    link = SenseLink("http://127.0.0.1:8766", on_gesture=lambda g, a: got.append((g, a)))
    assert link.state()["sense"] is False
    link.handle({"type": "gesture", "gesture": "swipe_left", "action": "next", "handled_by": "hub"})
    link.handle({"type": "gesture", "gesture": "push", "action": "window:desktop", "handled_by": "sense"})
    link.handle({"type": "settings", "settings": {"gesture_actions": {"ok": "listen"}}})
    link.handle({"type": "vision", "enabled": True, "custom": [{"name": "rock2"}]})
    assert got == [("swipe_left", "next")]
    link.connected = True
    assert link.state()["enabled"] and link.list_custom() == [{"name": "rock2"}]


def test_hub_settings_read_gesture_keys_from_sense():
    from hub import config
    from hub.sense_link import SenseLink
    s = config.Settings()
    link = SenseLink("http://127.0.0.1:8766")
    s.sense = link
    assert s["gesture_actions"] == {}   # Sense off: Hub's settings only
    link.connected, link.settings = True, {"gesture_actions": {"ok": "listen"}, "gestures_enabled": True}
    assert s["gesture_actions"] == {"ok": "listen"} and s.all()["gestures_enabled"] is True


def test_sense_api_validates_mappings_and_blocks_other_sites(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from sense import app as sense_app
    monkeypatch.setattr(sense_app.settings, "path", tmp_path / "sense.yaml")   # do not touch the real sense.yaml
    c = TestClient(sense_app.app)   # no `with`: does not start the camera
    assert "error" in c.post("/api/settings", json={"gesture_actions": {"ok": "run:"}}).json()
    r = c.post("/api/settings", json={"gesture_actions": {"ok": "keys:ctrl+shift+m"}}).json()
    assert r["settings"]["gesture_actions"]["ok"] == "keys:ctrl+shift+m"
    assert r["settings"]["gesture_actions"]["swipe_left"] == "next"   # the rest is preserved
    assert c.post("/api/settings", json={"gesture_actions": {"ok": "run:calc"}},
                  headers={"Origin": "https://evil.example"}).status_code == 403
    assert set(c.get("/api/catalog").json()["gestures"]) >= {"swipe_left", "both_palms"}
