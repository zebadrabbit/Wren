import asyncio
import logging
from datetime import datetime, timezone, timedelta
from . import config
from . import reminders
from . import discord_utils

INTENTS = ["set_reminder", "recall_reminders", "cancel_reminder"]

PROMPT_GUIDELINES = """- set_reminder: user wants to be reminded of something at a specific time; content is what to remind them of, and you must compute "when" as an absolute ISO 8601 UTC datetime (e.g. 2026-07-12T21:00:00+00:00) based on the current date/time and the relative or absolute time they gave (e.g. "in 20 minutes", "at 6pm", "tomorrow morning")
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
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed < datetime.now(timezone.utc) - _GRACE:
        return None
    return parsed

async def handle(intent, message, client, user_id, content, tags, person, when):
    if intent == "set_reminder":
        parsed = _parse_when(when)
        if not content.strip() or parsed is None:
            await message.channel.send("I couldn't figure out when — try again with a specific time.")
        else:
            fire_at = parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
            reminders.save(user_id, content, fire_at)
            display = fire_at[:16].replace("T", " ")
            await message.channel.send(f"Reminder set for {display} UTC.")

    elif intent == "recall_reminders":
        items = reminders.pending(user_id)
        if not items:
            await message.channel.send("No reminders set.")
        else:
            for r in items:
                display = r["fire_at"][:16].replace("T", " ")
                await message.channel.send(f"[{display} UTC] {r['content']}")

    elif intent == "cancel_reminder":
        if not content.strip():
            await message.channel.send("Which reminder do you want to cancel?")
        else:
            matches = reminders.find_pending(user_id, content)
            if not matches:
                await message.channel.send("No reminder found matching that.")
            elif len(matches) > 1:
                listing = "\n".join(f"- {m['content']}" for m in matches)
                await message.channel.send(f"Found more than one match, be more specific.\n{listing}")
            else:
                reminders.cancel(matches[0]["id"])
                await message.channel.send(f"Cancelled: {matches[0]['content']}.")

async def start(client) -> None:
    while True:
        try:
            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for r in reminders.due(now_iso):
                ok = await discord_utils.notify_id(client, int(r["owner_id"]), f"⏰ Reminder: {r['content']}")
                if not ok:
                    logging.warning(f"Could not deliver reminder {r['id']} to {r['owner_id']}")
                reminders.mark_fired(r["id"])
        except Exception as e:
            logging.warning(f"reminder poll failed: {e}")
        await asyncio.sleep(config.REMINDER_POLL_SECONDS)
