import os
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")
os.environ.setdefault("TIMEZONE", "UTC")

import asyncio
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import httpx
import pytest

from wren import config
from wren.channel import Ctx, CollectingChannel
from wren.skills import calendar_skill
from wren.skills.calendar_feed import Event

TZ = ZoneInfo("UTC")
FEED = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:1\nSUMMARY:Standup\n"
        "DTSTART:20260826T090000\nDTEND:20260826T093000\nEND:VEVENT\n"
        "BEGIN:VEVENT\nUID:2\nSUMMARY:Kate in town\nDTSTART;VALUE=DATE:20260826\n"
        "DTEND;VALUE=DATE:20260827\nEND:VEVENT\nEND:VCALENDAR\n")


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(config, "CALENDAR_URLS", ["https://cal.example/private-abc/basic.ics"])
    monkeypatch.setattr(config, "CALENDAR_CACHE_SECONDS", 300)
    calendar_skill._cache.clear()


def _response(text: str, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.text = text
    resp.status_code = status
    if status >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "boom", request=MagicMock(), response=resp)
    return resp


def test_inactive_without_urls(monkeypatch):
    monkeypatch.setattr(config, "CALENDAR_URLS", [])
    assert calendar_skill.is_active() is False
    assert "CALENDAR_URLS" in calendar_skill.inactive_reason()


def test_active_with_a_url():
    assert calendar_skill.is_active() is True


def test_events_between_fetches_every_feed_and_merges(monkeypatch):
    monkeypatch.setattr(config, "CALENDAR_URLS", ["https://a.example/x.ics", "https://b.example/y.ics"])
    with patch.object(calendar_skill.httpx, "get", return_value=_response(FEED)) as get:
        events = calendar_skill.events_between(date(2026, 8, 26), 1)
    assert get.call_count == 2
    assert [e.summary for e in events] == ["Kate in town", "Kate in town", "Standup", "Standup"]


def test_fetch_is_cached_inside_the_ttl():
    with patch.object(calendar_skill.httpx, "get", return_value=_response(FEED)) as get:
        calendar_skill.events_between(date(2026, 8, 26), 1)
        calendar_skill.events_between(date(2026, 8, 26), 1)
    assert get.call_count == 1


def test_cache_expires(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(calendar_skill.time, "monotonic", lambda: clock[0])
    with patch.object(calendar_skill.httpx, "get", return_value=_response(FEED)) as get:
        calendar_skill.events_between(date(2026, 8, 26), 1)
        clock[0] += 301
        calendar_skill.events_between(date(2026, 8, 26), 1)
    assert get.call_count == 2


def test_http_failure_raises_feederror_naming_only_the_host():
    with patch.object(calendar_skill.httpx, "get", return_value=_response("", 503)):
        with pytest.raises(calendar_skill.FeedError) as info:
            calendar_skill.events_between(date(2026, 8, 26), 1)
    assert "cal.example" in str(info.value)
    assert "private-abc" not in str(info.value)


def test_transport_error_is_a_feederror_too():
    with patch.object(calendar_skill.httpx, "get", side_effect=httpx.ConnectError("nope")):
        with pytest.raises(calendar_skill.FeedError):
            calendar_skill.events_between(date(2026, 8, 26), 1)


def _ev(summary, y, m, d, h=None, length_h=1, all_day=False):
    start = datetime(y, m, d, h or 0, tzinfo=TZ)
    end = start + (timedelta(days=1) if all_day else timedelta(hours=length_h))
    return Event(summary, start, end, all_day)


def test_day_lines_puts_all_day_first_and_formats_times():
    events = [_ev("Trip", 2026, 8, 26, all_day=True), _ev("Standup", 2026, 8, 26, 9)]
    assert calendar_skill.day_lines(events, date(2026, 8, 26)) == [
        "  all day      Trip", "  09:00–10:00  Standup"]


def test_format_range_single_day():
    events = [_ev("Standup", 2026, 8, 26, 9)]
    assert calendar_skill.format_range(events, date(2026, 8, 26), 1, "today") == \
        "Today (Wed Aug 26):\n  09:00–10:00  Standup"


def test_format_range_empty():
    assert calendar_skill.format_range([], date(2026, 8, 26), 1, "today") == "Nothing on the calendar today."
    assert calendar_skill.format_range([], date(2026, 8, 24), 7, "this week") == "Nothing on the calendar this week."


def test_format_range_week_skips_empty_days():
    events = [_ev("Standup", 2026, 8, 24, 9), _ev("Dentist", 2026, 8, 27, 13)]
    assert calendar_skill.format_range(events, date(2026, 8, 24), 7, "this week") == \
        "Mon Aug 24:\n  09:00–10:00  Standup\nThu Aug 27:\n  13:00–14:00  Dentist"


def _ctx(text="", when=None):
    return Ctx(user_id=1, channel=CollectingChannel(), text=text, when=when)


def test_window_defaults_to_today(monkeypatch):
    monkeypatch.setattr(calendar_skill, "_today", lambda: date(2026, 8, 26))
    assert calendar_skill._window(_ctx("what's on my calendar")) == (date(2026, 8, 26), 1, "today")


def test_window_tomorrow_and_week(monkeypatch):
    monkeypatch.setattr(calendar_skill, "_today", lambda: date(2026, 8, 26))
    assert calendar_skill._window(_ctx("anything tomorrow?")) == (date(2026, 8, 27), 1, "tomorrow")
    assert calendar_skill._window(_ctx("what does my week look like")) == (date(2026, 8, 26), 7, "this week")


def test_window_uses_the_models_date_when_given(monkeypatch):
    monkeypatch.setattr(calendar_skill, "_today", lambda: date(2026, 8, 26))
    assert calendar_skill._window(_ctx("what's on friday", when="2026-08-28T00:00:00")) == \
        (date(2026, 8, 28), 1, "Fri Aug 28")
    # garbage from the model falls back to today rather than crashing
    assert calendar_skill._window(_ctx("hm", when="friday-ish"))[0] == date(2026, 8, 26)


def test_handle_sends_the_formatted_day(monkeypatch):
    monkeypatch.setattr(calendar_skill, "_today", lambda: date(2026, 8, 26))
    ctx = _ctx("what's on today")
    with patch.object(calendar_skill.httpx, "get", return_value=_response(FEED)):
        asyncio.run(calendar_skill.handle("recall_calendar", ctx))
    assert ctx.channel.sent == ["Today (Wed Aug 26):\n  all day      Kate in town\n  09:00–09:30  Standup"]


def test_handle_reports_a_dead_feed(monkeypatch):
    monkeypatch.setattr(calendar_skill, "_today", lambda: date(2026, 8, 26))
    ctx = _ctx("what's on today")
    with patch.object(calendar_skill.httpx, "get", side_effect=httpx.ConnectError("nope")):
        asyncio.run(calendar_skill.handle("recall_calendar", ctx))
    assert ctx.channel.sent == ["Couldn't reach the calendar feed."]


def test_calendar_urls_is_a_secret_not_a_setting():
    assert "CALENDAR_URLS" in config.SECRET_KEYS
    assert "CALENDAR_URLS" not in config.SETTABLE
    assert "CALENDAR_CACHE_SECONDS" in config.SETTABLE


def test_calendar_cache_setting_is_labelled_in_the_panel():
    from pathlib import Path
    page = (Path(__file__).parent.parent / "wren" / "communication" / "chat.html").read_text()
    assert "CALENDAR_CACHE_SECONDS: { group: \"Day\"" in page
