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
