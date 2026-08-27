import sqlite3

from .. import config
from .. import contacts
from .. import flourish
from ..channel import Ctx

INTENTS = ["add_contact", "remove_contact", "list_contacts"]
DESTRUCTIVE = ["remove_contact"]
CONFIRM = {"remove_contact": "remove the contact"}
PLUGIN_NAME = "Contacts"

PROMPT_GUIDELINES = """- add_contact: owner wants to add a new whitelisted contact (e.g. "add 123456789012345678 as hubby"); content is the raw numeric user ID, person is the alias
- remove_contact: owner wants to remove a whitelisted contact (e.g. "remove hubby"); person is the alias to remove
- list_contacts: owner wants to see who's currently whitelisted (e.g. "who's whitelisted", "list contacts")"""


async def handle(intent: str, ctx: Ctx) -> None:
    if ctx.user_id != config.WHITELIST["owner"]:
        await ctx.channel.send("Only the owner can manage contacts.")
        return

    if intent == "add_contact":
        alias = (ctx.person or "").strip().lower()
        discord_id_raw = (ctx.content or "").strip()
        if not alias:
            await ctx.channel.send("Who should I add?")
            return
        if not discord_id_raw.isdigit():
            await ctx.channel.send("That doesn't look like a user ID.")
            return
        if alias in config.whitelist():
            await ctx.channel.send("That name's already taken.")
            return
        try:
            contacts.add(alias, int(discord_id_raw))
        except sqlite3.IntegrityError:
            await ctx.channel.send("That user ID is already registered under another name.")
            return
        await ctx.channel.send(flourish.flourish(f"Added {alias}."))

    elif intent == "remove_contact":
        alias = (ctx.person or "").strip().lower()
        if contacts.remove(alias):
            await ctx.channel.send(flourish.flourish(f"Removed {alias}."))
        else:
            await ctx.channel.send(f"No contact named {alias}.")

    elif intent == "list_contacts":
        names = [name for name in config.whitelist() if name != "owner"]
        if not names:
            await ctx.channel.send("No contacts yet.")
        else:
            await ctx.channel.send(", ".join(names))
