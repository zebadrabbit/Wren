import asyncio
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .. import config
from .. import router
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
    # Compare instants (timestamp), not the aware datetimes directly: two aware
    # datetimes sharing one tzinfo compare wall-clock fields, not instants, so
    # a target inside a DST spring-forward gap (e.g. 02:30 America/Chicago on
    # 2026-03-08) would satisfy `target <= now` for no minute of the day and
    # the briefing would silently never fire. `target` resolves (fold=0) to a
    # real instant past the gap, so this fires once, slightly late, rather
    # than never.
    t0 = target.timestamp()
    n = now.timestamp()
    return t0 <= n < t0 + _WINDOW.total_seconds()


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
            # Local imports: registry imports this module at module scope, and
            # "off" in the panel must mean off for the unprompted half too --
            # BRIEFING_TIME="" is the other, independent switch (asking still
            # works either way).
            from .. import registry
            from . import briefing_skill
            if not registry.is_enabled(briefing_skill):
                continue
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
