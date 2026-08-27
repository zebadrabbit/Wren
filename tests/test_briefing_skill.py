import os
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")
os.environ.setdefault("TIMEZONE", "UTC")

import asyncio
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from wren import brain, config, registry, settings
from wren.channel import Ctx, CollectingChannel
from wren.skills import briefing_skill, calendar_skill, weather_skill
from wren.skills import reminders_store as reminders
from wren.skills import shopping_store as shopping
from wren.skills.calendar_feed import Event

NOW = datetime(2026, 8, 26, 7, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def stores(monkeypatch):
    settings.init_db()
    reminders.init_db()
    shopping.init_db()
    monkeypatch.setattr(briefing_skill, "_now", lambda: NOW)
    monkeypatch.setattr(config, "CALENDAR_URLS", ["https://cal.example/x.ics"])
    monkeypatch.setattr(config, "WEATHER_LAT", 1.0)
    monkeypatch.setattr(config, "WEATHER_LON", 2.0)
    # the briefing must never call the model
    monkeypatch.setattr(brain, "chat", lambda *a, **k: pytest.fail("briefing called brain.chat"))


def _event(summary, hour):
    start = datetime(2026, 8, 26, hour, tzinfo=timezone.utc)
    return Event(summary, start, start + timedelta(hours=1), False)


def test_full_briefing_in_order():
    reminders.save(1, "call the vet", "2026-08-26T17:00:00+00:00")
    reminders.save(1, "next week thing", "2026-09-02T09:00:00+00:00")
    shopping.add("milk", "owner")
    shopping.add("eggs", "owner")
    with patch.object(weather_skill, "summary", return_value="72°F and clear."), \
         patch.object(calendar_skill, "events_between", return_value=[_event("Standup", 9)]):
        text = briefing_skill.compose(1)
    assert text == (
        "Good morning. Wed Aug 26.\n"
        "Weather: 72°F and clear.\n"
        "Calendar:\n"
        "  09:00–10:00  Standup\n"
        "Reminders today:\n"
        "  17:00  call the vet\n"
        "Shopping list: 2 items.")


def test_everything_empty():
    with patch.object(weather_skill, "summary", return_value="72°F and clear."), \
         patch.object(calendar_skill, "events_between", return_value=[]):
        text = briefing_skill.compose(1)
    assert text == "Good morning. Wed Aug 26.\nWeather: 72°F and clear.\nNothing on the calendar, no reminders, list is empty."


def test_unconfigured_sources_are_omitted_not_apologised_for(monkeypatch):
    monkeypatch.setattr(config, "CALENDAR_URLS", [])
    monkeypatch.setattr(config, "WEATHER_LAT", None)
    shopping.add("milk", "owner")
    text = briefing_skill.compose(1)
    assert text == "Good morning. Wed Aug 26.\nShopping list: 1 item."


def test_a_dead_feed_degrades_to_one_line():
    with patch.object(weather_skill, "summary", side_effect=RuntimeError("down")), \
         patch.object(calendar_skill, "events_between", side_effect=calendar_skill.FeedError("cal.example: ConnectError")):
        text = briefing_skill.compose(1)
    assert "Weather: couldn't reach the weather service." in text
    assert "Calendar: couldn't reach the feed." in text


def test_disabled_skill_is_skipped():
    registry.set_enabled(calendar_skill, False)
    try:
        with patch.object(weather_skill, "summary", return_value="72°F."), \
             patch.object(calendar_skill, "events_between", side_effect=AssertionError("should not fetch")):
            text = briefing_skill.compose(1)
    finally:
        registry.set_enabled(calendar_skill, True)
    assert "Calendar" not in text


def test_greeting_follows_the_clock(monkeypatch):
    monkeypatch.setattr(config, "CALENDAR_URLS", [])
    monkeypatch.setattr(config, "WEATHER_LAT", None)
    monkeypatch.setattr(briefing_skill, "_now", lambda: NOW.replace(hour=15))
    assert briefing_skill.compose(1).startswith("Good afternoon.")
    monkeypatch.setattr(briefing_skill, "_now", lambda: NOW.replace(hour=20))
    assert briefing_skill.compose(1).startswith("Good evening.")


def test_reminders_today_use_the_local_day(monkeypatch):
    monkeypatch.setattr(config, "TIMEZONE", "America/Chicago")
    monkeypatch.setattr(config, "CALENDAR_URLS", [])
    monkeypatch.setattr(config, "WEATHER_LAT", None)
    # _now is UTC here on purpose: compose must convert to TIMEZONE itself
    monkeypatch.setattr(briefing_skill, "_now", lambda: datetime(2026, 8, 26, 12, 30, tzinfo=timezone.utc))
    reminders.save(1, "late one", "2026-08-27T03:00:00+00:00")     # 22:00 Chicago, still the 26th
    assert "  22:00  late one" in briefing_skill.compose(1)


def test_handle_sends_the_briefing():
    ctx = Ctx(user_id=1, channel=CollectingChannel(), text="what's my day look like")
    with patch.object(briefing_skill, "compose", return_value="Good morning."):
        asyncio.run(briefing_skill.handle("briefing", ctx))
    assert ctx.channel.sent == ["Good morning."]
