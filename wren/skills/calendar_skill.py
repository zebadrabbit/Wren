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

PROMPT_GUIDELINES = """- recall_calendar: user asks what is on their calendar, schedule or agenda, or what a NAMED day (today, tomorrow, Friday) or the week looks like; if they name a specific day set "when" to that date as ISO 8601, otherwise leave it out"""

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


_WEBCAL = re.compile(r"^webcal://", re.I)


def _fetch(url: str) -> str:
    now = time.monotonic()
    hit = _cache.get(url)
    if hit and now - hit[0] < config.CALENDAR_CACHE_SECONDS:
        return hit[1]
    # .hostname, not .netloc: netloc includes a `user:token@` userinfo prefix
    # when the host hands the feed out that way (Nextcloud, Radicale), which
    # would leak straight into a FeedError string otherwise.
    host = urlsplit(url).hostname or "feed"
    # webcal:// is what Apple/Google hand out; httpx has no opinion on the
    # scheme, so translate it to https before the request. The cache key
    # stays the original url so a webcal and https form of the same feed
    # don't double-fetch.
    request_url = _WEBCAL.sub("https://", url)
    try:
        resp = httpx.get(request_url, timeout=FETCH_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
    except Exception as e:
        # Broad on purpose: httpx.InvalidURL, UnsupportedProtocol (some
        # versions), UnicodeEncodeError etc. all escape a bare `except
        # httpx.HTTPError` and their messages may carry the URL -- and an
        # uncaught exception here isn't caught by compose() either, costing
        # the whole briefing over one dead feed.
        # `from None`: the message/traceback of `e` may carry the URL, and
        # the URL is the secret.
        raise FeedError(f"{host}: {type(e).__name__}") from None
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
    if not is_active():
        await ctx.channel.send("The calendar isn't configured — set CALENDAR_URLS.")
        return
    anchor, days, label = _window(ctx)
    try:
        events = await asyncio.to_thread(events_between, anchor, days)
    except FeedError as e:
        logging.warning(f"calendar: {e}")
        await ctx.channel.send("Couldn't reach the calendar feed.")
        return
    await ctx.channel.send(format_range(events, anchor, days, label))
