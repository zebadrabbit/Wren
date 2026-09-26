import logging

import discord

from .. import config
from . import chunking

# Discord's own limit. discord.py performs no client-side length check, so a
# body over this reaches the API as-is and comes back as a 400 ("content: Must
# be 2000 or fewer in length") — which propagates out of the skill and into
# core's blanket except, losing the reply entirely. Chunk rather than trust
# callers to be brief; see chunking.chunks for the splitting algorithm.
# discord_plugin imports this module, so it reads the value from here — one
# literal for the one plugin.
_MAX_MESSAGE_CHARS = 2000

def history_to_messages(messages: list[discord.Message], bot_user_id: int) -> list[dict]:
    result = []
    for m in messages:
        if not m.content.strip():
            continue
        role = "assistant" if m.author.id == bot_user_id else "user"
        result.append({"role": role, "content": m.content})
    return list(reversed(result))

async def notify_id(client: discord.Client, user_id: int, text: str) -> bool:
    try:
        target_user = await client.fetch_user(user_id)
        # Same chunker as DiscordChannel.send: an unprompted notify() (a
        # reminder firing) is exactly as capable of exceeding 2000 chars as a
        # reply is, and was hitting the same 400 before this existed.
        for part in chunking.chunks(text, _MAX_MESSAGE_CHARS):
            await target_user.send(part)
        return True
    except (discord.NotFound, discord.Forbidden) as e:
        logging.error(f"Could not DM user {user_id}: {e}")
        return False

async def notify(client: discord.Client, contact_name: str, text: str) -> bool:
    target_id = config.whitelist().get(contact_name.lower())
    if not target_id:
        return False
    return await notify_id(client, target_id, text)
