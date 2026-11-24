"""Pure-logic tests: gesture classification, router commands, timers and window zones."""

import math

import pytest

from sense.window.gesture_detector import GestureDetector
from sense.window.snap_zones import build_zones, nearest_zone_id
from hub import router, timers, whatsapp
from hub.listener import find_wake


def _hand(finger_ratios: dict[int, float]) -> dict[int, tuple[float, float]]:
    """Synthetic hand: wrist at (0,0); each finger (tip, mcp) at distance 1.0 from the origin at its own angle.
    finger_ratios[tip_idx] = tip distance / mcp distance (the metric the detector uses)."""
    pos = {0: (0.0, 0.0)}
    for i, (tip, mcp) in enumerate(zip([8, 12, 16, 20], [5, 9, 13, 17])):
        ang = i * 0.3
        pos[mcp] = (math.cos(ang), math.sin(ang))
        r = finger_ratios[tip]
        pos[tip] = (r * math.cos(ang), r * math.sin(ang))
    return pos


FIST = {t: 0.5 for t in [8, 12, 16, 20]}
OPEN = {t: 2.0 for t in [8, 12, 16, 20]}


def test_fist_when_all_fingers_curled():
    assert GestureDetector()._detect_fist(_hand(FIST))
    assert not GestureDetector()._detect_fist(_hand(OPEN))


def test_open_hand_needs_three_extended_fingers():
    det = GestureDetector()
    assert det._detect_open(_hand(OPEN))
    three = {**FIST, 8: 2.0, 12: 2.0, 16: 2.0}
    assert det._detect_open(_hand(three))
    two = {**FIST, 8: 2.0, 12: 2.0}
    assert not det._detect_open(_hand(two))


def test_peace_sign_is_index_and_middle_extended_only():
    det = GestureDetector()
    peace = {**FIST, 8: 2.0, 12: 2.0}
    assert det._detect_peace(_hand(peace))
    assert not det._detect_peace(_hand(OPEN))   # ring and pinky extended: not a ✌
    assert not det._detect_peace(_hand(FIST))


def test_missing_wrist_is_never_a_gesture():
    det = GestureDetector()
    pos = _hand(OPEN)
    del pos[0]
    assert not det._detect_fist(pos) and not det._detect_open(pos) and not det._detect_peace(pos)


@pytest.mark.parametrize("text", ["Cortex, ¿qué hora es?", "que hora es", "la hora"])
def test_router_answers_time_questions(text):
    answer = router.route(text, "es")
    assert answer and answer.startswith("Son las ")


@pytest.mark.parametrize("text, sens, rest", [
    ("Cortex, pon música", 1, "pon música"), ("Córtex pon música", 1, "pon música"), ("Pon algo de música, Cortex", 2, "Pon algo de música"),
    ("Oye Kortex, qué hora es", 2, "qué hora es"), ("Cor tex para", 2, "para"), ("Cortecs.", 2, ""), ("Gortex, para", 3, "para"),
    ("Kortex para", 1, None), ("Pepito, pon música", 3, None), ("Cortes de pelo", 3, None), ("El corte de luz", 3, None),
    ("Corteza de árbol", 3, None), ("Contexto del correo", 3, None), ("Corto y cambio", 3, None),
])
def test_wake_word(text, sens, rest):
    assert find_wake(text, sens) == rest


def test_whatsapp_only_sends_when_the_user_allowed_it(monkeypatch):
    posted = []

    class Reply:
        def json(self):
            return {"success": True}

    monkeypatch.setattr(whatsapp, "settings", {"whatsapp_mode": "read"})
    monkeypatch.setattr(whatsapp.httpx, "post", lambda url, **k: posted.append(k["json"]) or Reply())
    wa = whatsapp.WhatsApp()
    wa.chats, wa.state = [{"jid": "1@s.whatsapp.net"}], "connected"
    monkeypatch.setattr(wa, "_owns_port", lambda: True)
    assert wa.send("1@s.whatsapp.net", "hola")          # read-only
    whatsapp.settings["whatsapp_mode"] = "write"
    assert wa.send("2@s.whatsapp.net", "hola")          # unknown chat
    assert wa.send("1@s.whatsapp.net", "  ")
    assert wa.send("1@s.whatsapp.net", "x" * 4001)
    monkeypatch.setattr(wa, "_owns_port", lambda: False)
    assert wa.send("1@s.whatsapp.net", "hola")          # the port belongs to another bridge
    assert posted == []
    monkeypatch.setattr(wa, "_owns_port", lambda: True)
    assert wa.send("1@s.whatsapp.net", " hola ") == ""
    assert posted == [{"recipient": "1@s.whatsapp.net", "message": "hola"}]


def test_router_leaves_other_requests_to_claude():
    assert router.route("pon música de jazz", "es") is None


def test_is_ack_only_short_filler():
    assert router.is_ack("ah vale, gracias")
    assert router.is_ack("")
    assert not router.is_ack("pon el temporizador de diez minutos")


@pytest.mark.parametrize("text, expected", [
    ("pon un temporizador de 10 minutos para la pasta", (600, "pasta")),
    ("temporizador de 2 horas", (7200, "")),
    ("pon un temporizador de 0 segundos", None),
    ("hola qué tal", None),
])
def test_timer_parsing(text, expected):
    assert timers.parse(text) == expected


def test_nearest_zone_picks_the_monitor_half_under_the_point():
    mons = [{"x": 0, "y": 0, "w": 1920, "h": 1080}]
    zones = build_zones(mons)
    assert nearest_zone_id(100, 500, zones) != nearest_zone_id(1800, 500, zones)
