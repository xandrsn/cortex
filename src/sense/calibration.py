"""Calibración: Sense mide tu cámara, tu luz y tu mano y adapta los umbrales.

El asistente de la web hace tres pasos; en cada uno se guardan muestras de Vision.state():
  look   → mirando a la pantalla, quieto: luz, fps y el cabeceo que es «mirar» con tu montaje
  hand   → mano abierta a la vista: tamaño de la mano (distancia a la cámara) y mano dominante
  pinch  → juntar y separar pulgar e índice: tus distancias de pellizco cerrado y abierto
suggest() es pura (tests/test_sense.py): muestras → ajustes nuevos + informe.
"""

from statistics import median

REF_HAND = 0.16   # muñeca → nudillo del medio / alto del encuadre, mano a ~60 cm de una webcam 4:3 (estimado)
STEPS = ("look", "hand", "pinch")


def _vals(samples, key):
    return [s[key] for s in samples if s.get(key) is not None]


def suggest(samples: dict[str, list[dict]], desktop: tuple[int, int], defaults: dict) -> tuple[dict, dict]:
    """(cambios de ajustes, informe). Si un paso no tiene muestras útiles, sus ajustes no se tocan."""
    changes, report, notes = {}, {}, []
    look = samples.get("look", [])
    pitch = _vals(look, "pitch")
    if len(pitch) >= 5:
        report["pitch"] = round(median(pitch), 1)
        changes["look_pitch_offset"] = round(report["pitch"])
    else:
        notes.append("No te vi la cara: «solo cuando miro a la pantalla» puede fallar.")
    light = _vals(look, "brightness")
    if light:
        report["brightness"] = round(median(light))
        if report["brightness"] < 50:
            notes.append("Hay poca luz: los gestos fallarán más. Pon una luz delante de ti.")
    fps = _vals(look, "cam_fps")
    if fps:
        report["fps"] = round(median(fps), 1)
        if report["fps"] < 15:
            notes.append("La cámara va lenta (<15 fps): los deslizamientos rápidos pueden no contar.")

    hand = [s for s in samples.get("hand", []) if s.get("hand") and s.get("hand_size")]
    if len(hand) >= 5:
        size = median(_vals(hand, "hand_size"))
        report["hand_size"] = round(size, 3)
        scale = min(1.4, max(0.6, size / REF_HAND))   # mano lejos → movimientos más cortos en el encuadre
        changes["swipe_min_distance"] = round(defaults["swipe_min_distance"] * scale, 3)
        sides = _vals(hand, "side")
        if sides:
            report["side"] = max(set(sides), key=sides.count)
    else:
        notes.append("No vi bien la mano: la distancia de deslizamiento se queda como estaba.")

    pinch = [s["pinch"] for s in samples.get("pinch", []) if s.get("hand") and s.get("pinch")]
    w, h = desktop
    d = sorted(((dx * w) ** 2 + (dy * h) ** 2) ** 0.5 for dx, dy in pinch)
    if len(d) >= 8 and d[-1 - len(d) // 10] - d[len(d) // 10] > 30:
        closed, opened = d[len(d) // 10], d[-1 - len(d) // 10]   # percentiles 10 y 90: sin picos sueltos
        changes["pinch_grab"] = round(closed + 0.35 * (opened - closed))
        changes["pinch_release"] = round(closed + 0.6 * (opened - closed))
        report["pinch"] = [round(closed), round(opened)]
    else:
        notes.append("No vi abrir y cerrar el pellizco: los umbrales de agarrar ventanas se quedan como estaban.")
    report["notes"] = notes
    return changes, report
