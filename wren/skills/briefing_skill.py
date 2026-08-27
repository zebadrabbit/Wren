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
