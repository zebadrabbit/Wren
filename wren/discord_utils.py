import logging
import discord
from . import config

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
        await target_user.send(text)
        return True
    except (discord.NotFound, discord.Forbidden) as e:
        logging.error(f"Could not DM user {user_id}: {e}")
        return False

async def notify(client: discord.Client, contact_name: str, text: str) -> bool:
    target_id = config.whitelist().get(contact_name.lower())
    if not target_id:
        return False
    return await notify_id(client, target_id, text)
