import logging
import discord
from . import config
from . import brain
from . import notes
from . import shopping
from . import reminders
from . import plugins
from . import discord_utils

logging.basicConfig(level=logging.INFO)

HELP_TEXT = """Here's what I can actually do:

**Notes**
- "remind me to call the plumber" — saves a note
- "what do I need to do" — recalls and answers from your notes

**Ideas** (separate from notes — for things to revisit or expand later)
- "remember this idea: build a treehouse"
- "what ideas have I saved"
- "discard the idea about the treehouse"
- "expand on the treehouse idea"

**Reminders**
- "remind me to take out the trash at 6pm" / "in 20 minutes" / "tomorrow morning"
- "what are my reminders" — shows all upcoming reminders with times
- "cancel the trash reminder" — matches by phrase, asks for specifics if needed

**Shopping** (one shared list)
- "add potatoes to shopping"
- "got the potatoes" / "remove potatoes from shopping"
- "what's on the shopping list"
- "send shopping to husband"

**Messaging**
- "tell husband dinner's at 7" — DMs the other whitelisted contact

Anything else just falls through to open-ended chat."""

brain.register_plugins(plugins.all_intents(), plugins.all_guidelines())

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    notes.init_db()
    shopping.init_db()
    reminders.init_db()
    await plugins.start_all(client)
    logging.info(f"Wren online as {client.user}")

async def _react(message: discord.Message, emoji: str) -> None:
    try:
        await message.add_reaction(emoji)
    except Exception as e:
        logging.warning(f"Could not react with {emoji}: {e}")

@client.event
async def on_message(message: discord.Message):
    # ignore own messages and non-DMs
    if message.author == client.user:
        return
    if not isinstance(message.channel, discord.DMChannel):
        return

    user_id = message.author.id

    # whitelist gate
    if user_id not in config.ID_TO_NAME:
        return

    text = message.content.strip()
    if not text:
        return

    await _react(message, "👀")

    try:
        result = brain.detect_intent(user_id, text)
        intent = result.get("intent", "chat")
        content = result.get("content", text)
        tags = result.get("tags", [])
        person = result.get("person")
        when = result.get("when")

        if intent in plugins.INTENT_HANDLERS:
            await plugins.INTENT_HANDLERS[intent].handle(
                intent, message, client, user_id, content, tags, person, when
            )

        elif intent == "help":
            await message.channel.send(HELP_TEXT)

        elif intent == "send_to_person":
            target_name = (person or "").lower()
            if target_name not in config.WHITELIST:
                await message.channel.send("I don't know how to reach them.")
            else:
                notes.save(user_id, content, tags)
                ok = await discord_utils.notify(
                    client, target_name,
                    f"From {config.ID_TO_NAME.get(user_id, 'someone')}: {content}",
                )
                if ok:
                    await message.channel.send(f"Sent to {target_name}.")
                else:
                    await message.channel.send(f"Couldn't reach {target_name} — their DMs may be closed.")

        else:  # chat
            reply = brain.chat(text)
            await message.channel.send(reply)

        await _react(message, "✅")
    except Exception as e:
        logging.error(f"Error handling message from {user_id}: {e}")
        await message.channel.send("Something went wrong, try again.")
        await _react(message, "❌")

client.run(config.DISCORD_TOKEN)
