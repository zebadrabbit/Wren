import os, asyncio, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")
os.environ.setdefault("TIMEZONE", "UTC")

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, patch
from wren.skills import reminders_store as reminders
from wren.skills import reminder_skill as reminder_plugin
from wren import router
from wren import config
from wren.channel import Ctx, CollectingChannel
from wren.flourish import EMOTES

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("WREN_DB", str(tmp_path / "wren.db"))
    reminders.init_db()

def _future_iso(seconds=120):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")

def _assert_flourished(sent: str, prefix: str):
    assert sent.startswith(prefix + " ")
    assert sent.rsplit(" ", 1)[1] in EMOTES

def test_set_reminder_valid():
    ch = CollectingChannel()
    when = _future_iso()
    asyncio.run(reminder_plugin.handle("set_reminder", Ctx(user_id=1, channel=ch, content="check the oven", when=when)))
    assert "Reminder set for" in ch.sent[0]
    assert len(reminders.pending(1)) == 1

def test_set_reminder_missing_content():
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("set_reminder", Ctx(user_id=1, channel=ch, content="", when=_future_iso())))
    assert ch.sent == ["I couldn't figure out when — try again with a specific time."]
    assert reminders.pending(1) == []

def test_set_reminder_unparseable_when():
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("set_reminder", Ctx(user_id=1, channel=ch, content="check the oven", when="not-a-real-date")))
    assert ch.sent == ["I couldn't figure out when — try again with a specific time."]
    assert reminders.pending(1) == []

def test_set_reminder_past_time_rejected():
    ch = CollectingChannel()
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    asyncio.run(reminder_plugin.handle("set_reminder", Ctx(user_id=1, channel=ch, content="check the oven", when=past)))
    assert ch.sent == ["I couldn't figure out when — try again with a specific time."]
    assert reminders.pending(1) == []

def test_set_reminder_within_grace_window_accepted():
    ch = CollectingChannel()
    almost_now = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(timespec="seconds")
    asyncio.run(reminder_plugin.handle("set_reminder", Ctx(user_id=1, channel=ch, content="check the oven", when=almost_now)))
    assert len(reminders.pending(1)) == 1

def test_recall_reminders_empty():
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("recall_reminders", Ctx(user_id=1, channel=ch, content="")))
    assert ch.sent == ["No reminders set."]

def test_recall_reminders_lists_one_message_each():
    reminders.save(1, "check the oven", _future_iso(60))
    reminders.save(1, "call mom", _future_iso(120))
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("recall_reminders", Ctx(user_id=1, channel=ch, content="")))
    assert len(ch.sent) == 2

def test_cancel_reminder_empty_content_guarded():
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("cancel_reminder", Ctx(user_id=1, channel=ch, content="")))
    assert ch.sent == ["Which reminder do you want to cancel?"]

def test_cancel_reminder_no_match():
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("cancel_reminder", Ctx(user_id=1, channel=ch, content="oven")))
    assert ch.sent == ["No reminder found matching that."]

def test_cancel_reminder_single_match():
    reminders.save(1, "check the oven", _future_iso())
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("cancel_reminder", Ctx(user_id=1, channel=ch, content="oven")))
    _assert_flourished(ch.sent[0], "Cancelled: check the oven.")
    assert reminders.pending(1) == []

def test_cancel_reminder_nonliteral_phrase_resolves_when_only_one_pending():
    # Regression test: the model often extracts a paraphrase ("the last
    # reminder", "the 9am one") rather than words literally present in the
    # saved content. With only one pending reminder, that's still resolvable.
    reminders.save(1, "message me at 9am my time to confirm", _future_iso())
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("cancel_reminder", Ctx(user_id=1, channel=ch, content="the last reminder")))
    _assert_flourished(ch.sent[0], "Cancelled: message me at 9am my time to confirm.")
    assert reminders.pending(1) == []

def test_cancel_reminder_nonliteral_phrase_stays_ambiguous_with_multiple_pending():
    reminders.save(1, "check the oven", _future_iso())
    reminders.save(1, "call mom", _future_iso())
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("cancel_reminder", Ctx(user_id=1, channel=ch, content="the last reminder")))
    assert ch.sent == ["No reminder found matching that."]
    assert len(reminders.pending(1)) == 2

def test_cancel_reminder_multiple_matches():
    reminders.save(1, "check the oven at noon", _future_iso())
    reminders.save(1, "check the oven at night", _future_iso())
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("cancel_reminder", Ctx(user_id=1, channel=ch, content="oven")))
    assert "Found more than one match" in ch.sent[0]

def test_start_fires_due_reminder_and_marks_fired():
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(timespec="seconds")
    reminders.save(1, "check the oven", past)

    async def run_one_iteration():
        with patch.object(router, "notify", new=AsyncMock(return_value=True)) as mock_notify, \
             patch("wren.reminder_plugin.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
            try:
                await reminder_plugin.start()
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
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("set_reminder", Ctx(user_id=1, channel=ch, content="check the oven", when="2030-01-01T09:00:00")))
    assert "2030-01-01 09:00 CST" in ch.sent[0]
    assert "UTC" not in ch.sent[0]

def test_start_does_not_fire_future_reminder():
    reminders.save(1, "check the oven", _future_iso(3600))

    async def run_one_iteration():
        with patch.object(router, "notify", new=AsyncMock(return_value=True)) as mock_notify, \
             patch("wren.reminder_plugin.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
            try:
                await reminder_plugin.start()
            except asyncio.CancelledError:
                pass
        return mock_notify

    mock_notify = asyncio.run(run_one_iteration())
    mock_notify.assert_not_called()
    assert len(reminders.pending(1)) == 1


# ── transient-failure regression (found by adversarial review 2026-08-08) ────

def test_transient_delivery_failure_does_not_consume_the_reminder(monkeypatch):
    """A reminder must survive a Discord 503 / network blip and be retried on
    the next poll, not be marked fired and lost forever."""
    reminders.save(1, "take the bins out", "2020-01-01T00:00:00+00:00")

    async def exploding_notify(user_id, text):
        raise RuntimeError("Discord 503")

    monkeypatch.setattr(reminder_plugin.router, "notify", exploding_notify)
    monkeypatch.setattr(reminder_plugin.config, "REMINDER_POLL_SECONDS", 0)

    async def run():
        task = asyncio.create_task(reminder_plugin.start())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())

    still_pending = reminders.pending(1)
    assert len(still_pending) == 1, "transient failure consumed the reminder"
    assert still_pending[0]["content"] == "take the bins out"


def test_permanent_delivery_failure_does_consume_the_reminder(monkeypatch):
    """False means permanent (DMs closed). Retrying forever would just spin."""
    reminders.save(1, "call the dentist", "2020-01-01T00:00:00+00:00")

    async def refusing_notify(user_id, text):
        return False

    monkeypatch.setattr(reminder_plugin.router, "notify", refusing_notify)
    monkeypatch.setattr(reminder_plugin.config, "REMINDER_POLL_SECONDS", 0)

    async def run():
        task = asyncio.create_task(reminder_plugin.start())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert reminders.pending(1) == []
