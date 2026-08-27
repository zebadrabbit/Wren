import os
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")
os.environ.setdefault("TIMEZONE", "UTC")

import asyncio
from unittest.mock import MagicMock, patch

import httpx
import pytest

from wren import config
from wren.channel import Ctx, CollectingChannel
from wren.skills import weather_skill

PAYLOAD = {
    "current": {"temperature_2m": 71.6, "weather_code": 2, "wind_speed_10m": 8.3},
    "daily": {
        "weather_code": [2, 1],
        "temperature_2m_max": [81.2, 78.9],
        "temperature_2m_min": [63.4, 61.0],
        "precipitation_probability_max": [20, 10],
    },
}


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(config, "WEATHER_LAT", 41.88)
    monkeypatch.setattr(config, "WEATHER_LON", -87.63)
    monkeypatch.setattr(config, "WEATHER_UNITS", "fahrenheit")
    weather_skill._cache.clear()


def _response(payload, status=200):
    resp = MagicMock()
    resp.json.return_value = payload
    if status >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "boom", request=MagicMock(), response=resp)
    return resp


def test_inactive_without_coordinates(monkeypatch):
    monkeypatch.setattr(config, "WEATHER_LAT", None)
    assert weather_skill.is_active() is False
    assert "WEATHER_LAT" in weather_skill.inactive_reason()


def test_forecast_asks_open_meteo_in_the_configured_zone_and_units():
    with patch.object(weather_skill.httpx, "get", return_value=_response(PAYLOAD)) as get:
        weather_skill.forecast()
    params = get.call_args.kwargs["params"]
    assert params["latitude"] == 41.88 and params["longitude"] == -87.63
    assert params["timezone"] == config.TIMEZONE
    assert params["temperature_unit"] == "fahrenheit"
    assert params["wind_speed_unit"] == "mph"
    assert params["forecast_days"] == 2


def test_forecast_is_cached(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(weather_skill.time, "monotonic", lambda: clock[0])
    with patch.object(weather_skill.httpx, "get", return_value=_response(PAYLOAD)) as get:
        weather_skill.forecast()
        weather_skill.forecast()
        clock[0] += 601
        weather_skill.forecast()
    assert get.call_count == 2


def test_summary_today():
    with patch.object(weather_skill.httpx, "get", return_value=_response(PAYLOAD)):
        assert weather_skill.summary() == \
            "72°F and partly cloudy, wind 8 mph. High 81, low 63, 20% chance of rain."


def test_summary_tomorrow():
    with patch.object(weather_skill.httpx, "get", return_value=_response(PAYLOAD)):
        assert weather_skill.summary(1) == "Tomorrow: high 79, low 61, mostly clear, 10% chance of rain."


def test_summary_in_celsius_and_without_precipitation_data(monkeypatch):
    monkeypatch.setattr(config, "WEATHER_UNITS", "celsius")
    payload = {**PAYLOAD, "daily": {**PAYLOAD["daily"], "precipitation_probability_max": [None, None]}}
    with patch.object(weather_skill.httpx, "get", return_value=_response(payload)):
        assert weather_skill.summary() == "72°C and partly cloudy, wind 8 km/h. High 81, low 63."


def test_unknown_weather_code_has_a_word():
    payload = {**PAYLOAD, "current": {**PAYLOAD["current"], "weather_code": 42}}
    with patch.object(weather_skill.httpx, "get", return_value=_response(payload)):
        assert "unsettled" in weather_skill.summary()


def _ctx(text):
    return Ctx(user_id=1, channel=CollectingChannel(), text=text)


def test_handle_now_and_tomorrow():
    with patch.object(weather_skill.httpx, "get", return_value=_response(PAYLOAD)):
        ctx = _ctx("what's the weather")
        asyncio.run(weather_skill.handle("get_weather", ctx))
        assert ctx.channel.sent[0].startswith("72°F")
        ctx = _ctx("what's the weather tomorrow")
        asyncio.run(weather_skill.handle("get_weather", ctx))
        assert ctx.channel.sent[0].startswith("Tomorrow:")


def test_handle_reports_unconfigured_without_fetching(monkeypatch):
    # F3: handle() must check is_active() itself -- otherwise an unconfigured
    # weather skill would make a live request with latitude=&longitude=.
    monkeypatch.setattr(config, "WEATHER_LAT", None)
    ctx = _ctx("what's the weather")
    with patch.object(weather_skill.httpx, "get") as get:
        asyncio.run(weather_skill.handle("get_weather", ctx))
    get.assert_not_called()
    assert ctx.channel.sent == ["Weather isn't configured — set WEATHER_LAT and WEATHER_LON."]


def test_handle_reports_a_dead_service():
    with patch.object(weather_skill.httpx, "get", side_effect=httpx.ConnectError("nope")):
        ctx = _ctx("weather?")
        asyncio.run(weather_skill.handle("get_weather", ctx))
    assert ctx.channel.sent == ["Couldn't reach the weather service."]


def test_web_search_guideline_no_longer_claims_weather():
    from wren.skills import web_skill
    assert "weather" not in web_skill.PROMPT_GUIDELINES.lower()


def test_weather_settings_are_labelled_in_the_panel():
    # M3: two independent substrings (key present, "Day" present somewhere)
    # would still pass if the key were labelled under a different group --
    # assert the joined form, as the calendar/briefing panel tests do, so the
    # key and its group are actually adjacent.
    from pathlib import Path
    page = (Path(__file__).parent.parent / "wren" / "communication" / "chat.html").read_text()
    for key in ("WEATHER_LAT", "WEATHER_LON", "WEATHER_UNITS"):
        assert f'{key}: {{ group: "Day"' in page


def test_weather_coordinate_fields_carry_their_own_min_max_and_settingrow_reads_them():
    # Latitude/longitude go negative, so a hardcoded HTML5 min=5 (the
    # poll-seconds default) would mark -87.63 :invalid and hide the minus key
    # on mobile numeric keypads. settingRow() must read min/max/step from
    # SETTING_META per-field instead of hardcoding 5.
    from pathlib import Path
    page = (Path(__file__).parent.parent / "wren" / "communication" / "chat.html").read_text()
    assert "min: -90" in page
    assert "min: -180" in page
    assert "min: 0.01" in page       # MEMORY_DEDUP_THRESHOLD -- fixed by the briefing wave; "min: 0" alone was a substring of this and passed vacuously
    assert "input.min = 5;" not in page


def test_handle_reports_unreachable_on_a_non_json_body():
    # A 200 with a non-JSON body (e.g. a CDN error page) makes resp.json()
    # raise json.JSONDecodeError, a ValueError -- must be treated the same
    # as "couldn't reach the service", not propagate out of the skill.
    resp = MagicMock()
    resp.json.side_effect = ValueError("not json")
    with patch.object(weather_skill.httpx, "get", return_value=resp):
        ctx = _ctx("weather?")
        asyncio.run(weather_skill.handle("get_weather", ctx))
    assert ctx.channel.sent == ["Couldn't reach the weather service."]
