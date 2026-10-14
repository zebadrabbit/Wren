import asyncio
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .. import config
from ..communication import gmail_state

# subjects in the briefing before "…and N more": a digest, not the inbox
_DIGEST_CAP = 5
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

PROMPT_GUIDELINES = """- briefing: user asks for their briefing, a rundown or overview of TODAY as a whole ("morning briefing", "catch me up", "what's my day look like") — questions about a specific day or the calendar are recall_calendar"""


def _now() -> datetime:
    return datetime.now(ZoneInfo(config.TIMEZONE))


def _greeting(now: datetime) -> str:
    if now.hour < 12:
        return "Good morning."
    if now.hour < 17:
        return "Good afternoon."
    return "Good evening."


def gather(user_id: int) -> dict:
    """Everything the briefing knows, as data. Blocking (feeds, HTTP); call
    under to_thread. None means a section is off or unconfigured; an empty
    list or 0 means configured and empty. A calendar that failed is a string
    so the card can say so in the section's own place.

    Composed, never generated: nothing here touches brain. A briefing that
    invents a meeting is worse than no briefing, and a deterministic one is
    free to send every morning. Each source is wrapped on its own so one dead
    feed costs one section, not the whole message.
    """
    from .. import registry   # local: registry imports this module

    tz = ZoneInfo(config.TIMEZONE)
    now = _now().astimezone(tz)
    today = now.date()
    data: dict = {"greeting": f"{_greeting(now)} {today:%a %b} {today.day}.",
                  "weather": None, "calendar": None, "reminders": None,
                  "email": [], "shopping": None}

    if registry.is_enabled(weather_skill) and weather_skill.is_active():
        try:
            data["weather"] = weather_skill.summary(0)
        except Exception as e:
            logging.warning(f"briefing: weather failed: {type(e).__name__}: {e}")
            data["weather"] = "couldn't reach the weather service."

    if registry.is_enabled(calendar_skill) and calendar_skill.is_active():
        try:
            data["calendar"] = calendar_skill.day_lines(calendar_skill.events_between(today, 1), today)
        except calendar_skill.FeedError as e:
            logging.warning(f"briefing: calendar failed: {e}")
            data["calendar"] = "couldn't reach the feed."

    if registry.is_enabled(reminder_skill):
        due = []
        for r in reminders.pending(user_id):
            local = datetime.fromisoformat(r["fire_at"]).astimezone(tz)
            if local.date() == today:
                due.append({"local": f"{local:%H:%M}", "content": r["content"]})
        data["reminders"] = due

    if config.EMAIL_DIGEST:
        # the watcher's last day of watched mail, by this person's alias
        # (EMAIL_WATCH maps senders to aliases, not ids)
        alias = config.id_to_name().get(user_id)
        mail = gmail_state.recent(alias) if alias else []
        data["email"] = [{"sender": m["sender"], "subject": m["subject"]} for m in mail]

    if registry.is_enabled(shopping_skill):
        data["shopping"] = len(shopping.active_items())
    return data


def render(data: dict) -> str:
    """The day in one message, from gather()'s data. The same lines the
    briefing has always sent; the card is the other rendering of this dict."""
    lines = [data["greeting"]]
    empties: list[str] = []
    if data["weather"] is not None:
        lines.append("Weather: " + data["weather"])
    cal = data["calendar"]
    if isinstance(cal, str):
        lines.append("Calendar: " + cal)
    elif cal:
        lines.append("Calendar:")
        lines.extend(cal)
    elif cal is not None:
        empties.append("nothing on the calendar")
    due = data["reminders"]
    if due:
        lines.append("Reminders today:")
        lines.extend(f"  {r['local']}  {r['content']}" for r in due)
    elif due is not None:
        empties.append("no reminders")
    mail = data["email"]
    if mail:
        lines.append("Email:")
        lines.extend(f"  {m['sender']} — {m['subject']}" for m in mail[:_DIGEST_CAP])
        if len(mail) > _DIGEST_CAP:
            lines.append(f"  …and {len(mail) - _DIGEST_CAP} more.")
    n = data["shopping"]
    if n:
        lines.append(f"Shopping list: {n} item{'' if n == 1 else 's'}.")
    elif n is not None:
        empties.append("list is empty")
    # Only when every data section came up empty: a briefing that lists what
    # it does not have reads as an apology; one that says nothing reads as broken.
    if empties and not any(l.startswith(("Calendar", "Reminders", "Shopping")) for l in lines):
        sentence = ", ".join(empties)
        lines.append(sentence[:1].upper() + sentence[1:] + ".")
    return "\n".join(lines)


def compose(user_id: int) -> str:
    return render(gather(user_id))


async def handle(intent: str, ctx: Ctx) -> None:
    data = await asyncio.to_thread(gather, ctx.user_id)
    # one send: the card is the message, the prose its fallback on Discord
    # and Telegram, and what the timeline stores
    await ctx.channel.send_card("briefing", data, render(data),
                                intent="briefing", params={"content": ""})


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
