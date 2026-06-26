import logging
import discord
import config
import brain
import notes

logging.basicConfig(level=logging.INFO)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    notes.init_db()
    logging.info(f"Wren online as {client.user}")

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

    result = brain.detect_intent(user_id, text)
    intent = result.get("intent", "chat")
    content = result.get("content", text)
    tags = result.get("tags", [])
    person = result.get("person")

    if intent == "save_note":
        notes.save(user_id, content, tags)
        await message.channel.send("Saved.")

    elif intent == "recall_notes":
        matches = notes.search(user_id, tags=tags if tags else None)
        if not matches:
            await message.channel.send("No notes found.")
        else:
            summary = brain.recall(matches, content)
            await message.channel.send(summary)

    elif intent == "send_to_person":
        target_name = (person or "").lower()
        target_id = config.WHITELIST.get(target_name)
        if not target_id:
            await message.channel.send("I don't know how to reach them.")
            return
        notes.save(user_id, content, tags)
        target_user = await client.fetch_user(target_id)
        await target_user.send(f"From {config.ID_TO_NAME.get(user_id, 'someone')}: {content}")
        await message.channel.send(f"Sent to {target_name}.")

    else:  # chat
        reply = brain.chat(text)
        await message.channel.send(reply)

client.run(config.DISCORD_TOKEN)
