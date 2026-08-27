import os
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")
os.environ.setdefault("TIMEZONE", "UTC")

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from wren import config
from wren.skills import calendar_feed as feed


@pytest.fixture(autouse=True)
def chicago(monkeypatch):
    monkeypatch.setattr(config, "TIMEZONE", "America/Chicago")


def ics(*events: str) -> str:
    body = "\n".join(f"BEGIN:VEVENT\n{e.strip()}\nEND:VEVENT" for e in events)
    return f"BEGIN:VCALENDAR\nVERSION:2.0\n{body}\nEND:VCALENDAR\n"


def names(events):
    return [e.summary for e in events]


def test_all_day_event_on_its_day():
    text = ics("UID:a\nSUMMARY:Kate in town\nDTSTART;VALUE=DATE:20260826\nDTEND;VALUE=DATE:20260827")
    (e,) = feed.events_between(text, date(2026, 8, 26), 1)
    assert e.summary == "Kate in town"
    assert e.all_day is True
    assert e.start == datetime(2026, 8, 26, tzinfo=ZoneInfo("America/Chicago"))
    assert feed.events_between(text, date(2026, 8, 27), 1) == []


def test_timed_event_with_tzid_lands_in_the_configured_zone():
    text = ics("UID:b\nSUMMARY:Standup\nDTSTART;TZID=America/New_York:20260826T100000\n"
               "DTEND;TZID=America/New_York:20260826T103000")
    (e,) = feed.events_between(text, date(2026, 8, 26), 1)
    assert e.all_day is False
    assert e.start.hour == 9 and e.start.tzinfo == ZoneInfo("America/Chicago")
    assert (e.end - e.start).seconds == 1800


def test_utc_and_floating_times():
    text = ics("UID:c\nSUMMARY:UTC one\nDTSTART:20260826T150000Z\nDTEND:20260826T160000Z",
               "UID:d\nSUMMARY:Floating one\nDTSTART:20260826T080000\nDTEND:20260826T090000")
    events = feed.events_between(text, date(2026, 8, 26), 1)
    assert names(events) == ["Floating one", "UTC one"]       # 08:00 local before 10:00 local
    assert events[1].start.hour == 10


def test_duration_instead_of_dtend():
    text = ics("UID:e\nSUMMARY:Dentist\nDTSTART:20260826T130000\nDURATION:PT1H30M")
    (e,) = feed.events_between(text, date(2026, 8, 26), 1)
    assert e.end.hour == 14 and e.end.minute == 30


def test_summary_is_unescaped_and_folded_lines_are_joined():
    text = ics("UID:f\nSUMMARY:Lunch\\, with Kim\\; then\n  coffee\nDTSTART:20260826T120000\nDTEND:20260826T130000")
    (e,) = feed.events_between(text, date(2026, 8, 26), 1)
    # the fold removes exactly one leading whitespace character (RFC 5545 3.1)
    assert e.summary == "Lunch, with Kim; then coffee"


def test_weekly_byday_recurrence_expands_inside_the_window_only():
    text = ics("UID:g\nSUMMARY:Standup\nDTSTART:20260803T090000\nDTEND:20260803T091500\n"
               "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR")
    week = feed.events_between(text, date(2026, 8, 24), 7)      # Mon 24 .. Sun 30
    assert [e.start.date() for e in week] == [date(2026, 8, 24), date(2026, 8, 26), date(2026, 8, 28)]
    assert feed.events_between(text, date(2026, 7, 27), 7) == []  # before DTSTART


def test_daily_recurrence_honours_count_and_until():
    counted = ics("UID:h\nSUMMARY:Pills\nDTSTART:20260825T080000\nDTEND:20260825T080500\nRRULE:FREQ=DAILY;COUNT=3")
    assert [e.start.day for e in feed.events_between(counted, date(2026, 8, 24), 7)] == [25, 26, 27]
    until = ics("UID:i\nSUMMARY:Pills\nDTSTART:20260825T080000\nDTEND:20260825T080500\n"
                "RRULE:FREQ=DAILY;UNTIL=20260826T130000Z")
    assert [e.start.day for e in feed.events_between(until, date(2026, 8, 24), 7)] == [25, 26]


def test_recurrence_keeps_wall_clock_time_across_dst():
    text = ics("UID:j\nSUMMARY:Standup\nDTSTART:20261026T090000\nDTEND:20261026T091500\nRRULE:FREQ=WEEKLY")
    # US DST ends 2026-11-01; the 9am standup is still 9am local the week after
    (nxt,) = feed.events_between(text, date(2026, 11, 2), 1)
    assert nxt.start.hour == 9


def test_monthly_recurrence_skips_months_without_the_day():
    text = ics("UID:k\nSUMMARY:Rent\nDTSTART:20260131T090000\nDTEND:20260131T091500\nRRULE:FREQ=MONTHLY")
    assert feed.events_between(text, date(2026, 2, 1), 28) == []           # no Feb 31
    (march,) = feed.events_between(text, date(2026, 3, 31), 1)
    assert march.start.month == 3


def test_exdate_and_recurrence_id_override():
    text = ics("UID:m\nSUMMARY:Standup\nDTSTART:20260824T090000\nDTEND:20260824T091500\n"
               "RRULE:FREQ=DAILY;COUNT=5\nEXDATE:20260826T090000",
               "UID:m\nRECURRENCE-ID:20260827T090000\nSUMMARY:Standup (moved)\n"
               "DTSTART:20260827T140000\nDTEND:20260827T141500")
    week = feed.events_between(text, date(2026, 8, 24), 7)
    assert [(e.start.day, e.start.hour, e.summary) for e in week] == [
        (24, 9, "Standup"), (25, 9, "Standup"), (27, 14, "Standup (moved)"), (28, 9, "Standup")]


def test_cancelled_events_are_dropped():
    text = ics("UID:n\nSUMMARY:Gone\nSTATUS:CANCELLED\nDTSTART:20260826T090000\nDTEND:20260826T100000")
    assert feed.events_between(text, date(2026, 8, 26), 1) == []


def test_event_straddling_midnight_shows_on_both_days():
    text = ics("UID:o\nSUMMARY:Red-eye\nDTSTART:20260826T230000\nDTEND:20260827T020000")
    assert names(feed.events_between(text, date(2026, 8, 26), 1)) == ["Red-eye"]
    assert names(feed.events_between(text, date(2026, 8, 27), 1)) == ["Red-eye"]


def test_multi_day_all_day_event_covers_every_day():
    text = ics("UID:p\nSUMMARY:Trip\nDTSTART;VALUE=DATE:20260825\nDTEND;VALUE=DATE:20260828")
    for day in (25, 26, 27):
        assert names(feed.events_between(text, date(2026, 8, day), 1)) == ["Trip"]
    assert feed.events_between(text, date(2026, 8, 28), 1) == []


def test_sorted_with_all_day_first():
    text = ics("UID:q\nSUMMARY:Late\nDTSTART:20260826T150000\nDTEND:20260826T160000",
               "UID:r\nSUMMARY:Early\nDTSTART:20260826T080000\nDTEND:20260826T090000",
               "UID:s\nSUMMARY:Whole day\nDTSTART;VALUE=DATE:20260826\nDTEND;VALUE=DATE:20260827")
    assert names(feed.events_between(text, date(2026, 8, 26), 1)) == ["Whole day", "Early", "Late"]


def test_a_malformed_event_does_not_take_the_others_down():
    text = ics("UID:t\nSUMMARY:Broken\nDTSTART:not-a-date",
               "UID:u\nSUMMARY:Fine\nDTSTART:20260826T080000\nDTEND:20260826T090000")
    assert names(feed.events_between(text, date(2026, 8, 26), 1)) == ["Fine"]


def test_unknown_tzid_falls_back_to_the_configured_zone():
    text = ics("UID:v\nSUMMARY:Outlook\nDTSTART;TZID=Central Standard Time:20260826T080000\n"
               "DTEND;TZID=Central Standard Time:20260826T090000")
    (e,) = feed.events_between(text, date(2026, 8, 26), 1)
    assert e.start.hour == 8 and e.start.tzinfo == ZoneInfo("America/Chicago")


def test_crlf_line_endings_and_quoted_colons_in_params():
    text = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:w\r\n"
            'X-LOC;X-TITLE="Room: 4":geo:1,2\r\n'
            "SUMMARY:Meet\r\nDTSTART:20260826T080000\r\nDTEND:20260826T090000\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n")
    assert names(feed.events_between(text, date(2026, 8, 26), 1)) == ["Meet"]
