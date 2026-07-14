from . import config
from . import contacts

INTENTS = ["add_contact", "remove_contact", "list_contacts"]

PROMPT_GUIDELINES = """- add_contact: owner wants to add a new whitelisted contact (e.g. "add 123456789012345678 as hubby"); content is the raw numeric Discord ID, person is the alias
- remove_contact: owner wants to remove a whitelisted contact (e.g. "remove hubby"); person is the alias to remove
- list_contacts: owner wants to see who's currently whitelisted (e.g. "who's whitelisted", "list contacts")"""


async def handle(intent, message, client, user_id, content, tags, person, when):
    if user_id != config.WHITELIST["owner"]:
        await message.channel.send("Only the owner can manage contacts.")
        return

    if intent == "add_contact":
        alias = (person or "").strip().lower()
        discord_id_raw = (content or "").strip()
        if not discord_id_raw.isdigit():
            await message.channel.send("That doesn't look like a Discord ID.")
            return
        if alias in config.whitelist():
            await message.channel.send("That name's already taken.")
            return
        contacts.add(alias, int(discord_id_raw))
        await message.channel.send(f"Added {alias}.")

    elif intent == "remove_contact":
        alias = (person or "").strip().lower()
        if contacts.remove(alias):
            await message.channel.send(f"Removed {alias}.")
        else:
            await message.channel.send(f"No contact named {alias}.")

    elif intent == "list_contacts":
        names = [name for name in config.whitelist() if name != "owner"]
        if not names:
            await message.channel.send("No contacts yet.")
        else:
            await message.channel.send(", ".join(names))
