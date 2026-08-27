# Daily Briefing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wren can answer "what's on my calendar" and "what's the weather", and
once a day, unprompted, sends the owner a briefing composed from calendar,
reminders, weather and the shopping list — with no LLM call anywhere in it.

**Architecture:** Three new skills. `calendar_skill` fetches ICS feed URLs
(cached) and parses them with a stdlib parser in `calendar_feed.py` that
expands recurrences inside a requested window. `weather_skill` reads
Open-Meteo (no key). `briefing_skill` composes the two plus
`reminders_store`/`shopping_store` into one message, answers the `briefing`
intent on demand, and runs a daily loop (same shape as `reminder_skill.start`)
that pushes it through `router.notify` at `BRIEFING_TIME`.

**Tech Stack:** Python 3.12, stdlib (`re`, `datetime`, `zoneinfo`, `calendar`),
`httpx` (already a dependency), asyncio, pytest. No new entries in
`requirements.txt`.

**Spec:** `docs/superpowers/specs/2026-08-26-daily-briefing-design.md`

## Global Constraints

From `CLAUDE.md` and the spec. Every task implicitly includes these.

- **A skill never imports a transport.** Skills touch `ctx.channel` only.
  `httpx` is a data source, not a transport, and is already used the same way
  by `wren/skills/web_search.py`.
- **No new dependencies.** ICS parsing is hand-rolled stdlib with a named
  ceiling (see Task 1). `dateutil`/`icalendar` are NOT installed — do not
  import them.
- **No LLM call in the briefing.** `briefing_skill.compose` never touches
  `brain`. A test pins this.
- **The classifier prompt stays small.** One `PROMPT_GUIDELINES` line per
  intent; the skills parse "week"/"tomorrow" from `ctx.text` themselves.
- **`CALENDAR_URLS` is a capability URL → `SECRET_KEYS`, never `SETTABLE`.**
  `GET /api/plugins` returns every `SETTABLE` value. `manage.sh` and
  `chat.html`'s `SECRET_INFO` mirror the list and must be updated together
  (`tests/test_webchat_plugins.py` checks the sets stay disjoint).
- **Log the feed host, never the URL.** The path of a private ICS URL is the
  secret.
- **Every new `SETTABLE` key gets a `SETTING_META` entry in `chat.html`**
  (`{ group, label, help, num? }`, ~line 1043) under a `"Day"` group added to
  `SETTING_GROUPS` (~line 1071, before `"Other"`). Without it the plugins panel
  renders the key as an unlabelled text box under "Other" — the memory slice's
  final review caught exactly this omission.
- **Blocking I/O runs under `asyncio.to_thread`** (see `core.py`'s comment on
  why: the event loop is shared with the Discord gateway heartbeat).
- **Never run anything against the real `wren.db`.** `tests/conftest.py`
  isolates every test; manual checks must `export WREN_DB=$(mktemp -d)/scratch.db`.
- **Every test module starts with the env preamble:**
  ```python
  import os
  os.environ.setdefault("DISCORD_TOKEN", "test")
  os.environ.setdefault("WREN_OWNER_ID", "1")
  os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
  os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
  os.environ.setdefault("LMSTUDIO_MODEL", "test-model")
  os.environ.setdefault("TIMEZONE", "UTC")
  ```
- **No network in tests.** Patch `httpx.get` on the module under test.
- **Comments explain why**, matching `reminder_skill.py`'s style.
- Test invocations below say `venv/bin/python3`; in a worktree use the
  absolute `/home/winter/work/Wren/venv/bin/python3`.

---

### Task 1: The ICS parser

**Files:**
- Create: `wren/skills/calendar_feed.py`
- Test: `tests/test_calendar_feed.py`

**Interfaces:**
- Consumes: `config.TIMEZONE` (str, IANA name).
- Produces: `Event` dataclass (`summary: str, start: datetime, end: datetime,
  all_day: bool`; `start`/`end` tz-aware in `config.TIMEZONE`) and
  `events_between(text: str, start: date, days: int) -> list[Event]` — every
  instance (recurrences expanded) overlapping `[start, start+days)`, sorted by
  start with all-day first on ties.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run them and watch them fail**

Run: `venv/bin/python3 -m pytest -q tests/test_calendar_feed.py`
Expected: FAIL — `ImportError: cannot import name 'calendar_feed'`

- [ ] **Step 3: Write the parser**

```python
"""A small ICS reader: enough of RFC 5545 for a personal calendar feed.

Stdlib only, on purpose. requirements.txt is six lines because people are
meant to self-host Wren from it, and the two libraries that would do this
properly (icalendar, dateutil) are the kind of dependency that pulls in a
tree. The ceiling is documented on _expand; if it is ever hit, swap that one
function for dateutil.rrule and leave the rest alone.
"""
import calendar as _cal
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import config


@dataclass(frozen=True)
class Event:
    summary: str
    start: datetime      # tz-aware, in config.TIMEZONE
    end: datetime        # exclusive; all-day events end at the next midnight
    all_day: bool


_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_DURATION = re.compile(
    r"^(-)?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def _local() -> ZoneInfo:
    return ZoneInfo(config.TIMEZONE)


def _unfold(text: str) -> list[str]:
    # RFC 5545 3.1: a line beginning with a space or tab continues the one before.
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def _split(line: str) -> tuple[str, str]:
    """Name+params, value — split on the first colon outside double quotes.

    Parameter values may be quoted and contain colons (Apple's structured
    locations do); a plain partition would cut the line inside the quotes.
    """
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            return line[:i], line[i + 1:]
    return line, ""


def _vevents(lines: list[str]) -> list[dict[str, list[tuple[dict, str]]]]:
    """Each VEVENT as {NAME: [(params, value), ...]} — a list per name because
    EXDATE may repeat."""
    events, current = [], None
    for line in lines:
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
        elif current is not None:
            head, value = _split(line)
            name, *params = head.split(";")
            pdict = {}
            for p in params:
                k, _, v = p.partition("=")
                pdict[k.upper()] = v.strip('"')
            current.setdefault(name.upper(), []).append((pdict, value))
    return events


def _unescape(value: str) -> str:
    return (value.replace("\\n", "\n").replace("\\N", "\n")
                 .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def _zone(tzid: str) -> ZoneInfo:
    try:
        return ZoneInfo(tzid)
    except Exception:
        # Outlook exports Windows zone names ("Central Standard Time"). Reading
        # the event in the configured zone is a better lie than dropping it.
        return _local()


def _parse_dt(params: dict, value: str) -> tuple[datetime, bool]:
    """(aware datetime in the local zone, all_day)."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or len(value) == 8:
        d = datetime.strptime(value, "%Y%m%d")
        return d.replace(tzinfo=_local()), True
    naive = datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        dt = naive.replace(tzinfo=timezone.utc)
    elif "TZID" in params:
        dt = naive.replace(tzinfo=_zone(params["TZID"]))
    else:
        dt = naive.replace(tzinfo=_local())     # floating: the reader's zone
    return dt.astimezone(_local()), False


def _parse_duration(value: str) -> timedelta | None:
    m = _DURATION.match(value.strip())
    if not m:
        return None
    sign, w, d, h, mi, s = m.groups()
    td = timedelta(weeks=int(w or 0), days=int(d or 0), hours=int(h or 0),
                   minutes=int(mi or 0), seconds=int(s or 0))
    return -td if sign else td


def _rrule(value: str) -> dict[str, str]:
    return {k.upper(): v for k, _, v in (p.partition("=") for p in value.split(";")) if k}


def _shift(start: datetime, freq: str, n: int) -> datetime | None:
    """`start` moved n periods forward, on the wall clock.

    Aware-datetime arithmetic in Python is wall-clock arithmetic, which is
    exactly right here: a 9am weekly standup is still 9am after the clocks
    change. None when the month has no such day (RFC: skip, do not clamp).
    """
    if freq == "DAILY":
        return start + timedelta(days=n)
    if freq == "WEEKLY":
        return start + timedelta(weeks=n)
    if freq == "MONTHLY":
        month0 = start.month - 1 + n
        year, month = start.year + month0 // 12, month0 % 12 + 1
    else:  # YEARLY
        year, month = start.year + n, start.month
    if start.day > _cal.monthrange(year, month)[1]:
        return None
    return start.replace(year=year, month=month)


def _expand(start: datetime, rule: dict, window_end: datetime) -> list[datetime]:
    """Instances of a recurrence from its DTSTART up to (not including) window_end.

    ponytail: FREQ, INTERVAL, COUNT, UNTIL, and BYDAY for WEEKLY only. BYDAY
    ordinals (2MO), BYMONTHDAY, BYSETPOS and WKST are ignored, so a "second
    Monday" rule shows on its DTSTART and every plain-monthly instance instead.
    Swap this function for dateutil.rrule if that ever matters.
    """
    freq = rule.get("FREQ", "").upper()
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        return [start]
    interval = max(1, int(rule.get("INTERVAL", "1")))
    count = int(rule["COUNT"]) if "COUNT" in rule else None
    until = None
    if "UNTIL" in rule:
        until, all_day = _parse_dt({}, rule["UNTIL"])
        if all_day:
            until += timedelta(days=1) - timedelta(seconds=1)   # inclusive of that day

    out: list[datetime] = []
    n = 0
    if freq == "WEEKLY" and "BYDAY" in rule:
        days = sorted(_WEEKDAYS[d] for d in rule["BYDAY"].split(",") if d in _WEEKDAYS) or [start.weekday()]
        week0 = start - timedelta(days=start.weekday())
        w = 0
        while True:
            monday = week0 + timedelta(weeks=w * interval)
            if monday >= window_end:
                return out
            for wd in days:
                occ = monday + timedelta(days=wd)
                if occ < start:
                    continue
                if (until and occ > until) or (count is not None and n >= count):
                    return out
                n += 1
                if occ >= window_end:
                    return out
                out.append(occ)
            w += 1

    step = 0
    while True:
        occ = _shift(start, freq, step * interval)
        step += 1
        if occ is None:
            if step > 12 * 400:      # a MONTHLY rule on the 31st can skip; never spin forever
                return out
            continue
        if occ >= window_end or (until and occ > until) or (count is not None and n >= count):
            return out
        n += 1
        out.append(occ)


def _instances(ev: dict, w_start: datetime, w_end: datetime,
               overridden: set[tuple[str, datetime]]) -> list[Event]:
    if ev.get("STATUS", [({}, "")])[0][1].upper() == "CANCELLED" or "DTSTART" not in ev:
        return []
    summary = _unescape(ev.get("SUMMARY", [({}, "(untitled)")])[0][1]).strip() or "(untitled)"
    start, all_day = _parse_dt(*ev["DTSTART"][0])
    if "DTEND" in ev:
        end, _ = _parse_dt(*ev["DTEND"][0])
    elif "DURATION" in ev:
        end = start + (_parse_duration(ev["DURATION"][0][1]) or timedelta())
    else:
        # RFC 5545 3.6.1: no DTEND means a one-day all-day event, or an instant
        end = start + timedelta(days=1) if all_day else start
    length = end - start
    starts = _expand(start, _rrule(ev["RRULE"][0][1]), w_end) if "RRULE" in ev else [start]
    exdates = {_parse_dt(p, one)[0] for p, v in ev.get("EXDATE", []) for one in v.split(",")}
    uid = ev.get("UID", [({}, "")])[0][1]
    out = []
    for s in starts:
        if s in exdates or (uid, s) in overridden:
            continue
        e = s + length
        # overlap test; `or s >= w_start` keeps a zero-length instant on its day
        if s < w_end and (e > w_start or s >= w_start):
            out.append(Event(summary, s, e, all_day))
    return out


def events_between(text: str, start: date, days: int) -> list[Event]:
    """Every instance overlapping [start, start + days), recurrences expanded."""
    w_start = datetime.combine(start, time.min, tzinfo=_local())
    w_end = w_start + timedelta(days=days)
    vevents = _vevents(_unfold(text))

    # An override (same UID + RECURRENCE-ID) replaces one instance of its
    # master. Collect them first so the master knows which instance to drop.
    overridden: set[tuple[str, datetime]] = set()
    for ev in vevents:
        if "RECURRENCE-ID" in ev and "UID" in ev:
            try:
                overridden.add((ev["UID"][0][1], _parse_dt(*ev["RECURRENCE-ID"][0])[0]))
            except ValueError:
                pass

    out: list[Event] = []
    for ev in vevents:
        try:
            out.extend(_instances(ev, w_start, w_end, overridden))
        except (ValueError, KeyError, IndexError, OverflowError) as e:
            # one bad event must not take the feed down; the rest still parse
            logging.debug(f"calendar: skipped a malformed event: {e}")
    out.sort(key=lambda e: (e.start, not e.all_day, e.summary))
    return out
```

- [ ] **Step 4: Run the tests**

Run: `venv/bin/python3 -m pytest -q tests/test_calendar_feed.py`
Expected: PASS (18 tests)

- [ ] **Step 5: Commit**

```bash
git add wren/skills/calendar_feed.py tests/test_calendar_feed.py
git commit -m "feat(calendar): stdlib ICS parser with windowed recurrence expansion"
```

---

### Task 2: The calendar skill

**Files:**
- Create: `wren/skills/calendar_skill.py`
- Modify: `wren/config.py` (constants beside `REMINDER_POLL_SECONDS`, `SETTABLE`,
  `SECRET_KEYS`), `wren/registry.py` (`PLUGINS`), `manage.sh` (`SECRET_KEYS`),
  `wren/communication/chat.html` (`SKILL_INFO`, `SECRET_INFO`), `.env.example`,
  `wren/core.py` (`HELP_TEXT`), `README.md`
- Test: `tests/test_calendar_skill.py`, `tests/test_config_overrides.py`

**Interfaces:**
- Consumes: `calendar_feed.events_between(text, start, days)`, `calendar_feed.Event`.
- Produces: `config.CALENDAR_URLS: list[str]`, `config.CALENDAR_CACHE_SECONDS: int`;
  `calendar_skill.FeedError`, `calendar_skill.is_active()`,
  `calendar_skill.inactive_reason()`,
  `calendar_skill.events_between(start: date, days: int) -> list[Event]` (sync,
  fetches every configured feed, raises `FeedError`),
  `calendar_skill.day_lines(events: list[Event], day: date) -> list[str]`
  (the indented lines for one day, used by the briefing),
  `calendar_skill.format_range(events, start, days, label) -> str`.

- [ ] **Step 1: Write the failing tests**

`tests/test_calendar_skill.py`:

```python
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
```

Append to `tests/test_config_overrides.py` (match its existing style; it
already imports `config`):

```python
def test_calendar_cache_seconds_rejects_zero():
    with pytest.raises(ValueError):
        config.SETTABLE["CALENDAR_CACHE_SECONDS"].coerce("0")
```

- [ ] **Step 2: Run them and watch them fail**

Run: `venv/bin/python3 -m pytest -q tests/test_calendar_skill.py tests/test_config_overrides.py`
Expected: FAIL — `ImportError: cannot import name 'calendar_skill'`

- [ ] **Step 3: Config**

In `wren/config.py`, beside `REMINDER_POLL_SECONDS`:

```python
# Calendar feeds. A private ICS address is a capability URL -- whoever has it
# can read the calendar -- so it is a SECRET_KEY (reported set/not-set only),
# never a SETTABLE value the plugins panel would echo back.
CALENDAR_URLS: list[str] = [u.strip() for u in os.environ.get("CALENDAR_URLS", "").split(",") if u.strip()]
CALENDAR_CACHE_SECONDS = int(os.environ.get("CALENDAR_CACHE_SECONDS", "300"))
```

In `SETTABLE`, after `GITHUB_POLL_SECONDS`:

```python
    "CALENDAR_CACHE_SECONDS": Setting(_coerce_poll_seconds),
```

In `SECRET_KEYS`, append `"CALENDAR_URLS"`. In `manage.sh`'s `SECRET_KEYS=(...)`
array, append `CALENDAR_URLS` (it says it is kept in sync by hand). In
`chat.html`'s `SECRET_INFO`, add:

```javascript
  CALENDAR_URLS:      "Calendar feed URLs (ICS, comma-separated)",
```

In `chat.html`'s `SETTING_META` (after the `FIRECRAWL_URL` entry) add:

```javascript
  CALENDAR_CACHE_SECONDS: { group: "Day", label: "Calendar cache (seconds)",
                            help: "How long a fetched feed is reused before asking again. Minimum 5.", num: true },
```

and insert `"Day"` into `SETTING_GROUPS` before `"Other"`:

```javascript
const SETTING_GROUPS = ["General", "Watchers", "Web lookup", "Day", "Models", "Other"];
```

Add to `tests/test_calendar_skill.py` the same kind of page-source assertion
the existing chat tests use (`grep -n "SETTING_META" tests/` for the pattern):

```python
def test_calendar_cache_setting_is_labelled_in_the_panel():
    from pathlib import Path
    page = (Path(__file__).parent.parent / "wren" / "communication" / "chat.html").read_text()
    assert "CALENDAR_CACHE_SECONDS: { group: \"Day\"" in page
```

- [ ] **Step 4: Write the skill**

```python
import asyncio
import logging
import re
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from .. import config
from ..channel import Ctx
from . import calendar_feed
from .calendar_feed import Event

INTENTS = ["recall_calendar"]
PLUGIN_NAME = "Calendar"

PROMPT_GUIDELINES = """- recall_calendar: user asks what is on their calendar, schedule or agenda, or what a day/week looks like; if they name a specific day set "when" to that date as ISO 8601, otherwise leave it out"""

FETCH_TIMEOUT = 15.0


class FeedError(RuntimeError):
    """A feed could not be fetched. The message names the host only -- the
    path of a private ICS URL is the secret."""


def is_active() -> bool:
    return bool(config.CALENDAR_URLS)


def inactive_reason() -> str:
    return "CALENDAR_URLS is not set — the calendar is disabled."


# url -> (monotonic time fetched, body). A year of events is hundreds of KB;
# the briefing and a question a minute later should not fetch it twice.
# ponytail: in-memory, a restart just refetches.
_cache: dict[str, tuple[float, str]] = {}


def _fetch(url: str) -> str:
    now = time.monotonic()
    hit = _cache.get(url)
    if hit and now - hit[0] < config.CALENDAR_CACHE_SECONDS:
        return hit[1]
    try:
        resp = httpx.get(url, timeout=FETCH_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        # `from None`: httpx puts the full URL in its messages and chained
        # tracebacks, and the URL is the secret
        raise FeedError(f"{urlsplit(url).netloc}: {type(e).__name__}") from None
    _cache[url] = (now, resp.text)
    return resp.text


def events_between(start: date, days: int) -> list[Event]:
    """Every configured feed, merged and sorted. Blocking; call under to_thread.

    ponytail: one dead feed fails the whole read. Show the healthy ones with
    a note if that ever bites.
    """
    out: list[Event] = []
    for url in config.CALENDAR_URLS:
        out.extend(calendar_feed.events_between(_fetch(url), start, days))
    out.sort(key=lambda e: (e.start, not e.all_day, e.summary))
    return out


def _today() -> date:
    return datetime.now(ZoneInfo(config.TIMEZONE)).date()


def _on_day(e: Event, day: date) -> bool:
    day_start = datetime.combine(day, datetime.min.time(), tzinfo=e.start.tzinfo)
    day_end = day_start + timedelta(days=1)
    return e.start < day_end and (e.end > day_start or e.start >= day_start)


def day_lines(events: list[Event], day: date) -> list[str]:
    lines = []
    for e in events:
        if not _on_day(e, day):
            continue
        # padded to the width of "09:00–10:00" so summaries line up
        when = "all day".ljust(11) if e.all_day else f"{e.start:%H:%M}–{e.end:%H:%M}"
        lines.append(f"  {when}  {e.summary}")
    return lines


def _day_label(day: date) -> str:
    return f"{day:%a %b} {day.day}"       # no %-d: not portable


def format_range(events: list[Event], start: date, days: int, label: str) -> str:
    if days == 1:
        lines = day_lines(events, start)
        if not lines:
            return f"Nothing on the calendar {label}."
        return f"{label[:1].upper()}{label[1:]} ({_day_label(start)}):\n" + "\n".join(lines)
    blocks = []
    for i in range(days):
        day = start + timedelta(days=i)
        lines = day_lines(events, day)
        if lines:
            blocks.append(f"{_day_label(day)}:\n" + "\n".join(lines))
    return "\n".join(blocks) if blocks else f"Nothing on the calendar {label}."


_WEEK = re.compile(r"\bweek\b", re.I)
_TOMORROW = re.compile(r"\btomorrow\b", re.I)


def _window(ctx: Ctx) -> tuple[date, int, str]:
    """(anchor day, number of days, label for the reply).

    The model supplies a date when the user named one; "tomorrow" and "week"
    are read straight from the text so the classifier prompt does not have to
    teach a 7B model calendar arithmetic (see reminder_skill._relative_when
    for why that matters).
    """
    today = _today()
    anchor = today
    if ctx.when:
        try:
            anchor = date.fromisoformat(ctx.when.strip()[:10])
        except ValueError:
            pass
    elif _TOMORROW.search(ctx.text or ""):
        anchor = today + timedelta(days=1)
    if _WEEK.search(ctx.text or ""):
        return anchor, 7, "this week" if anchor == today else f"the week of {_day_label(anchor)}"
    if anchor == today:
        label = "today"
    elif anchor == today + timedelta(days=1):
        label = "tomorrow"
    else:
        label = _day_label(anchor)
    return anchor, 1, label


async def handle(intent: str, ctx: Ctx) -> None:
    anchor, days, label = _window(ctx)
    try:
        events = await asyncio.to_thread(events_between, anchor, days)
    except FeedError as e:
        logging.warning(f"calendar: {e}")
        await ctx.channel.send("Couldn't reach the calendar feed.")
        return
    await ctx.channel.send(format_range(events, anchor, days, label))
```

- [ ] **Step 5: Register and document**

`wren/registry.py`: `from .skills import calendar_skill` and append
`calendar_skill` to `PLUGINS`.

`wren/core.py` `HELP_TEXT`, a new block after **Reminders**:

```
**Calendar** (needs CALENDAR_URLS)
- "what's on my calendar" / "what does tomorrow look like" / "show my week"
```

`chat.html` `SKILL_INFO`:

```javascript
  calendar_skill: "Calendar — what's on today, tomorrow or this week, read from your ICS feed(s).",
```

`.env.example`, after the Web lookup block:

```
# Calendar (optional — leave CALENDAR_URLS unset to disable)
# One or more ICS feed URLs, comma-separated. Google: calendar settings ->
# "Secret address in iCal format". iCloud/Nextcloud/Outlook all export one.
# This address IS the calendar: anyone holding it can read every event, which
# is why it is managed like a token (manage.sh secret set CALENDAR_URLS ...)
# and never shown in the plugins panel.
# CALENDAR_URLS=https://calendar.google.com/calendar/ical/.../private-.../basic.ics
# CALENDAR_CACHE_SECONDS=300     # how long a fetched feed is reused
```

`README.md`, a new section after "## Web lookup (optional)":

```
## Calendar (optional)

Set `CALENDAR_URLS` in `.env` to one or more ICS feed URLs (comma-separated).
Every mainstream calendar exports one — in Google Calendar it is the "Secret
address in iCal format" under the calendar's settings. Treat it like a
password: it is managed with `manage.sh secret set CALENDAR_URLS …` and the
plugins panel only ever says whether it is set.

- "what's on my calendar" / "what does today look like"
- "anything tomorrow?" / "what's on Friday"
- "show my week"

Read-only: Wren never creates or edits events. Recurring events are expanded
for the day you asked about (daily/weekly/monthly/yearly, with weekday lists,
exceptions and moved instances); exotic rules like "second Monday" fall back
to the plain frequency.
```

- [ ] **Step 6: Run the whole suite**

Run: `venv/bin/python3 -m pytest -q`
Expected: PASS — `registry.PLUGINS` and `SECRET_KEYS` changed, so the whole
suite matters (`tests/test_webchat_plugins.py` checks `SECRET_KEYS` vs `SETTABLE`).

- [ ] **Step 7: Commit**

```bash
git add wren/skills/calendar_skill.py wren/config.py wren/registry.py wren/core.py \
        wren/communication/chat.html manage.sh .env.example README.md \
        tests/test_calendar_skill.py tests/test_config_overrides.py
git commit -m "feat(calendar): recall_calendar skill over cached ICS feeds"
```

---

### Task 3: The weather skill

**Files:**
- Create: `wren/skills/weather_skill.py`
- Modify: `wren/config.py`, `wren/registry.py`, `wren/skills/web_skill.py:12`
  (`PROMPT_GUIDELINES`), `wren/core.py` (`HELP_TEXT`),
  `wren/communication/chat.html` (`SKILL_INFO`), `.env.example`, `README.md`
- Test: `tests/test_weather_skill.py`, `tests/test_config_overrides.py`

**Interfaces:**
- Consumes: `config.TIMEZONE`.
- Produces: `config.WEATHER_LAT: float | None`, `config.WEATHER_LON: float | None`,
  `config.WEATHER_UNITS: str` (`"fahrenheit"` | `"celsius"`);
  `weather_skill.is_active()`, `weather_skill.inactive_reason()`,
  `weather_skill.forecast() -> dict` (sync, cached), `weather_skill.summary(day: int = 0) -> str`.

- [ ] **Step 1: Write the failing tests**

`tests/test_weather_skill.py`:

```python
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


def test_handle_reports_a_dead_service():
    with patch.object(weather_skill.httpx, "get", side_effect=httpx.ConnectError("nope")):
        ctx = _ctx("weather?")
        asyncio.run(weather_skill.handle("get_weather", ctx))
    assert ctx.channel.sent == ["Couldn't reach the weather service."]


def test_web_search_guideline_no_longer_claims_weather():
    from wren.skills import web_skill
    assert "weather" not in web_skill.PROMPT_GUIDELINES.lower()
```

Append to `tests/test_config_overrides.py`:

```python
def test_weather_coordinates_are_range_checked():
    assert config.SETTABLE["WEATHER_LAT"].coerce("41.88") == 41.88
    assert config.SETTABLE["WEATHER_LAT"].coerce("") is None
    with pytest.raises(ValueError):
        config.SETTABLE["WEATHER_LAT"].coerce("91")
    with pytest.raises(ValueError):
        config.SETTABLE["WEATHER_LON"].coerce("-181")


def test_weather_units_is_an_enum():
    assert config.SETTABLE["WEATHER_UNITS"].coerce(" Celsius ") == "celsius"
    with pytest.raises(ValueError):
        config.SETTABLE["WEATHER_UNITS"].coerce("kelvin")
```

- [ ] **Step 2: Run them and watch them fail**

Run: `venv/bin/python3 -m pytest -q tests/test_weather_skill.py tests/test_config_overrides.py`
Expected: FAIL — `ImportError: cannot import name 'weather_skill'`

- [ ] **Step 3: Config**

In `wren/config.py`, beside the calendar constants:

```python
def _coerce_coordinate(limit: float):
    def coerce(raw: str) -> float | None:
        raw = (raw or "").strip()
        if not raw:
            return None            # unset -> weather skill inactive
        value = float(raw)
        if not -limit <= value <= limit:
            raise ValueError(f"must be between -{limit} and {limit}")
        return value
    return coerce


_coerce_lat = _coerce_coordinate(90.0)
_coerce_lon = _coerce_coordinate(180.0)


def _coerce_units(raw: str) -> str:
    value = (raw or "").strip().lower()
    if value not in ("fahrenheit", "celsius"):
        raise ValueError("WEATHER_UNITS must be fahrenheit or celsius")
    return value


WEATHER_LAT: float | None = _coerce_lat(os.environ.get("WEATHER_LAT", ""))
WEATHER_LON: float | None = _coerce_lon(os.environ.get("WEATHER_LON", ""))
WEATHER_UNITS: str = _coerce_units(os.environ.get("WEATHER_UNITS", "fahrenheit"))
```

In `SETTABLE`, after `CALENDAR_CACHE_SECONDS`:

```python
    "WEATHER_LAT":   Setting(_coerce_lat, serialize=lambda v: "" if v is None else str(v)),
    "WEATHER_LON":   Setting(_coerce_lon, serialize=lambda v: "" if v is None else str(v)),
    "WEATHER_UNITS": Setting(_coerce_units),
```

- [ ] **Step 4: Write the skill**

```python
import asyncio
import logging
import re
import time

import httpx

from .. import config
from ..channel import Ctx

INTENTS = ["get_weather"]
PLUGIN_NAME = "Weather"

PROMPT_GUIDELINES = """- get_weather: user asks about the weather, temperature, rain or the forecast for here, now, today or tomorrow"""

_URL = "https://api.open-meteo.com/v1/forecast"
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


async def handle(intent: str, ctx: Ctx) -> None:
    day = 1 if _TOMORROW.search(ctx.text or "") else 0
    try:
        text = await asyncio.to_thread(summary, day)
    except (httpx.HTTPError, KeyError, IndexError, TypeError) as e:
        logging.warning(f"weather: {type(e).__name__}: {e}")
        await ctx.channel.send("Couldn't reach the weather service.")
        return
    await ctx.channel.send(text)
```

- [ ] **Step 5: Register, retarget web search, document**

`wren/registry.py`: import `weather_skill`, append to `PLUGINS` (after
`calendar_skill`).

`wren/skills/web_skill.py` `PROMPT_GUIDELINES`, first line: change
`news, weather, prices,` to `news, prices,` — with a weather skill registered,
the word there would steer the classifier the wrong way.

`wren/core.py` `HELP_TEXT`, after the Calendar block:

```
**Weather** (needs WEATHER_LAT / WEATHER_LON)
- "what's the weather" / "will it rain tomorrow"
```

`chat.html` `SKILL_INFO`:

```javascript
  weather_skill:  "Weather — now and tomorrow for your coordinates (Open-Meteo, no key needed).",
```

`chat.html` `SETTING_META`, after the `CALENDAR_CACHE_SECONDS` entry:

```javascript
  WEATHER_LAT:   { group: "Day", label: "Latitude",
                   help: "Decimal degrees, e.g. 41.88. Empty disables weather.", num: true },
  WEATHER_LON:   { group: "Day", label: "Longitude",
                   help: "Decimal degrees, e.g. -87.63.", num: true },
  WEATHER_UNITS: { group: "Day", label: "Units",
                   help: "fahrenheit or celsius." },
```

Add to `tests/test_weather_skill.py`:

```python
def test_weather_settings_are_labelled_in_the_panel():
    from pathlib import Path
    page = (Path(__file__).parent.parent / "wren" / "communication" / "chat.html").read_text()
    for key in ("WEATHER_LAT", "WEATHER_LON", "WEATHER_UNITS"):
        assert f'{key}:' in page and 'group: "Day"' in page
```

`.env.example`, after the Calendar block:

```
# Weather (optional — leave WEATHER_LAT/LON unset to disable). Open-Meteo,
# no account or key. Decimal degrees; also settable in the plugins panel.
# WEATHER_LAT=41.88
# WEATHER_LON=-87.63
# WEATHER_UNITS=fahrenheit      # or celsius
```

`README.md`, a new section after "## Calendar (optional)":

```
## Weather (optional)

Set `WEATHER_LAT` and `WEATHER_LON` (decimal degrees) in `.env` or the
plugins panel. Forecasts come from Open-Meteo — no account, no key, and the
only thing sent is the coordinates.

- "what's the weather" — now, plus today's high, low and rain chance
- "will it rain tomorrow" — tomorrow's outlook
```

- [ ] **Step 6: Run the whole suite**

Run: `venv/bin/python3 -m pytest -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add wren/skills/weather_skill.py wren/skills/web_skill.py wren/config.py wren/registry.py \
        wren/core.py wren/communication/chat.html .env.example README.md \
        tests/test_weather_skill.py tests/test_config_overrides.py
git commit -m "feat(weather): get_weather skill on Open-Meteo, no key"
```

---

### Task 4: The briefing — compose and the intent

**Files:**
- Create: `wren/skills/briefing_skill.py`
- Modify: `wren/registry.py`, `wren/core.py` (`HELP_TEXT`),
  `wren/communication/chat.html` (`SKILL_INFO`)
- Test: `tests/test_briefing_skill.py`

**Interfaces:**
- Consumes: `calendar_skill.is_active/events_between/day_lines/FeedError`,
  `weather_skill.is_active/summary`, `reminders_store.pending(owner_id)`
  (rows with `content`, `fire_at` — UTC ISO), `shopping_store.active_items()`,
  `registry.is_enabled(plugin)`.
- Produces: `briefing_skill.compose(user_id: int) -> str` (sync, no LLM) and
  `handle` for the `briefing` intent. `start()` is added in Task 5.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run and watch them fail**

Run: `venv/bin/python3 -m pytest -q tests/test_briefing_skill.py`
Expected: FAIL — `ImportError: cannot import name 'briefing_skill'`

- [ ] **Step 3: Write compose and the handler**

```python
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from .. import config
from ..channel import Ctx
from . import calendar_skill
from . import reminder_skill
from . import reminders_store as reminders
from . import shopping_skill
from . import shopping_store as shopping
from . import weather_skill

INTENTS = ["briefing"]
PLUGIN_NAME = "Briefing"

PROMPT_GUIDELINES = """- briefing: user asks for their briefing, a rundown or overview of the day ("what's my day look like", "morning briefing", "catch me up")"""


def _now() -> datetime:
    return datetime.now(ZoneInfo(config.TIMEZONE))


def _greeting(now: datetime) -> str:
    if now.hour < 12:
        return "Good morning."
    if now.hour < 17:
        return "Good afternoon."
    return "Good evening."


def compose(user_id: int) -> str:
    """The day in one message. Blocking (feeds, HTTP); call under to_thread.

    Composed, never generated: nothing here touches brain. A briefing that
    invents a meeting is worse than no briefing, and a deterministic one is
    free to send every morning. Each source is wrapped on its own so one dead
    feed costs one line, not the whole message.
    """
    from .. import registry   # local: registry imports this module

    tz = ZoneInfo(config.TIMEZONE)
    now = _now().astimezone(tz)
    today = now.date()
    lines = [f"{_greeting(now)} {today:%a %b} {today.day}."]
    empties: list[str] = []

    if registry.is_enabled(weather_skill) and weather_skill.is_active():
        try:
            lines.append("Weather: " + weather_skill.summary(0))
        except Exception as e:
            logging.warning(f"briefing: weather failed: {type(e).__name__}: {e}")
            lines.append("Weather: couldn't reach the weather service.")

    if registry.is_enabled(calendar_skill) and calendar_skill.is_active():
        try:
            day = calendar_skill.day_lines(calendar_skill.events_between(today, 1), today)
        except calendar_skill.FeedError as e:
            logging.warning(f"briefing: calendar failed: {e}")
            lines.append("Calendar: couldn't reach the feed.")
        else:
            if day:
                lines.append("Calendar:")
                lines.extend(day)
            else:
                empties.append("nothing on the calendar")

    if registry.is_enabled(reminder_skill):
        due = []
        for r in reminders.pending(user_id):
            local = datetime.fromisoformat(r["fire_at"]).astimezone(tz)
            if local.date() == today:
                due.append(f"  {local:%H:%M}  {r['content']}")
        if due:
            lines.append("Reminders today:")
            lines.extend(due)
        else:
            empties.append("no reminders")

    if registry.is_enabled(shopping_skill):
        n = len(shopping.active_items())
        if n:
            lines.append(f"Shopping list: {n} item{'' if n == 1 else 's'}.")
        else:
            empties.append("list is empty")

    # Only when every data section came up empty: a briefing that lists what
    # it does not have reads as an apology; one that says nothing reads as broken.
    if empties and not any(l.startswith(("Calendar", "Reminders", "Shopping")) for l in lines):
        sentence = ", ".join(empties)
        lines.append(sentence[:1].upper() + sentence[1:] + ".")
    return "\n".join(lines)


async def handle(intent: str, ctx: Ctx) -> None:
    await ctx.channel.send(await asyncio.to_thread(compose, ctx.user_id))
```

- [ ] **Step 4: Register and document**

`wren/registry.py`: import `briefing_skill`, append to `PLUGINS` (last).

`wren/core.py` `HELP_TEXT`, after the Weather block:

```
**Briefing**
- "what's my day look like" — weather, calendar, today's reminders and the shopping list in one message
```

`chat.html` `SKILL_INFO`:

```javascript
  briefing_skill: "Briefing — your day in one message, on demand and (optionally) every morning.",
```

- [ ] **Step 5: Run the whole suite**

Run: `venv/bin/python3 -m pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add wren/skills/briefing_skill.py wren/registry.py wren/core.py wren/communication/chat.html tests/test_briefing_skill.py
git commit -m "feat(briefing): compose the day from calendar, weather, reminders and the list"
```

---

### Task 5: The daily push

**Files:**
- Modify: `wren/skills/briefing_skill.py`, `wren/config.py`,
  `wren/communication/chat.html` (`SETTING_META`), `.env.example`, `README.md`
- Test: `tests/test_briefing_skill.py`, `tests/test_config_overrides.py`

**Interfaces:**
- Consumes: `compose` (Task 4), `router.notify(user_id, text) -> bool`,
  `config.WHITELIST["owner"]`.
- Produces: `config.BRIEFING_TIME: str` (`""` or `"HH:MM"`),
  `briefing_skill._should_fire(now, hhmm, last_sent) -> bool`, `briefing_skill.start()`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_briefing_skill.py`:

```python
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
```

Append to `tests/test_config_overrides.py`:

```python
def test_briefing_time_is_hhmm_or_empty():
    assert config.SETTABLE["BRIEFING_TIME"].coerce("") == ""
    assert config.SETTABLE["BRIEFING_TIME"].coerce("7:05") == "07:05"
    with pytest.raises(ValueError):
        config.SETTABLE["BRIEFING_TIME"].coerce("25:00")
    with pytest.raises(ValueError):
        config.SETTABLE["BRIEFING_TIME"].coerce("morning")
```

- [ ] **Step 2: Run and watch them fail**

Run: `venv/bin/python3 -m pytest -q tests/test_briefing_skill.py tests/test_config_overrides.py -k "fire or start or briefing_time"`
Expected: FAIL — `AttributeError: module 'wren.skills.briefing_skill' has no attribute '_should_fire'`

- [ ] **Step 3: Config**

In `wren/config.py`, beside the weather constants:

```python
def _coerce_hhmm(raw: str) -> str:
    """"" (off) or a normalised HH:MM in the configured TIMEZONE."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not m or not (0 <= int(m.group(1)) <= 23 and 0 <= int(m.group(2)) <= 59):
        raise ValueError("BRIEFING_TIME must be HH:MM (24-hour) or empty to switch it off")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


BRIEFING_TIME: str = _coerce_hhmm(os.environ.get("BRIEFING_TIME", ""))
```

(`import re` at the top of `config.py` if it is not already there.) In
`SETTABLE`, after `WEATHER_UNITS`:

```python
    "BRIEFING_TIME": Setting(_coerce_hhmm),
```

- [ ] **Step 4: Write the loop**

Add to `briefing_skill.py` (imports: `from datetime import date, datetime, timedelta`,
`from .. import router`):

```python
_STEP = 60                      # seconds between checks; also how quickly a panel change applies
_WINDOW = timedelta(minutes=10)  # fire only this long after BRIEFING_TIME; later means a restart, not a morning
_last_sent: date | None = None
# ponytail: process-local. A restart inside the ten-minute window could send
# twice; persist the date in settings if that is ever observed.


def _should_fire(now: datetime, hhmm: str, last_sent: date | None) -> bool:
    if not hhmm or last_sent == now.date():
        return False
    hour, minute = (int(x) for x in hhmm.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return target <= now < target + _WINDOW


async def start() -> None:
    """Once a day, at BRIEFING_TIME local, push the briefing to the owner.

    Sleeps first and re-reads the setting every step: the queue of things to
    say is empty at boot, and a time changed in the plugins panel should take
    effect without a restart. `_last_sent` is set only after notify returns,
    so a transient failure is retried next step, inside the window.
    """
    global _last_sent
    while True:
        await asyncio.sleep(_STEP)
        try:
            now = _now()
            if not _should_fire(now, config.BRIEFING_TIME, _last_sent):
                continue
            owner = config.WHITELIST["owner"]
            text = await asyncio.to_thread(compose, owner)
            ok = await router.notify(owner, text)
            if not ok:
                logging.warning("briefing: owner unreachable, skipping today")
            _last_sent = now.date()
        except Exception as e:
            logging.warning(f"briefing loop failed: {e}")
```

- [ ] **Step 5: Document**

`.env.example`, after the Weather block:

```
# Daily briefing (optional). Local time, 24-hour, in TIMEZONE. When set, Wren
# sends the owner one message a day via NOTIFY_VIA with the weather, today's
# calendar and reminders, and the shopping list. Also settable in the panel.
# BRIEFING_TIME=07:30
```

`chat.html` `SETTING_META`, after the `WEATHER_UNITS` entry:

```javascript
  BRIEFING_TIME: { group: "Day", label: "Daily briefing at",
                   help: "HH:MM in your timezone. Empty switches the daily push off; asking still works." },
```

Add to `tests/test_briefing_skill.py`:

```python
def test_briefing_time_is_labelled_in_the_panel():
    from pathlib import Path
    page = (Path(__file__).parent.parent / "wren" / "communication" / "chat.html").read_text()
    assert 'BRIEFING_TIME: { group: "Day"' in page
```

`README.md`, a new section after "## Weather (optional)":

```
## Daily briefing (optional)

Ask "what's my day look like" any time, or set `BRIEFING_TIME` (e.g. `07:30`,
in your `TIMEZONE`) and Wren sends it unprompted every morning through
`NOTIFY_VIA`:

    Good morning. Wed Aug 26.
    Weather: 72°F and partly cloudy, wind 8 mph. High 81, low 63, 20% chance of rain.
    Calendar:
      09:00–09:30  Standup
      13:00–14:00  Dentist
    Reminders today:
      17:00  call the vet
    Shopping list: 6 items.

Sections that have nothing to say are left out. No model is involved — the
briefing is assembled from what the calendar, weather, reminders and shopping
skills already know, so it cannot invent an appointment. Switching any of
those skills off in the plugins panel drops its section.
```

- [ ] **Step 6: Run the whole suite**

Run: `venv/bin/python3 -m pytest -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add wren/skills/briefing_skill.py wren/config.py wren/communication/chat.html .env.example README.md \
        tests/test_briefing_skill.py tests/test_config_overrides.py
git commit -m "feat(briefing): push the briefing to the owner daily at BRIEFING_TIME"
```

---

## After the plan

Manual smoke test against a scratch database, never `wren.db`:

```bash
export WREN_DB=$(mktemp -d)/scratch.db
export CALENDAR_URLS="<your ICS url>" WEATHER_LAT=41.88 WEATHER_LON=-87.63
venv/bin/python3 - <<'EOF'
from datetime import date
from wren.skills import calendar_skill, weather_skill, briefing_skill
from wren.skills import reminders_store, shopping_store
from wren import settings
for m in (settings, reminders_store, shopping_store): m.init_db()
print(calendar_skill.format_range(calendar_skill.events_between(date.today(), 7), date.today(), 7, "this week"))
print(weather_skill.summary(), weather_skill.summary(1), sep="\n")
print(briefing_skill.compose(1))
EOF
```

Then set `BRIEFING_TIME` in the panel to two minutes from now and watch
`journalctl -u wren.service -f | grep briefing`.

## Deliberately not in this plan

- Calendar / weather / briefing **cards** in the web chat.
- Creating or editing calendar events.
- Briefings for anyone but the owner.
- A persisted "last sent" date (see `ponytail:` note on `_last_sent`).
- Memory-aware briefings — once memory slice 1 has shipped and been tuned.
