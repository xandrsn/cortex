"""El tiempo para el panel, de Open-Meteo (gratis y sin clave). Se cachea 15 minutos."""

import logging
import time

import httpx

from .config import settings

log = logging.getLogger("weather")

URL = "https://api.open-meteo.com/v1/forecast"
# Códigos WMO → (texto, icono)
CODES = {
    0: ("Despejado", "sun"), 1: ("Casi despejado", "sun"), 2: ("Algunas nubes", "cloud-sun"), 3: ("Nublado", "cloud"),
    45: ("Niebla", "fog"), 48: ("Niebla", "fog"),
    51: ("Llovizna", "drizzle"), 53: ("Llovizna", "drizzle"), 55: ("Llovizna", "drizzle"),
    56: ("Llovizna helada", "drizzle"), 57: ("Llovizna helada", "drizzle"),
    61: ("Lluvia débil", "rain"), 63: ("Lluvia", "rain"), 65: ("Lluvia fuerte", "rain"),
    66: ("Lluvia helada", "rain"), 67: ("Lluvia helada", "rain"),
    71: ("Nieve débil", "snow"), 73: ("Nieve", "snow"), 75: ("Nieve fuerte", "snow"), 77: ("Nieve", "snow"),
    80: ("Chubascos", "rain"), 81: ("Chubascos", "rain"), 82: ("Chubascos fuertes", "rain"),
    85: ("Chubascos de nieve", "snow"), 86: ("Chubascos de nieve", "snow"),
    95: ("Tormenta", "storm"), 96: ("Tormenta con granizo", "storm"), 99: ("Tormenta con granizo", "storm"),
}

_cache: dict = {}


def forecast() -> dict:
    place = settings["weather_place"]
    key = (place["lat"], place["lon"])
    if _cache.get("key") == key and time.time() - _cache.get("at", 0) < 900:
        return _cache["data"]
    try:
        r = httpx.get(URL, timeout=8, params={
            "latitude": place["lat"], "longitude": place["lon"], "timezone": "Europe/Madrid", "forecast_days": 4,
            "current": "temperature_2m,apparent_temperature,weather_code,is_day,wind_speed_10m,relative_humidity_2m",
            "hourly": "temperature_2m,precipitation_probability,weather_code,is_day",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset",
        })
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        log.warning("No pude leer el tiempo: %s", e)
        return _cache.get("data") or {"ok": False}
    cur = d["current"]
    now_hour = cur["time"][:13]
    hours = d["hourly"]
    start = next((i for i, t in enumerate(hours["time"]) if t[:13] >= now_hour), 0)
    data = {
        "ok": True,
        "place": place["name"],
        "temp": round(cur["temperature_2m"]),
        "feels": round(cur["apparent_temperature"]),
        "text": CODES.get(cur["weather_code"], ("", "cloud"))[0],
        "icon": CODES.get(cur["weather_code"], ("", "cloud"))[1],
        "day": bool(cur["is_day"]),
        "wind": round(cur["wind_speed_10m"]),
        "humidity": cur["relative_humidity_2m"],
        "hours": [{"hour": hours["time"][i][11:16], "temp": round(hours["temperature_2m"][i]),
                   "rain": hours["precipitation_probability"][i], "day": bool(hours["is_day"][i]), "icon": CODES.get(hours["weather_code"][i], ("", "cloud"))[1]}
                  for i in range(start, min(start + 12, len(hours["time"])), 2)],
        "days": [{"date": t, "max": round(mx), "min": round(mn), "rain": p, "icon": CODES.get(c, ("", "cloud"))[1],
                  "text": CODES.get(c, ("", ""))[0]}
                 for t, mx, mn, p, c in zip(d["daily"]["time"], d["daily"]["temperature_2m_max"],
                                             d["daily"]["temperature_2m_min"], d["daily"]["precipitation_probability_max"],
                                             d["daily"]["weather_code"])],
    }
    _cache.update(key=key, at=time.time(), data=data)
    return data
