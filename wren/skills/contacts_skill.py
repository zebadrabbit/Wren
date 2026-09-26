import sqlite3

from .. import config
from .. import contacts
from .. import flourish
from .. import router
from ..channel import Ctx

INTENTS = ["add_contact", "remove_contact", "list_contacts"]
DESTRUCTIVE = ["remove_contact"]
CONFIRM = {"remove_contact": 'remove the contact "{content}"'}
PLUGIN_NAME = "Contacts"

PROMPT_GUIDELINES = """- add_contact: owner wants to add a new whitelisted contact, or give an existing one their id on another surface (e.g. "add 123456789012345678 as hubby", "add 987654321 as hubby on telegram"); content is the raw numeric user ID, person is the alias
- remove_contact: owner wants to remove a whitelisted contact (e.g. "remove hubby"); person is the alias to remove
- list_contacts: owner wants to see who's currently whitelisted (e.g. "who's whitelisted", "list contacts")"""


def _default_surface() -> str:
    """No surface named: the first configured one that has ids of its own, so
    "add 555 as hubby" means Telegram on a Telegram-first install."""
    return next((s for s in config.COMMUNICATION_PLUGINS if s in contacts.SURFACES),
                contacts.SURFACES[0])


async def handle(intent: str, ctx: Ctx) -> None:
    if ctx.user_id != config.WHITELIST["owner"]:
        await ctx.channel.send("Only the owner can manage contacts.")
        return

    if intent == "add_contact":
        alias = (ctx.person or "").strip().lower()
        id_raw = (ctx.content or "").strip()
        if not alias:
            await ctx.channel.send("Who should I add?")
            return
        if not id_raw.isdigit():
            await ctx.channel.send("That doesn't look like a user ID.")
            return
        named = router.named_surface(ctx.text)
        surface = named if named in contacts.SURFACES else _default_surface()
        existing = alias in contacts.all()
        # An existing contact gets a second surface; the owner's name, or a
        # surface the contact already has an id on, is taken.
        if (alias in config.whitelist() and not existing) or (
                existing and surface in contacts.surfaces_of(contacts.all()[alias])):
            await ctx.channel.send("That name's already taken.")
            return
        try:
            if existing:
                contacts.set_id(alias, surface, int(id_raw))
            else:
                contacts.add(alias, int(id_raw), surface=surface)
        except sqlite3.IntegrityError:
            await ctx.channel.send("That user ID is already registered under another name.")
            return
        await ctx.channel.send(flourish.flourish(
            f"Added {alias} on {surface}." if (named or existing) else f"Added {alias}."))

    elif intent == "remove_contact":
        alias = (ctx.person or "").strip().lower()
        if contacts.remove(alias):
            await ctx.channel.send(flourish.flourish(f"Removed {alias}."))
        else:
            await ctx.channel.send(f"No contact named {alias}.")

    elif intent == "list_contacts":
        people = contacts.all()
        if not people:
            await ctx.channel.send("No contacts yet.")
        else:
            await ctx.channel.send(", ".join(
                f"{name} ({', '.join(contacts.surfaces_of(wid))})" for name, wid in people.items()))
