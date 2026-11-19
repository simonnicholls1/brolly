"""Weather from Open-Meteo (free, no API key), with in-memory caching."""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import httpx

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

CURRENT_TTL_S = 10 * 60
CLIMATE_TTL_S = 24 * 60 * 60
CLIMATE_YEARS = 10
CLIMATE_WINDOW_DAYS = 7  # +/- days around today's date in previous years


class TTLCache:
    def __init__(self, ttl: float, max_items: int = 5000):
        self.ttl, self.max_items, self._d = ttl, max_items, {}

    def get(self, key):
        hit = self._d.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        self._d.pop(key, None)
        return None

    def set(self, key, value):
        if len(self._d) >= self.max_items:
            self._d.clear()
        self._d[key] = (time.time() + self.ttl, value)


def _doy_distance(a: date, b: date) -> int:
    d = abs(a.timetuple().tm_yday - b.timetuple().tm_yday)
    return min(d, 365 - d)


def summarise_climate(daily: dict, today: date) -> dict | None:
    """Average the same calendar window over past years."""
    temps, feels, wet, years = [], [], 0, set()
    for i, day in enumerate(daily.get("time", [])):
        d = date.fromisoformat(day)
        if _doy_distance(d, today) > CLIMATE_WINDOW_DAYS:
            continue
        t = daily["temperature_2m_mean"][i]
        if t is None:
            continue
        temps.append(t)
        f = (daily.get("apparent_temperature_mean") or [None] * (i + 1))[i]
        if f is not None:
            feels.append(f)
        if ((daily.get("precipitation_sum") or [0] * (i + 1))[i] or 0) >= 1:
            wet += 1
        years.add(d.year)
    if not temps:
        return None
    return {
        "temp_mean": round(sum(temps) / len(temps), 1),
        "feels_like_mean": round(sum(feels) / len(feels), 1) if feels else None,
        "wet_day_pct": round(wet / len(temps), 2),
        "years": len(years),
        "days": len(temps),
    }


class OpenMeteo:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(timeout=8.0)
        self._current = TTLCache(CURRENT_TTL_S)
        self._climate = TTLCache(CLIMATE_TTL_S)

    async def current(self, lat: float, lon: float) -> dict:
        key = (round(lat, 2), round(lon, 2))
        if (hit := self._current.get(key)) is not None:
            return hit
        r = await self.client.get(
            FORECAST_URL,
            params={
                "latitude": key[0],
                "longitude": key[1],
                "current": "temperature_2m,apparent_temperature,precipitation,snowfall,"
                "weather_code,cloud_cover,wind_speed_10m,wind_gusts_10m,is_day",
                "hourly": "precipitation_probability",
                "forecast_hours": 4,
                "timezone": "auto",
            },
        )
        r.raise_for_status()
        j = r.json()
        c = j["current"]
        chances = [x for x in (j.get("hourly", {}).get("precipitation_probability") or [])[:3] if x is not None]
        result = {
            "temp": c["temperature_2m"],
            "feels_like": c["apparent_temperature"],
            "precip": c.get("precipitation") or 0,
            "snowfall": c.get("snowfall") or 0,
            "code": c.get("weather_code"),
            "cloud": c.get("cloud_cover"),
            "wind": c.get("wind_speed_10m"),
            "gusts": c.get("wind_gusts_10m"),
            "is_day": bool(c.get("is_day", 1)),
            "rain_chance": max(chances) if chances else None,
            "timezone": j.get("timezone"),
            "utc_offset_s": j.get("utc_offset_seconds", 0),
        }
        self._current.set(key, result)
        return result

    async def climate(self, lat: float, lon: float, today: date) -> dict | None:
        key = (round(lat, 1), round(lon, 1), today.isoformat())
        if (hit := self._climate.get(key)) is not None:
            return hit
        r = await self.client.get(
            ARCHIVE_URL,
            params={
                "latitude": key[0],
                "longitude": key[1],
                "start_date": today.replace(year=today.year - CLIMATE_YEARS).isoformat()
                if not (today.month == 2 and today.day == 29)
                else (today - timedelta(days=365 * CLIMATE_YEARS)).isoformat(),
                "end_date": (today - timedelta(days=8)).isoformat(),  # archive lags a few days
                "daily": "temperature_2m_mean,apparent_temperature_mean,precipitation_sum",
                "timezone": "auto",
            },
        )
        r.raise_for_status()
        result = summarise_climate(r.json().get("daily", {}), today)
        self._climate.set(key, result)
        return result

    async def geocode(self, q: str) -> list[dict]:
        r = await self.client.get(GEOCODE_URL, params={"name": q, "count": 5, "language": "en"})
        r.raise_for_status()
        return [
            {
                "name": p["name"],
                "region": ", ".join(x for x in (p.get("admin1"), p.get("country")) if x),
                "lat": p["latitude"],
                "lon": p["longitude"],
            }
            for p in r.json().get("results", []) or []
        ]


def local_date(weather: dict | None, now: float) -> date:
    offset = (weather or {}).get("utc_offset_s", 0) or 0
    return datetime.fromtimestamp(now + offset, tz=timezone.utc).date()
