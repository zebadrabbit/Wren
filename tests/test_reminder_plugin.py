import os, asyncio, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
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

def _past_iso(seconds=120):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")

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

def test_recall_reminders_lists_one_combined_message():
    # Changed from one-message-per-reminder: recall_reminders now emits a
    # single card, and CollectingChannel's send_card sends its combined
    # prose fallback as one message rather than one per row.
    oven_when = _future_iso(60)
    mom_when = _future_iso(120)
    reminders.save(1, "check the oven", oven_when)
    reminders.save(1, "call mom", mom_when)
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("recall_reminders", Ctx(user_id=1, channel=ch, content="")))
    assert len(ch.sent) == 1
    # Notes and ideas both assert their combined prose's content; reminders
    # hadn't, despite this string now being the entire Discord/Telegram
    # experience for recall_reminders. Times come from _format_local rather
    # than a literal, so this doesn't fail on a host in another timezone.
    combined = ch.sent[0]
    assert "check the oven" in combined
    assert "call mom" in combined
    # within the hour a reminder reads as "in N min" (timers made this matter)
    assert reminder_plugin._when_phrase(oven_when) in combined
    assert reminder_plugin._when_phrase(mom_when) in combined


def test_recall_reminders_emits_a_card_with_local_times():
    reminders.save(1, "call mum", "2030-01-01T09:00:00+00:00")
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("recall_reminders", Ctx(user_id=1, channel=ch)))

    card = ch.cards[0]
    assert card["kind"] == "reminders"
    row = card["data"]["reminders"][0]
    assert row["content"] == "call mum"
    assert row["fire_at"] == "2030-01-01T09:00:00+00:00"
    # formatted server-side, so the card never does timezone maths
    assert row["local"] == reminder_plugin._format_local("2030-01-01T09:00:00+00:00")
    assert card["intent"] == "recall_reminders"
    assert len(ch.sent) == 1


def test_no_reminders_still_emits_a_card():
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("recall_reminders", Ctx(user_id=1, channel=ch)))
    assert ch.cards[0]["data"]["reminders"] == []
    assert ch.sent == ["No reminders set."]

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
             patch("wren.skills.reminder_skill.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
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
             patch("wren.skills.reminder_skill.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
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

    async def exploding_notify(user_id, text, via=None):
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

    async def refusing_notify(user_id, text, via=None):
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


def test_disabling_the_skill_does_not_stop_reminders_already_set():
    # Deliberate, and it looks like a bug until you know why: switching
    # Reminders off stops Wren OFFERING it, but a reminder already set for 6pm
    # still arrives. The loop marks a reminder fired only once delivery
    # succeeds, so a stopped poller would pile them up and then fire the lot on
    # re-enable -- and marking them fired without delivering would silently
    # destroy something the user explicitly asked for.
    from wren import registry, settings
    from wren.skills import reminder_skill

    settings.init_db()
    reminders.save(1, "check the oven", _past_iso(60))
    registry.set_enabled(reminder_skill, False)

    async def run_one_iteration():
        with patch.object(router, "notify", new=AsyncMock(return_value=True)) as mock_notify, \
             patch("wren.skills.reminder_skill.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
            try:
                await reminder_skill.start()
            except asyncio.CancelledError:
                pass
        return mock_notify

    try:
        mock_notify = asyncio.run(run_one_iteration())
    finally:
        registry.set_enabled(reminder_skill, True)

    assert mock_notify.await_count == 1


def test_relative_phrase_beats_the_models_arithmetic():
    # the 2026-08-18 bug: model returned a time two hours past the phrase
    ch = CollectingChannel()
    bogus = (datetime.now(timezone.utc) + timedelta(hours=2, seconds=30)).isoformat(timespec="seconds")
    asyncio.run(reminder_plugin.handle("set_reminder", Ctx(
        user_id=1, channel=ch, content="this is a 30 second test",
        text="remind me in 30 seconds that this is a 30 second test", when=bogus)))
    fire_at = datetime.fromisoformat(reminders.pending(1)[0]["fire_at"])
    assert abs((fire_at - datetime.now(timezone.utc)).total_seconds() - 30) < 5


def test_absolute_when_still_comes_from_the_model():
    ch = CollectingChannel()
    when = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat(timespec="seconds")
    asyncio.run(reminder_plugin.handle("set_reminder", Ctx(
        user_id=1, channel=ch, content="dinner", text="remind me at 6pm about dinner", when=when)))
    assert reminders.pending(1)[0]["fire_at"] == when


def test_when_with_trailing_timezone_abbreviation_parses():
    ch = CollectingChannel()
    naive = (datetime.now(ZoneInfo(config.TIMEZONE)) + timedelta(hours=1)).replace(microsecond=0)
    asyncio.run(reminder_plugin.handle("set_reminder", Ctx(
        user_id=1, channel=ch, content="check the oven",
        text="remind me at 6pm to check the oven", when=f"{naive.isoformat()} UTC")))
    assert len(reminders.pending(1)) == 1


def test_cancel_all_via_the_phrase():
    reminders.save(1, "one", _future_iso()); reminders.save(1, "two", _future_iso(300))
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("cancel_reminder", Ctx(
        user_id=1, channel=ch, content="all", text="cancel all reminders")))
    _assert_flourished(ch.sent[0], "Cancelled 2 reminders.")
    assert reminders.pending(1) == []


def test_cancel_all_from_raw_text_when_the_model_extracted_nothing():
    reminders.save(1, "one", _future_iso())
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("cancel_reminder", Ctx(
        user_id=1, channel=ch, content="", text="clear all my reminders please")))
    _assert_flourished(ch.sent[0], "Cancelled 1 reminder.")
    assert reminders.pending(1) == []


def test_cancel_all_with_nothing_pending():
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("cancel_reminder", Ctx(
        user_id=1, channel=ch, content="all", text="cancel all reminders")))
    assert ch.sent == ["No reminders to cancel."]


def test_all_hands_is_not_a_bulk_cancel():
    reminders.save(1, "prep for the all-hands", _future_iso())
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("cancel_reminder", Ctx(
        user_id=1, channel=ch, content="all-hands",
        text="cancel my reminder about the all-hands")))
    assert reminders.pending(1) == []          # matched that one by phrase, not by "all"
    assert "Cancelled" in ch.sent[0]


def test_empty_phrase_lists_the_options():
    reminders.save(1, "take out the bins", _future_iso())
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("cancel_reminder", Ctx(user_id=1, channel=ch, content="")))
    assert ch.sent == ["Which one? You have:\n- take out the bins"]


def test_empty_phrase_with_nothing_pending():
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("cancel_reminder", Ctx(user_id=1, channel=ch, content="")))
    assert ch.sent == ["No reminders to cancel."]


def test_cancel_all_is_scoped_to_the_owner():
    reminders.save(1, "mine", _future_iso()); reminders.save(2, "theirs", _future_iso())
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("cancel_reminder", Ctx(
        user_id=1, channel=ch, content="all", text="cancel all reminders")))
    assert len(reminders.pending(2)) == 1


class _Reachable:
    CAN_NOTIFY = True
    async def notify(self, user_id, text):
        return True


class _SendOnly:
    CAN_NOTIFY = False
    async def notify(self, user_id, text):
        return False


@pytest.fixture
def surfaces(monkeypatch):
    router.reset()
    router.register("telegram", _Reachable())
    router.register("discord", _Reachable())
    router.register("http", _SendOnly())
    monkeypatch.setattr(config, "COMMUNICATION_PLUGINS", ["telegram", "discord", "http"])
    yield
    router.reset()


def test_reminder_is_routed_to_a_named_surface(surfaces):
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("set_reminder", Ctx(
        user_id=1, channel=ch, content="join the boys for mythics",
        text="wren can you remind me on discord to join the boys for mythics in 30 mins?",
        when=_future_iso())))
    assert reminders.pending(1)[0]["via"] == "discord"
    assert "discord" in ch.sent[0]


def test_reminder_with_no_named_surface_uses_the_default(surfaces):
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("set_reminder", Ctx(
        user_id=1, channel=ch, content="check the oven",
        text="remind me in 30 minutes to check the oven", when=_future_iso())))
    assert reminders.pending(1)[0]["via"] is None
    assert "via" not in ch.sent[0]


def test_a_day_name_is_not_a_surface(surfaces):
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("set_reminder", Ctx(
        user_id=1, channel=ch, content="call the dentist",
        text="remind me on tuesday to call the dentist", when=_future_iso())))
    assert reminders.pending(1)[0]["via"] is None


def test_a_send_only_surface_is_refused(surfaces):
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("set_reminder", Ctx(
        user_id=1, channel=ch, content="check the oven",
        text="remind me on http to check the oven", when=_future_iso())))
    assert ch.sent == ["I can't send reminders on http."]
    assert reminders.pending(1) == []


def test_a_configured_but_unstarted_surface_is_refused(monkeypatch):
    router.reset()
    monkeypatch.setattr(config, "COMMUNICATION_PLUGINS", ["telegram", "discord"])
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("set_reminder", Ctx(
        user_id=1, channel=ch, content="join mythics",
        text="remind me on discord to join mythics", when=_future_iso())))
    assert ch.sent == ["I can't send reminders on discord."]
    assert reminders.pending(1) == []


def test_firing_uses_the_stored_route(surfaces):
    reminders.save(1, "join mythics", _past_iso(10), via="discord")

    async def run_one_iteration():
        with patch.object(router, "notify", new=AsyncMock(return_value=True)) as mock_notify, \
             patch("wren.skills.reminder_skill.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
            try:
                await reminder_plugin.start()
            except asyncio.CancelledError:
                pass
        return mock_notify

    mock_notify = asyncio.run(run_one_iteration())
    assert mock_notify.call_args.kwargs["via"] == "discord"


@pytest.mark.parametrize("phrase,seconds", [
    ("remind me in 30 mins to stretch", 1800),
    ("remind me in 5 min to stretch", 300),
    ("remind me in 2 hrs to stretch", 7200),
    ("remind me in 45 secs to stretch", 45),
    ("remind me in 1 hr to stretch", 3600),
])
def test_abbreviated_units_are_parsed_here_not_by_the_model(phrase, seconds):
    ch = CollectingChannel()
    bogus = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(timespec="seconds")
    asyncio.run(reminder_plugin.handle("set_reminder", Ctx(
        user_id=1, channel=ch, content="stretch", text=phrase, when=bogus)))
    fire_at = datetime.fromisoformat(reminders.pending(1)[0]["fire_at"])
    assert abs((fire_at - datetime.now(timezone.utc)).total_seconds() - seconds) < 5


# --- recurring reminders --------------------------------------------------
# "every …" is parsed here, like "in 20 minutes", because the classifier's
# clock maths cannot be trusted; the model still supplies the FIRST occurrence
# as `when`. A fired recurring reminder re-arms instead of finishing.

@pytest.mark.parametrize("text,expected", [
    ("take the bins out every tuesday at 8pm", "7d"),
    ("every day at 8am take my meds", "1d"),
    ("water the plants every 3 days", "3d"),
    ("every morning check the chickens", "1d"),
    ("every night lock up", "1d"),
    ("stretch every 2 hours", "2h"),
    ("check the oven every 30 minutes", "30m"),
    ("every other week put the recycling out", "14d"),
    ("every week on friday do the timesheet", "7d"),
    ("daily standup at 9", "1d"),
    ("weekly review", "7d"),
    ("hourly posture check", "1h"),
    ("remind me at 6pm to call mum", None),
    ("remind me about the everyday bag", None),
    ("every weekday at 7", None),  # out of scope for now, must not mis-parse as daily
])
def test_parse_repeat(text, expected):
    assert reminder_plugin._parse_repeat(text) == expected


@pytest.mark.parametrize("repeat,phrase", [
    ("1d", "every day"), ("3d", "every 3 days"), ("7d", "every week"),
    ("14d", "every 2 weeks"), ("1h", "every hour"), ("2h", "every 2 hours"),
    ("30m", "every 30 minutes"),
])
def test_format_repeat(repeat, phrase):
    assert reminder_plugin._format_repeat(repeat) == phrase


def test_set_recurring_reminder_stores_the_rule_and_says_so():
    ch = CollectingChannel()
    when = _future_iso()
    asyncio.run(reminder_plugin.handle("set_reminder", Ctx(
        user_id=1, channel=ch, content="water the plants every 3 days",
        text="remind me to water the plants every 3 days at 6pm", when=when)))
    assert "every 3 days" in ch.sent[0]
    row = reminders.pending(1)[0]
    assert row["repeat"] == "3d"
    # the rule is not part of what gets read back at fire time
    assert row["content"] == "water the plants"


def test_one_shot_reminder_has_no_rule():
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("set_reminder", Ctx(
        user_id=1, channel=ch, content="call mum", text="remind me to call mum at 6pm", when=_future_iso())))
    assert reminders.pending(1)[0]["repeat"] is None


def _run_one_poll():
    async def go():
        with patch.object(router, "notify", new=AsyncMock(return_value=True)) as mock_notify, \
             patch("wren.skills.reminder_skill.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
            try:
                await reminder_plugin.start()
            except asyncio.CancelledError:
                pass
        return mock_notify
    return asyncio.run(go())


def test_firing_a_recurring_reminder_rearms_it_from_the_scheduled_time():
    scheduled = datetime.now(timezone.utc) - timedelta(seconds=5)
    reminders.save(1, "water the plants", scheduled.isoformat(timespec="seconds"), repeat="3d")
    mock_notify = _run_one_poll()
    assert mock_notify.await_count == 1
    rows = reminders.pending(1)
    assert len(rows) == 1 and rows[0]["status"] == "pending"
    assert datetime.fromisoformat(rows[0]["fire_at"]) == (scheduled + timedelta(days=3)).replace(microsecond=0)


def test_missed_periods_fire_once_and_skip_to_the_next_future_slot():
    # the service was down for ten days on a three-day rule: one reminder, not
    # three, and the next slot is the first one still ahead of now
    scheduled = datetime.now(timezone.utc) - timedelta(days=10)
    reminders.save(1, "water the plants", scheduled.isoformat(timespec="seconds"), repeat="3d")
    mock_notify = _run_one_poll()
    assert mock_notify.await_count == 1
    nxt = datetime.fromisoformat(reminders.pending(1)[0]["fire_at"])
    assert nxt == (scheduled + timedelta(days=12)).replace(microsecond=0)
    assert nxt > datetime.now(timezone.utc)


def test_daily_step_keeps_the_wall_clock_time_across_the_dst_change(monkeypatch):
    monkeypatch.setattr(config, "TIMEZONE", "America/Chicago")
    # 08:00 CDT on the last day of daylight time is 13:00Z; the next 08:00 is CST, 14:00Z
    fired = "2026-10-31T13:00:00+00:00"
    now = datetime(2026, 10, 31, 13, 0, 5, tzinfo=timezone.utc)
    nxt = reminder_plugin._next_fire(fired, "1d", now)
    assert nxt == datetime(2026, 11, 1, 14, 0, tzinfo=timezone.utc)


def test_hourly_step_is_absolute_not_wall_clock(monkeypatch):
    monkeypatch.setattr(config, "TIMEZONE", "America/Chicago")
    fired = "2026-11-01T06:00:00+00:00"   # 01:00 CDT, an hour before the fall-back
    now = datetime(2026, 11, 1, 6, 0, 5, tzinfo=timezone.utc)
    assert reminder_plugin._next_fire(fired, "2h", now) == datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)


def test_cancelling_a_recurring_reminder_stops_the_series():
    reminders.save(1, "water the plants", _future_iso(), repeat="3d")
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("cancel_reminder", Ctx(user_id=1, channel=ch, content="plants")))
    assert reminders.pending(1) == []


def test_recall_shows_the_rule_in_prose_and_in_the_card():
    reminders.save(1, "water the plants", _future_iso(), repeat="3d")
    reminders.save(1, "call mum", _future_iso())
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("recall_reminders", Ctx(user_id=1, channel=ch, content="")))
    assert "water the plants" in ch.sent[0] and "every 3 days" in ch.sent[0]
    rows = ch.cards[0]["data"]["reminders"]
    assert rows[0]["local"].endswith(", every 3 days")
    assert "every" not in rows[1]["local"]


# --- timers ---------------------------------------------------------------
# "timer 20 minutes" is a reminder with nothing to say; "how long is left" is
# recall, which now reads "in N min" for anything due within the hour.

@pytest.mark.parametrize("text,expected", [
    ("timer 20 minutes", (20, "minutes")),
    ("set a 10 minute timer", (10, "minutes")),
    ("timer for 2 hours", (2, "hours")),
    ("5 min timer", (5, "minutes")),
    ("start a 90 second timer", (90, "seconds")),
    ("remind me in 20 minutes to check the oven", None),
    ("how long is left on the timer", None),
])
def test_parse_timer(text, expected):
    assert reminder_plugin._parse_timer(text) == expected

def test_a_timer_needs_no_content_and_says_how_long():
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("set_reminder", Ctx(user_id=1, channel=ch, content="", text="timer 20 minutes")))
    _assert_flourished(ch.sent[0], "Timer set, 20 minutes.")
    row = reminders.pending(1)[0]
    assert row["content"] == "20 minute timer is up"
    fire = datetime.fromisoformat(row["fire_at"])
    assert timedelta(minutes=19) < fire - datetime.now(timezone.utc) <= timedelta(minutes=20)

def test_a_one_hour_timer_reads_singular():
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("set_reminder", Ctx(user_id=1, channel=ch, content="timer", text="set a 1 hour timer")))
    _assert_flourished(ch.sent[0], "Timer set, 1 hour.")
    assert reminders.pending(1)[0]["content"] == "1 hour timer is up"

def test_when_phrase_is_relative_within_the_hour_and_absolute_beyond():
    assert reminder_plugin._when_phrase(_future_iso(12 * 60 + 5)) == "in 12 min"
    assert reminder_plugin._when_phrase(_future_iso(30)) == "in under a minute"
    far = _future_iso(2 * 24 * 3600)
    assert reminder_plugin._when_phrase(far) == reminder_plugin._format_local(far)

def test_how_long_is_left_shows_minutes_remaining():
    reminders.save(1, "20 minute timer is up", _future_iso(12 * 60 + 5))
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("recall_reminders", Ctx(user_id=1, channel=ch, content="", text="how long is left on the timer")))
    assert ch.sent == ["[in 12 min] 20 minute timer is up"]
    assert ch.cards[0]["data"]["reminders"][0]["local"] == "in 12 min"


# --- undo --------------------------------------------------------------------

def test_undo_cancel_restores_the_reminder():
    reminders.save(1, "call mum", _future_iso())
    ctx = Ctx(user_id=1, channel=CollectingChannel(), content="mum", text="cancel the mum reminder")
    asyncio.run(reminder_plugin.handle("cancel_reminder", ctx))
    assert reminders.pending(1) == []
    asyncio.run(reminder_plugin.UNDO["cancel_reminder"](ctx))
    assert [r["content"] for r in reminders.pending(1)] == ["call mum"]
    _assert_flourished(ctx.channel.sent[-1], "Restored: call mum.")

def test_undo_cancel_all_restores_them_all():
    reminders.save(1, "a", _future_iso()); reminders.save(1, "b", _future_iso())
    ctx = Ctx(user_id=1, channel=CollectingChannel(), content="all", text="cancel all my reminders")
    asyncio.run(reminder_plugin.handle("cancel_reminder", ctx))
    asyncio.run(reminder_plugin.UNDO["cancel_reminder"](ctx))
    assert sorted(r["content"] for r in reminders.pending(1)) == ["a", "b"]
    _assert_flourished(ctx.channel.sent[-1], "Restored 2 reminders.")


def test_a_timer_can_be_directed_to_a_surface(surfaces):
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("set_reminder", Ctx(
        user_id=1, channel=ch, content="",
        text="set a timer for 10 minutes and notify me on discord")))
    assert reminders.pending(1)[0]["via"] == "discord"
    assert ch.sent[0].startswith("Timer set, 10 minutes") and "via discord" in ch.sent[0]
