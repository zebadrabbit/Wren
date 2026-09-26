import os
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")
os.environ.setdefault("TIMEZONE", "UTC")

import asyncio
import logging
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


def test_a_non_httperror_calendar_failure_still_degrades_to_one_line():
    # F6: _fetch used to wrap only httpx.HTTPError -- any other exception
    # (here a stand-in RuntimeError for httpx.InvalidURL etc.) escaped
    # _fetch() as a raw exception compose() does not catch, costing the whole
    # briefing instead of just the Calendar line. Goes through the real
    # events_between()/_fetch() path (only httpx.get is mocked) so this would
    # have failed against the old bare `except httpx.HTTPError`.
    calendar_skill._cache.clear()
    with patch.object(weather_skill, "summary", return_value="72°F."), \
         patch.object(calendar_skill.httpx, "get", side_effect=RuntimeError("boom")):
        text = briefing_skill.compose(1)
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


from unittest.mock import AsyncMock
from wren import router


def test_should_fire_inside_the_window_once_per_day():
    t = datetime(2026, 8, 26, 7, 30, tzinfo=timezone.utc)
    assert briefing_skill._should_fire(t.replace(hour=7, minute=29), "07:30", None) is False
    assert briefing_skill._should_fire(t, "07:30", None) is True
    assert briefing_skill._should_fire(t.replace(minute=39), "07:30", None) is True
    assert briefing_skill._should_fire(t.replace(minute=41), "07:30", None) is False   # restart later in the day: skip
    assert briefing_skill._should_fire(t, "07:30", date(2026, 8, 26)) is False           # already sent today
    assert briefing_skill._should_fire(t, "07:30", date(2026, 8, 25)) is True
    assert briefing_skill._should_fire(t, "", None) is False                             # switched off


def test_start_sends_the_briefing_to_the_owner(monkeypatch):
    monkeypatch.setattr(config, "BRIEFING_TIME", "07:30")
    monkeypatch.setattr(config, "WHITELIST", {"owner": 1})
    briefing_skill._last_sent = None
    with patch.object(briefing_skill, "compose", return_value="Good morning."), \
         patch.object(router, "notify", new=AsyncMock(return_value=True)) as notify, \
         patch("wren.skills.briefing_skill.asyncio.sleep", new=AsyncMock(side_effect=[None, asyncio.CancelledError])):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(briefing_skill.start())
    notify.assert_awaited_once_with(1, "Good morning.")
    assert briefing_skill._last_sent == date(2026, 8, 26)


def test_start_still_marks_the_day_sent_when_notify_returns_false(monkeypatch, caplog):
    # M5: router.notify() returning False means "delivered to nobody" (e.g.
    # NOTIFY_VIA is send-only), not a transient failure like a raised
    # exception -- retrying every minute for the rest of the day would just
    # repeat the same no-op. _last_sent must still advance, with a warning
    # logged so the miss is visible.
    monkeypatch.setattr(config, "BRIEFING_TIME", "07:30")
    monkeypatch.setattr(config, "WHITELIST", {"owner": 1})
    briefing_skill._last_sent = None
    with patch.object(briefing_skill, "compose", return_value="Good morning."), \
         patch.object(router, "notify", new=AsyncMock(return_value=False)) as notify, \
         patch("wren.skills.briefing_skill.asyncio.sleep", new=AsyncMock(side_effect=[None, asyncio.CancelledError])):
        with caplog.at_level(logging.WARNING):
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(briefing_skill.start())
    notify.assert_awaited_once_with(1, "Good morning.")
    assert briefing_skill._last_sent == date(2026, 8, 26)
    assert any("unreachable" in r.getMessage() for r in caplog.records)


def test_start_does_nothing_when_off(monkeypatch):
    monkeypatch.setattr(config, "BRIEFING_TIME", "")
    briefing_skill._last_sent = None
    with patch.object(router, "notify", new=AsyncMock(return_value=True)) as notify, \
         patch("wren.skills.briefing_skill.asyncio.sleep", new=AsyncMock(side_effect=[None, asyncio.CancelledError])):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(briefing_skill.start())
    notify.assert_not_called()


def test_start_survives_a_raising_notify_and_retries_next_step(monkeypatch):
    monkeypatch.setattr(config, "BRIEFING_TIME", "07:30")
    monkeypatch.setattr(config, "WHITELIST", {"owner": 1})
    briefing_skill._last_sent = None
    with patch.object(briefing_skill, "compose", return_value="Good morning."), \
         patch.object(router, "notify", new=AsyncMock(side_effect=[RuntimeError("503"), True])) as notify, \
         patch("wren.skills.briefing_skill.asyncio.sleep", new=AsyncMock(side_effect=[None, None, asyncio.CancelledError])):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(briefing_skill.start())
    assert notify.await_count == 2
    assert briefing_skill._last_sent == date(2026, 8, 26)


def test_guideline_defers_specific_day_questions_to_recall_calendar():
    # F4: the reciprocal half of the calendar_skill guideline fix -- pins that
    # `briefing`'s guideline explicitly hands off to recall_calendar rather
    # than re-claiming "what does a specific day look like".
    assert "recall_calendar" in briefing_skill.PROMPT_GUIDELINES


def test_briefing_time_is_labelled_in_the_panel():
    from pathlib import Path
    page = (Path(__file__).parent.parent / "wren" / "communication" / "chat.html").read_text()
    assert 'BRIEFING_TIME: { group: "Day"' in page


def test_should_fire_survives_a_dst_spring_forward_gap():
    # America/Chicago springs forward 01:59 CST -> 03:00 CDT on 2026-03-08;
    # 02:30 is a wall-clock time that never happens that day. `_should_fire`
    # must still fire once (for the resolved, slightly-later real instant)
    # rather than never, which is what a wall-clock (not instant) comparison
    # would silently do.
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/Chicago")

    # fold=0 resolves an imaginary wall time using the PRE-transition (CST,
    # -06:00) offset, so 02:30 lands on the UTC instant 08:30 -- which is
    # 03:30 CDT, half an hour past the transition itself (08:00 UTC), not
    # the transition's first minute. 12 hours of UTC-anchored minutes from
    # midnight comfortably covers that instant with room either side.
    gap_start = datetime(2026, 3, 8, 0, 0, tzinfo=timezone.utc)
    gap_minutes = [(gap_start + timedelta(seconds=60 * i)).astimezone(tz) for i in range(12 * 60)]
    assert any(briefing_skill._should_fire(n, "02:30", None) for n in gap_minutes)

    # A normal day (no DST transition) is unaffected: the window is exactly
    # the 10 consecutive minutes starting at BRIEFING_TIME, no more, no less.
    normal_start = datetime(2026, 8, 26, 0, 0, tzinfo=timezone.utc)
    normal_minutes = [(normal_start + timedelta(seconds=60 * i)).astimezone(tz) for i in range(24 * 60)]
    fires = sum(1 for n in normal_minutes if briefing_skill._should_fire(n, "07:30", None))
    assert fires == 10


def test_start_does_nothing_when_the_skill_is_disabled_in_the_panel(monkeypatch):
    monkeypatch.setattr(config, "BRIEFING_TIME", "07:30")
    monkeypatch.setattr(config, "WHITELIST", {"owner": 1})
    briefing_skill._last_sent = None
    registry.set_enabled(briefing_skill, False)
    try:
        with patch.object(router, "notify", new=AsyncMock(return_value=True)) as notify, \
             patch("wren.skills.briefing_skill.asyncio.sleep", new=AsyncMock(side_effect=[None, asyncio.CancelledError])):
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(briefing_skill.start())
    finally:
        registry.set_enabled(briefing_skill, True)
    notify.assert_not_called()
    assert briefing_skill._last_sent is None


# --- email digest ------------------------------------------------------------

def test_briefing_lists_the_last_days_watched_mail_when_digest_is_on(monkeypatch):
    from wren.communication import gmail_state
    gmail_state.init_db()
    monkeypatch.setattr(config, "EMAIL_DIGEST", True)
    monkeypatch.setattr(config, "id_to_name", lambda: {1: "owner"})
    gmail_state.add("owner", "alice@example.com", "Invoice 42")
    gmail_state.add("owner", "school@example.org", "Picture day")
    gmail_state.add("hubby", "bob@example.com", "Not yours")
    with patch.object(weather_skill, "summary", return_value="72°F and clear."), \
         patch.object(calendar_skill, "events_between", return_value=[]):
        text = briefing_skill.compose(1)
    assert "Email:\n  school@example.org — Picture day\n  alice@example.com — Invoice 42" in text
    assert "Not yours" not in text


def test_briefing_caps_the_digest_at_five(monkeypatch):
    from wren.communication import gmail_state
    gmail_state.init_db()
    monkeypatch.setattr(config, "EMAIL_DIGEST", True)
    monkeypatch.setattr(config, "id_to_name", lambda: {1: "owner"})
    for i in range(8):
        gmail_state.add("owner", f"s{i}@example.com", f"Subject {i}")
    with patch.object(weather_skill, "summary", return_value="x"), \
         patch.object(calendar_skill, "events_between", return_value=[]):
        text = briefing_skill.compose(1)
    assert text.count("@example.com — ") == 5 and "  …and 3 more." in text


def test_briefing_says_nothing_about_email_when_digest_is_off_or_empty(monkeypatch):
    from wren.communication import gmail_state
    gmail_state.init_db()
    monkeypatch.setattr(config, "EMAIL_DIGEST", False)
    monkeypatch.setattr(config, "id_to_name", lambda: {1: "owner"})
    gmail_state.add("owner", "alice@example.com", "Invoice 42")
    with patch.object(weather_skill, "summary", return_value="x"), \
         patch.object(calendar_skill, "events_between", return_value=[]):
        assert "Email" not in briefing_skill.compose(1)
    monkeypatch.setattr(config, "EMAIL_DIGEST", True)
    monkeypatch.setattr(config, "id_to_name", lambda: {2: "hubby"})
    with patch.object(weather_skill, "summary", return_value="x"), \
         patch.object(calendar_skill, "events_between", return_value=[]):
        assert "Email" not in briefing_skill.compose(2)
