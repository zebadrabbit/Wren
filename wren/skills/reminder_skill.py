import asyncio
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from .. import config
from . import reminders_store as reminders
from .. import router
from .. import flourish
from ..channel import Ctx

INTENTS = ["set_reminder", "recall_reminders", "cancel_reminder"]
PLUGIN_NAME = "Reminders"

PROMPT_GUIDELINES = """- set_reminder: user wants to be reminded of something at a specific time; content is what to remind them of, and you must compute "when" as an absolute ISO 8601 datetime in the LOCAL timezone you were already told "today" is in (e.g. 2026-07-12T21:00:00, no UTC conversion) based on the current date/time and the relative or absolute time they gave (e.g. "in 20 minutes", "at 6pm", "tomorrow morning")
- recall_reminders: user wants to see their upcoming reminders
- cancel_reminder: user wants to cancel a previously set reminder; content is a short phrase identifying which one, not the full reminder text"""

_GRACE = timedelta(seconds=30)

def _parse_when(when):
    if not when:
        return None
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
        parsed_utc = _parse_when(ctx.when)
        if not ctx.content.strip() or parsed_utc is None:
            await ctx.channel.send("I couldn't figure out when — try again with a specific time.")
        else:
            fire_at = parsed_utc.isoformat(timespec="seconds")
            reminders.save(ctx.user_id, ctx.content, fire_at)
            await ctx.channel.send(flourish.flourish(f"Reminder set for {_format_local(fire_at)}."))

    elif intent == "recall_reminders":
        items = reminders.pending(ctx.user_id)
        rows = [{"content": r["content"], "fire_at": r["fire_at"],
                 # formatted here, not in the page: the skill already owns the
                 # timezone and the card should never have to
                 "local": _format_local(r["fire_at"])}
                for r in items]
        text = "\n".join(f"[{_format_local(r['fire_at'])}] {r['content']}"
                         for r in items) if items else "No reminders set."
        await ctx.channel.send_card(
            "reminders", {"reminders": rows}, text,
            intent="recall_reminders", params={"content": ""},
        )

    elif intent == "cancel_reminder":
        if not ctx.content.strip():
            await ctx.channel.send("Which reminder do you want to cancel?")
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
                ok = await router.notify(int(r["owner_id"]), f"⏰ Reminder: {r['content']}")
                if not ok:
                    logging.warning(f"Could not deliver reminder {r['id']} to {r['owner_id']}")
                reminders.mark_fired(r["id"])
        except Exception as e:
            logging.warning(f"reminder poll failed: {e}")
        await asyncio.sleep(config.REMINDER_POLL_SECONDS)
