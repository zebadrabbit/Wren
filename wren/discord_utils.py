import logging
import discord
from . import config

async def notify(client: discord.Client, contact_name: str, text: str) -> bool:
    target_id = config.WHITELIST.get(contact_name.lower())
    if not target_id:
        return False
    try:
        target_user = await client.fetch_user(target_id)
        await target_user.send(text)
        return True
    except (discord.NotFound, discord.Forbidden) as e:
        logging.error(f"Could not DM {contact_name}: {e}")
        return False
