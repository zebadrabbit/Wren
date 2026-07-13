import os, asyncio, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("HUSBAND_ID", "2")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock, AsyncMock, patch
from wren import reminders
from wren import reminder_plugin
from wren import discord_utils
from wren import config

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(reminders, "DB_PATH", str(tmp_path / "test.db"))
    reminders.init_db()

def _message():
    message = MagicMock()
    message.channel.send = AsyncMock()
    return message

def _future_iso(seconds=120):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")

def test_set_reminder_valid():
    message = _message()
    when = _future_iso()
    asyncio.run(reminder_plugin.handle("set_reminder", message, None, 1, "check the oven", [], None, when))
    sent = message.channel.send.call_args[0][0]
    assert "Reminder set for" in sent
    assert len(reminders.pending(1)) == 1

def test_set_reminder_missing_content():
    message = _message()
    asyncio.run(reminder_plugin.handle("set_reminder", message, None, 1, "", [], None, _future_iso()))
    message.channel.send.assert_awaited_once_with("I couldn't figure out when — try again with a specific time.")
    assert reminders.pending(1) == []

def test_set_reminder_unparseable_when():
    message = _message()
    asyncio.run(reminder_plugin.handle("set_reminder", message, None, 1, "check the oven", [], None, "not-a-real-date"))
    message.channel.send.assert_awaited_once_with("I couldn't figure out when — try again with a specific time.")
    assert reminders.pending(1) == []

def test_set_reminder_past_time_rejected():
    message = _message()
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    asyncio.run(reminder_plugin.handle("set_reminder", message, None, 1, "check the oven", [], None, past))
    message.channel.send.assert_awaited_once_with("I couldn't figure out when — try again with a specific time.")
    assert reminders.pending(1) == []

def test_set_reminder_within_grace_window_accepted():
    message = _message()
    almost_now = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(timespec="seconds")
    asyncio.run(reminder_plugin.handle("set_reminder", message, None, 1, "check the oven", [], None, almost_now))
    assert len(reminders.pending(1)) == 1

def test_recall_reminders_empty():
    message = _message()
    asyncio.run(reminder_plugin.handle("recall_reminders", message, None, 1, "", [], None, None))
    message.channel.send.assert_awaited_once_with("No reminders set.")

def test_recall_reminders_lists_one_message_each():
    reminders.save(1, "check the oven", _future_iso(60))
    reminders.save(1, "call mom", _future_iso(120))
    message = _message()
    asyncio.run(reminder_plugin.handle("recall_reminders", message, None, 1, "", [], None, None))
    assert message.channel.send.await_count == 2

def test_cancel_reminder_empty_content_guarded():
    message = _message()
    asyncio.run(reminder_plugin.handle("cancel_reminder", message, None, 1, "", [], None, None))
    message.channel.send.assert_awaited_once_with("Which reminder do you want to cancel?")

def test_cancel_reminder_no_match():
    message = _message()
    asyncio.run(reminder_plugin.handle("cancel_reminder", message, None, 1, "oven", [], None, None))
    message.channel.send.assert_awaited_once_with("No reminder found matching that.")

def test_cancel_reminder_single_match():
    reminders.save(1, "check the oven", _future_iso())
    message = _message()
    asyncio.run(reminder_plugin.handle("cancel_reminder", message, None, 1, "oven", [], None, None))
    message.channel.send.assert_awaited_once_with("Cancelled: check the oven.")
    assert reminders.pending(1) == []

def test_cancel_reminder_multiple_matches():
    reminders.save(1, "check the oven at noon", _future_iso())
    reminders.save(1, "check the oven at night", _future_iso())
    message = _message()
    asyncio.run(reminder_plugin.handle("cancel_reminder", message, None, 1, "oven", [], None, None))
    sent = message.channel.send.call_args[0][0]
    assert "Found more than one match" in sent

def test_start_fires_due_reminder_and_marks_fired():
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(timespec="seconds")
    reminders.save(1, "check the oven", past)

    async def run_one_iteration():
        with patch.object(discord_utils, "notify_id", new=AsyncMock(return_value=True)) as mock_notify, \
             patch("wren.reminder_plugin.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
            try:
                await reminder_plugin.start(None)
            except asyncio.CancelledError:
                pass
        return mock_notify

    mock_notify = asyncio.run(run_one_iteration())
    mock_notify.assert_awaited_once()
    assert reminders.pending(1) == []

def test_parse_when_naive_datetime_localized_to_configured_timezone(monkeypatch):
    monkeypatch.setattr(config, "TIMEZONE", "America/Chicago")
    # Jan 1 2030, 9am Chicago time (CST, UTC-6 — no DST ambiguity), no offset in the string
    result = reminder_plugin._parse_when("2030-01-01T09:00:00")
    assert result is not None
    assert result.isoformat(timespec="seconds") == "2030-01-01T15:00:00+00:00"

def test_parse_when_offset_aware_input_unaffected_by_timezone_config(monkeypatch):
    monkeypatch.setattr(config, "TIMEZONE", "America/Chicago")
    # already has an explicit UTC offset — config.TIMEZONE must not touch it
    result = reminder_plugin._parse_when("2030-01-01T15:00:00+00:00")
    assert result.isoformat(timespec="seconds") == "2030-01-01T15:00:00+00:00"

def test_format_local_converts_utc_to_configured_timezone(monkeypatch):
    monkeypatch.setattr(config, "TIMEZONE", "America/Chicago")
    # July -> Chicago is CDT (UTC-5), no DST ambiguity
    result = reminder_plugin._format_local("2026-07-13T12:00:00+00:00")
    assert result == "2026-07-13 07:00 CDT"

def test_set_reminder_confirmation_shows_local_time(monkeypatch):
    monkeypatch.setattr(config, "TIMEZONE", "America/Chicago")
    message = _message()
    asyncio.run(reminder_plugin.handle("set_reminder", message, None, 1, "check the oven", [], None, "2030-01-01T09:00:00"))
    sent = message.channel.send.call_args[0][0]
    assert "2030-01-01 09:00 CST" in sent
    assert "UTC" not in sent

def test_start_does_not_fire_future_reminder():
    reminders.save(1, "check the oven", _future_iso(3600))

    async def run_one_iteration():
        with patch.object(discord_utils, "notify_id", new=AsyncMock(return_value=True)) as mock_notify, \
             patch("wren.reminder_plugin.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
            try:
                await reminder_plugin.start(None)
            except asyncio.CancelledError:
                pass
        return mock_notify

    mock_notify = asyncio.run(run_one_iteration())
    mock_notify.assert_not_called()
    assert len(reminders.pending(1)) == 1
