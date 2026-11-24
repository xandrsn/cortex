"""Weather for the panel, from Open-Meteo (free, no key). Cached for 15 minutes."""

import logging
import time

import httpx

from .config import settings

log = logging.getLogger("weather")

URL = "https://api.open-meteo.com/v1/forecast"
# WMO codes -> (text, icon)
CODES = {
    0: ("Clear", "sun"), 1: ("Mostly clear", "sun"), 2: ("Partly cloudy", "cloud-sun"), 3: ("Overcast", "cloud"),
    45: ("Fog", "fog"), 48: ("Fog", "fog"),
    51: ("Drizzle", "drizzle"), 53: ("Drizzle", "drizzle"), 55: ("Drizzle", "drizzle"),
    56: ("Freezing drizzle", "drizzle"), 57: ("Freezing drizzle", "drizzle"),
    61: ("Light rain", "rain"), 63: ("Rain", "rain"), 65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "rain"), 67: ("Freezing rain", "rain"),
    71: ("Light snow", "snow"), 73: ("Snow", "snow"), 75: ("Heavy snow", "snow"), 77: ("Snow", "snow"),
    80: ("Showers", "rain"), 81: ("Showers", "rain"), 82: ("Heavy showers", "rain"),
    85: ("Snow showers", "snow"), 86: ("Snow showers", "snow"),
    95: ("Thunderstorm", "storm"), 96: ("Thunderstorm with hail", "storm"), 99: ("Thunderstorm with hail", "storm"),
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
        log.warning("Could not read the weather: %s", e)
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
