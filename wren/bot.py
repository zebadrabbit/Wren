import logging
import time
import discord
from . import config
from . import brain
from . import notes
from . import shopping
from . import reminders
from . import contacts
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
- "send my notes as a markdown file" — exports all notes and ideas as a downloadable file

**Reminders**
- "remind me to take out the trash at 6pm" / "in 20 minutes" / "tomorrow morning"
- "what are my reminders" — shows all upcoming reminders with times
- "cancel the trash reminder" — matches by phrase, asks for specifics if needed

**Shopping** (one shared list)
- "add potatoes to shopping"
- "got the potatoes" / "remove potatoes from shopping"
- "what's on the shopping list"
- "send shopping to hubby"

**Messaging**
- "tell hubby dinner's at 7" — DMs a whitelisted contact by name

**Contacts** (owner only)
- "add 123456789012345678 as hubby" — whitelists a new contact
- "remove hubby" — un-whitelists a contact
- "who's whitelisted" — lists current contacts

**Status**
- "show status" / "what backend are you using" / "show model" — reports the active LLM backend, endpoint, uptime, and token usage

**Web** (when configured)
- "what's the weather in Chicago tomorrow" / "any news on the port strike" — searches the web and summarizes with links
- "read me the first one" / "read https://…" — fetches a page in full and summarizes

Anything else just falls through to open-ended chat."""

_START_TIME = time.monotonic()

brain.register_plugins(plugins.all_intents(), plugins.all_guidelines())

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    notes.init_db()
    shopping.init_db()
    reminders.init_db()
    contacts.init_db()
    await plugins.start_all(client)
    logging.info(f"Wren online as {client.user}")

async def _react(message: discord.Message, emoji: str) -> None:
    try:
        await message.add_reaction(emoji)
    except Exception as e:
        logging.warning(f"Could not react with {emoji}: {e}")

def _format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if days or hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)

@client.event
async def on_message(message: discord.Message):
    # ignore own messages and non-DMs
    if message.author == client.user:
        return
    if not isinstance(message.channel, discord.DMChannel):
        return

    user_id = message.author.id

    # whitelist gate
    if user_id not in config.id_to_name():
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

        elif intent == "status":
            info = brain.status()
            provider = info["provider"]
            tokens = info["tokens"]
            uptime = _format_uptime(time.monotonic() - _START_TIME)
            lines = [
                f"Backend: {provider['name']} ({provider['model']})",
                f"Endpoint: {provider['base_url']}",
                f"Uptime: {uptime}",
                f"Tokens this session: {tokens['total']:,} ({tokens['prompt']:,} prompt / {tokens['completion']:,} completion)",
            ]
            await message.channel.send("\n".join(lines))

        elif intent == "send_to_person":
            target_name = (person or "").lower()
            if target_name not in config.whitelist():
                await message.channel.send("I don't know how to reach them.")
            else:
                notes.save(user_id, content, tags)
                ok = await discord_utils.notify(
                    client, target_name,
                    f"From {config.id_to_name().get(user_id, 'someone')}: {content}",
                )
                if ok:
                    await message.channel.send(f"Sent to {target_name}.")
                else:
                    await message.channel.send(f"Couldn't reach {target_name} — their DMs may be closed.")

        else:  # chat
            try:
                raw_history = [m async for m in message.channel.history(limit=10, before=message)]
                history = discord_utils.history_to_messages(raw_history, client.user.id)
            except Exception as e:
                logging.warning(f"Could not fetch history: {e}")
                history = None
            reply = brain.chat(text, history)
            await message.channel.send(reply)

        await _react(message, "✅")
    except Exception as e:
        logging.error(f"Error handling message from {user_id}: {e}")
        await message.channel.send("Something went wrong, try again.")
        await _react(message, "❌")

client.run(config.DISCORD_TOKEN)
