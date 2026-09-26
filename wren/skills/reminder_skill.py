import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from .. import config
from . import reminders_store as reminders
from .. import router
from .. import flourish
from ..channel import Ctx

INTENTS = ["set_reminder", "recall_reminders", "cancel_reminder"]
DESTRUCTIVE = ["cancel_reminder"]
CONFIRM = {"cancel_reminder": 'cancel the reminder "{content}"'}
PLUGIN_NAME = "Reminders"

PROMPT_GUIDELINES = """- set_reminder: user wants to be reminded of something at a specific time; content is what to remind them of, and you must compute "when" as an absolute ISO 8601 datetime in the LOCAL timezone you were already told "today" is in (e.g. 2026-07-12T21:00:00, no UTC conversion) based on the current date/time and the relative or absolute time they gave (e.g. "in 20 minutes", "at 6pm", "tomorrow morning"); for a repeating reminder ("every tuesday at 8pm", "every morning") "when" is the FIRST occurrence — the repetition is handled elsewhere
- recall_reminders: user wants to see their upcoming reminders
- cancel_reminder: user wants to cancel a previously set reminder; content is a short phrase identifying which one, not the full reminder text — set content to "all" if they want every reminder cancelled ("clear my reminders", "cancel everything")"""

_GRACE = timedelta(seconds=30)

# Longest spelling first in each pair: "second" before "sec", so "seconds"
# matches whole rather than leaving a stray "onds".
_UNITS = {"second": "seconds", "sec": "seconds", "minute": "minutes", "min": "minutes",
          "hour": "hours", "hr": "hours", "day": "days", "week": "weeks"}
_RELATIVE = re.compile(
    r"\bin\s+(\d+|an?)\s+(second|sec|minute|min|hour|hr|day|week)s?\b", re.I)

def _relative_when(text: str):
    """Compute "in 20 minutes" here instead of trusting the model's arithmetic.

    Small local models are bad at clock maths in a way nothing downstream can
    detect: qwen2.5:7b, told the correct local time, turned "in 3 minutes" into
    a time two hours out, three runs in a row — a reminder that saves fine,
    confirms fine, and then just never fires when you expected it. The phrase
    is trivial to parse, so parse it. The model's ISO answer is still used for
    absolute times ("at 6pm", "tomorrow morning"), where it does no arithmetic.
    """
    m = _RELATIVE.search(text or "")
    if not m:
        return None
    count = int(m.group(1)) if m.group(1).isdigit() else 1
    return datetime.now(timezone.utc) + timedelta(**{_UNITS[m.group(2).lower()]: count})

_WEEKDAYS = "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
_EVERY = re.compile(
    r"\b(?:every\s+(?:(\d+|other|an?)\s+)?"
    r"(minute|min|hour|hr|day|week|morning|afternoon|evening|night|" + _WEEKDAYS + r")s?"
    r"|daily|weekly|hourly)\b", re.I)
_REPEAT_UNITS = {"m": "minutes", "h": "hours", "d": "days"}

def _parse_repeat(text: str) -> str | None:
    """The interval behind "every …" in what the user typed, as "<n><unit>"
    with unit m/h/d ("3d", "7d", "2h", "30m"), or None for a one-shot.

    Parsed here for the reason _relative_when gives: the model's clock maths
    is not trustworthy, the phrase is. Every rule reduces to a fixed interval
    — "every tuesday" is seven days from the first occurrence the model
    already computed. "every weekday" is deliberately not matched (it is not
    a fixed interval) rather than mis-read as daily.
    """
    m = _EVERY.search(text or "")
    if not m:
        return None
    whole = m.group(0).lower()
    if whole in ("daily", "weekly", "hourly"):
        return {"daily": "1d", "weekly": "7d", "hourly": "1h"}[whole]
    count, unit = m.group(1), m.group(2).lower()
    n = 2 if count == "other" else int(count) if count and count.isdigit() else 1
    if n == 0:
        return None
    if unit in ("minute", "min"):
        return f"{n}m"
    if unit in ("hour", "hr"):
        return f"{n}h"
    if unit == "week" or unit in _WEEKDAYS.split("|"):
        return f"{7 * n}d"
    return f"{n}d"

def _format_repeat(repeat: str) -> str:
    n, unit = int(repeat[:-1]), repeat[-1]
    if unit == "d" and n % 7 == 0:
        n, name = n // 7, "week"
    else:
        name = _REPEAT_UNITS[unit][:-1]
    return f"every {name}" if n == 1 else f"every {n} {name}s"

def _next_fire(fire_at_iso: str, repeat: str, now: datetime) -> datetime:
    """The first slot after `now`, stepping from the SCHEDULED time, so a
    reminder that fired late does not drift and a service that was down for
    a week fires once and skips forward rather than once per missed slot.

    Day steps are taken on the local wall clock (aware-datetime arithmetic
    keeps the clock time, so "8am every day" survives the DST change); hour
    and minute steps are absolute, in UTC, where the wall clock is the thing
    that lies.
    """
    n, unit = int(repeat[:-1]), repeat[-1]
    step = timedelta(**{_REPEAT_UNITS[unit]: n})
    tz = ZoneInfo(config.TIMEZONE) if unit == "d" else timezone.utc
    t = datetime.fromisoformat(fire_at_iso).astimezone(tz)
    while t <= now:
        t += step
    return t.astimezone(timezone.utc).replace(microsecond=0)

_VIA = re.compile(r"\b(?:on|via|through)\s+([a-z]+)\b", re.I)

def _parse_via(text: str) -> str | None:
    """The surface the user named for THIS reminder, or None for the default.

    Matched against what this install actually has rather than a list of
    transport names -- a skill has no business knowing what a "discord" is
    (CLAUDE.md rule 1), and the same check is what stops "remind me on
    tuesday" reading as a routing request. An unrecognised word after
    "on/via/through" is not a surface, it is part of the sentence.
    """
    m = _VIA.search(text or "")
    if not m:
        return None
    name = m.group(1).lower()
    known = set(router.registered()) | set(config.COMMUNICATION_PLUGINS)
    return name if name in known else None

def _parse_when(when):
    if not when:
        return None
    # models like to append the timezone abbreviation they were shown
    # ("2026-08-18T22:48:30 CDT"), which fromisoformat rejects outright
    when = re.sub(r"\s+[A-Za-z]{2,5}$", "", when.strip())
    try:
        parsed = datetime.fromisoformat(when)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(config.TIMEZONE))
    parsed_utc = parsed.astimezone(timezone.utc)
    if parsed_utc < datetime.now(timezone.utc) - _GRACE:
        return None
    return parsed_utc

def _format_local(fire_at_utc_iso: str) -> str:
    dt = datetime.fromisoformat(fire_at_utc_iso).astimezone(ZoneInfo(config.TIMEZONE))
    return dt.strftime("%Y-%m-%d %H:%M %Z")

# Deliberately narrow. "all" as the whole phrase, or "all/every … reminders"
# in what the user actually typed -- NOT a bare \ball\b, which would read
# "cancel my reminder about the all-hands" as "cancel everything".
_ALL_PHRASES = {"all", "everything", "all of them", "them all", "all reminders",
                "every reminder", "all my reminders"}
_ALL_TEXT = re.compile(r"\b(all|every)\s+(my\s+|the\s+)?reminders?\b", re.I)

def _means_all(ctx: Ctx) -> bool:
    phrase = (ctx.content or "").strip().lower().rstrip(".")
    return phrase in _ALL_PHRASES or bool(_ALL_TEXT.search(ctx.text or ""))

def _resolve_reminder(user_id: int, phrase: str) -> list[dict]:
    # The model often extracts a non-literal reference ("the last reminder",
    # "the 9am one") rather than words that literally appear in the saved
    # content, so a plain substring search finds nothing. If there's only
    # one pending reminder, there's nothing else the phrase could mean, so
    # fall back to it. With 2+ pending reminders an unmatched phrase still
    # correctly reports no match rather than guessing which one to cancel.
    items = reminders.pending(user_id)
    matches = reminders.find_pending(user_id, phrase)
    if not matches and len(items) == 1:
        matches = items
    return matches

async def handle(intent: str, ctx: Ctx) -> None:
    if intent == "set_reminder":
        via = _parse_via(ctx.text)
        if via is not None:
            # Checked now rather than at fire time: a reminder that silently
            # goes nowhere at 3am is the failure this whole feature is about.
            surface = router.surface(via)
            if surface is None or not getattr(surface, "CAN_NOTIFY", True):
                await ctx.channel.send(f"I can't send reminders on {via}.")
                return
        parsed_utc = _relative_when(ctx.text) or _parse_when(ctx.when)
        repeat = _parse_repeat(ctx.text)
        # the model tends to leave the rule inside content; what fires at 8pm
        # should say "take the bins out", not "take the bins out every tuesday"
        content = _EVERY.sub("", ctx.content).strip() if repeat else ctx.content.strip()
        if not content or parsed_utc is None:
            await ctx.channel.send("I couldn't figure out when — try again with a specific time.")
        else:
            fire_at = parsed_utc.isoformat(timespec="seconds")
            reminders.save(ctx.user_id, content, fire_at, via=via, repeat=repeat)
            await ctx.channel.send(flourish.flourish(
                f"Reminder set for {_format_local(fire_at)}"
                + (f", {_format_repeat(repeat)}" if repeat else "")
                + (f", via {via}." if via else ".")))

    elif intent == "recall_reminders":
        items = reminders.pending(ctx.user_id)
        # formatted here, not in the page: the skill already owns the timezone
        # and the card should never have to. The rule rides in the same string
        # so the card shows it without knowing what a rule is.
        def _local(r):
            return _format_local(r["fire_at"]) + (
                f", {_format_repeat(r['repeat'])}" if r.get("repeat") else "")
        rows = [{"content": r["content"], "fire_at": r["fire_at"], "local": _local(r)}
                for r in items]
        text = "\n".join(f"[{_local(r)}] {r['content']}"
                         for r in items) if items else "No reminders set."
        await ctx.channel.send_card(
            "reminders", {"reminders": rows}, text,
            intent="recall_reminders", params={"content": ""},
        )

    elif intent == "cancel_reminder":
        if _means_all(ctx):
            count = reminders.cancel_all(ctx.user_id)
            await ctx.channel.send(
                flourish.flourish(f"Cancelled {count} reminder{'' if count == 1 else 's'}.")
                if count else "No reminders to cancel.")
        elif not ctx.content.strip():
            # Answer with the options rather than a bare question: "which one?"
            # against a list the user cannot see is a dead end, and it is what
            # "clear reminders" hit on 2026-08-18.
            items = reminders.pending(ctx.user_id)
            listing = "\n".join(f"- {r['content']}" for r in items)
            await ctx.channel.send(
                f"Which one? You have:\n{listing}" if items else "No reminders to cancel.")
        else:
            matches = _resolve_reminder(ctx.user_id, ctx.content)
            if not matches:
                await ctx.channel.send("No reminder found matching that.")
            elif len(matches) > 1:
                listing = "\n".join(f"- {m['content']}" for m in matches)
                await ctx.channel.send(f"Found more than one match, be more specific.\n{listing}")
            else:
                reminders.cancel(matches[0]["id"])
                await ctx.channel.send(flourish.flourish(f"Cancelled: {matches[0]['content']}."))

async def start() -> None:
    while True:
        try:
            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for r in reminders.due(now_iso):
                ok = await router.notify(int(r["owner_id"]), f"⏰ Reminder: {r['content']}",
                                         via=r.get("via"))
                if not ok:
                    logging.warning(f"Could not deliver reminder {r['id']} to {r['owner_id']}")
                if r.get("repeat"):
                    reminders.reschedule(r["id"], _next_fire(
                        r["fire_at"], r["repeat"], datetime.now(timezone.utc)).isoformat(timespec="seconds"))
                else:
                    reminders.mark_fired(r["id"])
        except Exception as e:
            logging.warning(f"reminder poll failed: {e}")
        await asyncio.sleep(config.REMINDER_POLL_SECONDS)
