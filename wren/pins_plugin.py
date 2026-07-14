import logging
import discord

INTENTS = ["pin_note", "unpin_note", "list_pins"]
PLUGIN_NAME = "Pins"

PROMPT_GUIDELINES = """- pin_note: user wants to pin an important note (e.g. "pin: wifi password is 12345"); content is the text to pin
- unpin_note: user wants to remove a previously pinned message; content is a short phrase identifying which pin
- list_pins: user wants to see what's currently pinned"""


async def handle(intent, message, client, user_id, content, tags, person, when):
    if intent == "pin_note":
        if not content.strip():
            await message.channel.send("What should I pin?")
            return
        pinned_message = await message.channel.send(content)
        try:
            await pinned_message.pin()
        except discord.HTTPException as e:
            logging.warning(f"pin failed: {e}")
            await message.channel.send("Couldn't pin that — you may be at Discord's 50-pin limit.")

    elif intent == "unpin_note":
        if not content.strip():
            await message.channel.send("Which pin do you want to remove?")
            return
        pins = await message.channel.pins()
        matches = [p for p in pins if content.lower() in p.content.lower()]
        if not matches:
            await message.channel.send("No pin found matching that.")
        elif len(matches) > 1:
            listing = "\n".join(f"- {m.content}" for m in matches)
            await message.channel.send(f"Found more than one match, be more specific.\n{listing}")
        else:
            await matches[0].unpin()
            await message.channel.send(f"Unpinned: {matches[0].content}")

    elif intent == "list_pins":
        pins = await message.channel.pins()
        if not pins:
            await message.channel.send("Nothing pinned.")
        else:
            for p in pins:
                await message.channel.send(f"📌 {p.content}")
