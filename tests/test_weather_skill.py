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
    assert ctx.channel.sent == ['Weather isn\'t configured — tell me where you are, e.g. "my location is 72715".']


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


# ── set_location / locate_me ──────────────────────────────────────────────

GEOCODE = {"results": [{"name": "Bella Vista", "admin1": "Arkansas", "country_code": "US",
                        "latitude": 36.4814, "longitude": -94.2733}]}


@pytest.fixture
def scratch_settings():
    from wren import settings
    settings.init_db()
    yield
    for key in ("WEATHER_LAT", "WEATHER_LON"):
        config.clear_override(key)


def _owner(text, content):
    return Ctx(user_id=config.WHITELIST["owner"], channel=CollectingChannel(), text=text, content=content)


def test_set_location_geocodes_a_place_and_persists_it(scratch_settings):
    from wren import settings
    ctx = _owner("my location is bella vista, ar", "bella vista, ar")
    with patch.object(weather_skill.httpx, "get", return_value=_response(GEOCODE)) as get:
        asyncio.run(weather_skill.handle("set_location", ctx))
    assert get.call_args.args[0] == weather_skill._GEOCODE_URL
    assert get.call_args.kwargs["params"]["name"] == "bella vista, ar"
    assert "countryCode" not in get.call_args.kwargs["params"]
    assert (config.WEATHER_LAT, config.WEATHER_LON) == (36.4814, -94.2733)
    assert settings.get("WEATHER_LAT") == "36.4814"     # survives a restart
    assert ctx.channel.sent == ["Weather location set to Bella Vista, Arkansas, US."]


def test_set_location_biases_a_bare_us_zip(scratch_settings):
    ctx = _owner("my location is 72715", "72715")
    with patch.object(weather_skill.httpx, "get", return_value=_response(GEOCODE)) as get:
        asyncio.run(weather_skill.handle("set_location", ctx))
    assert get.call_args.kwargs["params"]["countryCode"] == "US"


def test_set_location_accepts_raw_coordinates_without_geocoding(scratch_settings):
    ctx = _owner("my location is 36.47, -94.27", "36.47, -94.27")
    with patch.object(weather_skill.httpx, "get") as get:
        asyncio.run(weather_skill.handle("set_location", ctx))
    get.assert_not_called()
    assert (config.WEATHER_LAT, config.WEATHER_LON) == (36.47, -94.27)
    assert ctx.channel.sent == ["Weather location set to 36.47, -94.27."]


def test_set_location_rejects_out_of_range_coordinates(scratch_settings, monkeypatch):
    monkeypatch.setattr(config, "WEATHER_LAT", None)
    ctx = _owner("my location is 95, 10", "95, 10")
    asyncio.run(weather_skill.handle("set_location", ctx))
    assert config.WEATHER_LAT is None
    assert "between -90.0 and 90.0" in ctx.channel.sent[0]


def test_set_location_reports_no_match(scratch_settings, monkeypatch):
    monkeypatch.setattr(config, "WEATHER_LAT", None)
    ctx = _owner("my location is xyzzy", "xyzzy")
    with patch.object(weather_skill.httpx, "get", return_value=_response({})):
        asyncio.run(weather_skill.handle("set_location", ctx))
    assert config.WEATHER_LAT is None
    assert ctx.channel.sent == ["Couldn't find that place."]


def test_set_location_reports_a_dead_geocoder():
    ctx = _owner("my location is bella vista", "bella vista")
    with patch.object(weather_skill.httpx, "get", side_effect=httpx.ConnectError("nope")):
        asyncio.run(weather_skill.handle("set_location", ctx))
    assert ctx.channel.sent == ["Couldn't reach the weather service."]


def test_set_location_with_nothing_to_set_asks():
    ctx = _owner("my location is", "")
    asyncio.run(weather_skill.handle("set_location", ctx))
    assert ctx.channel.sent == ["Where? A town, a zip code, or coordinates."]


def test_set_location_is_owner_only():
    ctx = Ctx(user_id=config.WHITELIST["owner"] + 1, channel=CollectingChannel(), content="72715")
    with patch.object(weather_skill.httpx, "get") as get:
        asyncio.run(weather_skill.handle("set_location", ctx))
    get.assert_not_called()
    assert ctx.channel.sent == ["Only the owner can change the location."]


def test_locate_me_sends_a_locate_card_with_prose_fallback():
    ctx = _owner("use my current location", "")
    asyncio.run(weather_skill.handle("locate_me", ctx))
    assert [c["kind"] for c in ctx.channel.cards] == ["locate"]
    # Discord/Telegram print the prose; it must tell the user what to do instead
    assert "Telegram" in ctx.channel.sent[0] and "web chat" in ctx.channel.sent[0]


def test_locate_me_is_owner_only():
    ctx = Ctx(user_id=config.WHITELIST["owner"] + 1, channel=CollectingChannel())
    asyncio.run(weather_skill.handle("locate_me", ctx))
    assert ctx.channel.cards == []


def test_location_intents_are_declared_and_guided():
    assert {"set_location", "locate_me"} <= set(weather_skill.INTENTS)
    assert "set_location" in weather_skill.PROMPT_GUIDELINES
    assert "locate_me" in weather_skill.PROMPT_GUIDELINES


def test_web_chat_draws_the_locate_card_with_browser_geolocation():
    from pathlib import Path
    page = (Path(__file__).parent.parent / "wren" / "communication" / "chat.html").read_text()
    assert "locate: locateCard" in page
    assert "navigator.geolocation.getCurrentPosition" in page


# With "what's the weather" / "not configured" in history, gemma classifies the
# very next "my location is 72715" as get_weather -- reproduced 3/3 live on
# 2026-09-26. The words are unambiguous, so the skill reroutes on them.
def test_get_weather_with_location_words_is_rerouted_to_set_location(scratch_settings):
    ctx = _owner("my location is 72715", "72715")
    with patch.object(weather_skill.httpx, "get", return_value=_response(GEOCODE)):
        asyncio.run(weather_skill.handle("get_weather", ctx))
    assert ctx.channel.sent == ["Weather location set to Bella Vista, Arkansas, US."]


def test_rerouted_set_location_takes_the_place_from_the_words_when_content_is_empty(scratch_settings):
    ctx = _owner("set my location to bella vista, ar", "")
    with patch.object(weather_skill.httpx, "get", return_value=_response(GEOCODE)) as get:
        asyncio.run(weather_skill.handle("get_weather", ctx))
    assert get.call_args.kwargs["params"]["name"] == "bella vista, ar"


def test_get_weather_with_locate_words_is_rerouted_to_locate_me():
    ctx = _owner("use my current location", "")
    asyncio.run(weather_skill.handle("locate_me", ctx))
    ctx = _owner("use my current location", "")
    with patch.object(weather_skill.httpx, "get") as get:
        asyncio.run(weather_skill.handle("get_weather", ctx))
    get.assert_not_called()
    assert [c["kind"] for c in ctx.channel.cards] == ["locate"]


def test_unconfigured_reply_says_what_to_type(monkeypatch):
    monkeypatch.setattr(config, "WEATHER_LAT", None)
    ctx = _ctx("what's the weather")
    asyncio.run(weather_skill.handle("get_weather", ctx))
    assert ctx.channel.sent == ['Weather isn\'t configured — tell me where you are, e.g. "my location is 72715".']


def test_set_location_with_locate_words_is_rerouted_even_when_content_was_invented():
    # Live 2026-09-26: "use my current location" in the web chat came back as
    # set_location with content "72715" lifted from history, so the skill
    # geocoded a stale zip instead of asking the browser.
    ctx = _owner("use my current location", "72715")
    with patch.object(weather_skill.httpx, "get") as get:
        asyncio.run(weather_skill.handle("set_location", ctx))
    get.assert_not_called()
    assert [c["kind"] for c in ctx.channel.cards] == ["locate"]


@pytest.mark.parametrize("intent,text", [("set_location", "my location is 72715"),
                                         ("locate_me", "use my current location")])
def test_location_set_in_env_is_not_changed_from_chat(scratch_settings, monkeypatch, intent, text):
    # .env is the source of truth for what it defines: say so up front, before
    # geocoding or asking the browser for a fix that could never be saved.
    monkeypatch.setattr(config, "ENV_DEFINED", frozenset({"WEATHER_LAT", "WEATHER_LON"}))
    ctx = _owner(text, "72715" if intent == "set_location" else "")
    with patch.object(weather_skill.httpx, "get") as get:
        asyncio.run(weather_skill.handle(intent, ctx))
    get.assert_not_called()
    assert len(ctx.channel.sent) == 1 and ".env" in ctx.channel.sent[0]
