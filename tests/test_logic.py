"""Pruebas de lógica pura: clasificación de gestos, órdenes del router, temporizadores y zonas de ventana."""

import math

import pytest

from gesture.gesture_detector import GestureDetector
from gesture.snap_zones import build_zones, nearest_zone_id
from pepito import router, timers


def _hand(finger_ratios: dict[int, float]) -> dict[int, tuple[float, float]]:
    """Mano sintética: muñeca en (0,0); cada dedo (tip, mcp) a distancia 1.0 del origen en su propio ángulo.
    finger_ratios[tip_idx] = distancia de la punta / distancia del mcp (la métrica que usa el detector)."""
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
    assert not det._detect_peace(_hand(OPEN))   # ring y meñique extendidos: no es ✌
    assert not det._detect_peace(_hand(FIST))


def test_missing_wrist_is_never_a_gesture():
    det = GestureDetector()
    pos = _hand(OPEN)
    del pos[0]
    assert not det._detect_fist(pos) and not det._detect_open(pos) and not det._detect_peace(pos)


@pytest.mark.parametrize("text", ["Pepito, ¿qué hora es?", "que hora es", "la hora"])
def test_router_answers_time_questions(text):
    answer = router.route(text, "es")
    assert answer and answer.startswith("Son las ")


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
