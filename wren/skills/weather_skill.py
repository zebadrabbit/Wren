import asyncio
import logging
import re
import time

import httpx

from .. import config
from ..channel import Ctx

INTENTS = ["get_weather"]
PLUGIN_NAME = "Weather"

PROMPT_GUIDELINES = """- get_weather: user asks about the weather, temperature, rain or the forecast for here, now, today or tomorrow"""

_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT = 10.0
_TTL = 600

# WMO 4677 codes, the ones Open-Meteo actually emits, in plain words.
_WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "foggy",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "rain showers", 81: "rain showers", 82: "heavy rain showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}


def is_active() -> bool:
    return config.WEATHER_LAT is not None and config.WEATHER_LON is not None


def inactive_reason() -> str:
    return "WEATHER_LAT / WEATHER_LON are not set — weather is disabled."


def _imperial() -> bool:
    return config.WEATHER_UNITS == "fahrenheit"


# (lat, lon, units) -> (monotonic time fetched, payload). Open-Meteo updates
# hourly; ten minutes is plenty and keeps a chatty household off their API.
_cache: dict[tuple, tuple[float, dict]] = {}


def forecast() -> dict:
    """Blocking; call under to_thread."""
    key = (config.WEATHER_LAT, config.WEATHER_LON, config.WEATHER_UNITS)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    resp = httpx.get(_URL, params={
        "latitude": config.WEATHER_LAT,
        "longitude": config.WEATHER_LON,
        "current": "temperature_2m,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": config.TIMEZONE,
        "forecast_days": 2,
        "temperature_unit": config.WEATHER_UNITS,
        "wind_speed_unit": "mph" if _imperial() else "kmh",
    }, timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    _cache[key] = (now, data)
    return data


def _words(code) -> str:
    return _WMO.get(code, "unsettled")


def summary(day: int = 0) -> str:
    """One line. day 0 is now + today's range; day 1 is tomorrow."""
    data = forecast()
    daily = data["daily"]
    unit = "°F" if _imperial() else "°C"
    hi, lo = round(daily["temperature_2m_max"][day]), round(daily["temperature_2m_min"][day])
    pop = daily["precipitation_probability_max"][day]
    rain = f", {pop}% chance of rain" if pop is not None else ""
    if day == 0:
        cur = data["current"]
        wind_unit = "mph" if _imperial() else "km/h"
        return (f"{round(cur['temperature_2m'])}{unit} and {_words(cur['weather_code'])}, "
                f"wind {round(cur['wind_speed_10m'])} {wind_unit}. High {hi}, low {lo}{rain}.")
    return f"Tomorrow: high {hi}, low {lo}, {_words(daily['weather_code'][day])}{rain}."


_TOMORROW = re.compile(r"\btomorrow\b", re.I)


async def handle(intent: str, ctx: Ctx) -> None:
    if not is_active():
        await ctx.channel.send("Weather isn't configured — set WEATHER_LAT and WEATHER_LON.")
        return
    day = 1 if _TOMORROW.search(ctx.text or "") else 0
    try:
        text = await asyncio.to_thread(summary, day)
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as e:
        logging.warning(f"weather: {type(e).__name__}: {e}")
        await ctx.channel.send("Couldn't reach the weather service.")
        return
    await ctx.channel.send(text)
