import asyncio
import logging
import re
import time

import httpx

from .. import config
from ..channel import Ctx

INTENTS = ["get_weather", "set_location", "locate_me"]
PLUGIN_NAME = "Weather"

PROMPT_GUIDELINES = """- get_weather: user asks about the weather, temperature, rain or the forecast for here, now, today or tomorrow
- set_location: user says where they live or wants the weather location changed (e.g. "my location is 72715", "set my location to Bella Vista, AR", "my location is 36.47, -94.27"); content is the place, zip code or coordinates exactly as given
- locate_me: user wants Wren to use the device's current location (e.g. "use my current location", "get my location", "where am I")"""

_URL = "https://api.open-meteo.com/v1/forecast"
_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
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
_COORDS = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
# With a weather exchange in history, a small model reads "my location is
# 72715" as a follow-up and emits get_weather (3/3 live, 2026-09-26). The
# words are unambiguous, so handle() reroutes on them the way it reads
# "tomorrow" -- the classifier proposes, the skill disposes.
_SET_LOC = re.compile(r"\b(?:my location is|set (?:my |the )?location to|location is)\s*(.+)$", re.I)
_LOCATE = re.compile(r"\b(?:use|get|find|detect)\s+my\s+(?:current\s+)?location\b|\bwhere am i\b", re.I)


def geocode(place: str) -> tuple[float, float, str] | None:
    """Blocking; call under to_thread. (lat, lon, label) for the best match,
    None when Open-Meteo knows no such place."""
    params: dict = {"name": place, "count": 1}
    if place.isdigit() and len(place) == 5:
        # ponytail: a bare five-digit code is a US zip until someone abroad
        # complains; other countries' postcodes still match by name unbiased
        params["countryCode"] = "US"
    resp = httpx.get(_GEOCODE_URL, params=params, timeout=_TIMEOUT)
    resp.raise_for_status()
    hits = resp.json().get("results") or []
    if not hits:
        return None
    hit = hits[0]
    label = ", ".join(str(hit[k]) for k in ("name", "admin1", "country_code") if hit.get(k))
    return float(hit["latitude"]), float(hit["longitude"]), label


async def _set_location(ctx: Ctx) -> None:
    place = (ctx.content or "").strip()
    if not place:
        await ctx.channel.send("Where? A town, a zip code, or coordinates.")
        return
    m = _COORDS.match(place)
    if m:
        lat, lon, label = float(m.group(1)), float(m.group(2)), f"{m.group(1)}, {m.group(2)}"
    else:
        try:
            found = await asyncio.to_thread(geocode, place)
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as e:
            logging.warning(f"weather geocode: {type(e).__name__}: {e}")
            await ctx.channel.send("Couldn't reach the weather service.")
            return
        if found is None:
            await ctx.channel.send("Couldn't find that place.")
            return
        lat, lon, label = found
    # set_override validates each key on its own, so check both before
    # writing either: a bad longitude must not leave a new latitude behind.
    try:
        config.SETTABLE["WEATHER_LAT"].coerce(str(lat))
        config.SETTABLE["WEATHER_LON"].coerce(str(lon))
    except ValueError as e:
        await ctx.channel.send(str(e))
        return
    config.set_override("WEATHER_LAT", str(lat))
    config.set_override("WEATHER_LON", str(lon))
    await ctx.channel.send(f"Weather location set to {label}.")


async def handle(intent: str, ctx: Ctx) -> None:
    if intent in ("get_weather", "set_location"):
        # set_location too: the model has answered "use my current location"
        # with set_location and a zip lifted from history, so the skill
        # geocoded a stale place instead of asking the device.
        if _LOCATE.search(ctx.text or ""):
            intent = "locate_me"
        elif m := _SET_LOC.search(ctx.text or ""):
            intent = "set_location"
            ctx.content = (ctx.content or "").strip() or m.group(1)
    if intent in ("set_location", "locate_me"):
        if ctx.user_id != config.WHITELIST["owner"]:
            await ctx.channel.send("Only the owner can change the location.")
            return
        if config.ENV_DEFINED & {"WEATHER_LAT", "WEATHER_LON"}:
            # set_override would refuse anyway; said here so no geocode or
            # browser location prompt is spent on a value that cannot be kept
            await ctx.channel.send("Your location is set in .env (WEATHER_LAT / WEATHER_LON). "
                                   "Change it there and restart me.")
            return
        if intent == "set_location":
            await _set_location(ctx)
        else:
            # Wren cannot poll a device; the surface has to push. The web chat
            # draws this card and asks the browser; everything else prints the
            # prose, which tells the user which door does work.
            await ctx.channel.send_card(
                "locate", {},
                "Share your location from Telegram's attachment menu, or say this in the web chat.")
        return
    if not is_active():
        await ctx.channel.send('Weather isn\'t configured — tell me where you are, e.g. "my location is 72715".')
        return
    day = 1 if _TOMORROW.search(ctx.text or "") else 0
    try:
        text = await asyncio.to_thread(summary, day)
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as e:
        logging.warning(f"weather: {type(e).__name__}: {e}")
        await ctx.channel.send("Couldn't reach the weather service.")
        return
    await ctx.channel.send(text)
