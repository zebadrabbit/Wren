import logging
import discord
import config
import brain
import notes
import shopping

logging.basicConfig(level=logging.INFO)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    notes.init_db()
    shopping.init_db()
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

    try:
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
            try:
                target_user = await client.fetch_user(target_id)
                await target_user.send(f"From {config.ID_TO_NAME.get(user_id, 'someone')}: {content}")
                await message.channel.send(f"Sent to {target_name}.")
            except (discord.NotFound, discord.Forbidden) as e:
                logging.error(f"Could not DM {target_name}: {e}")
                await message.channel.send(f"Couldn't reach {target_name} — their DMs may be closed.")
                return

        elif intent == "add_shopping_item":
            _, was_new = shopping.add(content, added_by=config.ID_TO_NAME[user_id])
            if was_new:
                await message.channel.send(f"Added {content}.")
            else:
                await message.channel.send("Already on the list.")

        elif intent == "remove_shopping_item":
            removed = shopping.remove(content)
            if removed:
                await message.channel.send(f"Got it, removed {content}.")
            else:
                await message.channel.send(f"{content} wasn't on the list.")

        elif intent == "recall_shopping":
            active = shopping.active_items()
            common = shopping.common_items()
            active_normalized = {i["item"] for i in active}
            suggestions = [c["item"] for c in common if c["item"] not in active_normalized]
            lines = []
            if active:
                lines.append(", ".join(i["original_text"] for i in active))
            if suggestions:
                lines.append("You often get: " + ", ".join(suggestions) + ".")
            if lines:
                await message.channel.send("\n".join(lines))
            else:
                await message.channel.send("Shopping list is empty.")

        elif intent == "send_shopping_list":
            target_name = (person or "").lower()
            target_id = config.WHITELIST.get(target_name)
            if not target_id:
                await message.channel.send("I don't know how to reach them.")
                return
            active = shopping.active_items()
            if not active:
                await message.channel.send("Nothing on the list to send.")
                return
            list_text = ", ".join(i["original_text"] for i in active)
            try:
                target_user = await client.fetch_user(target_id)
                await target_user.send(f"Shopping list from {config.ID_TO_NAME.get(user_id, 'someone')}: {list_text}")
                await message.channel.send(f"Sent to {target_name}.")
            except (discord.NotFound, discord.Forbidden) as e:
                logging.error(f"Could not DM {target_name}: {e}")
                await message.channel.send(f"Couldn't reach {target_name} — their DMs may be closed.")
                return

        else:  # chat
            reply = brain.chat(text)
            await message.channel.send(reply)
    except Exception as e:
        logging.error(f"Error handling message from {user_id}: {e}")
        await message.channel.send("Something went wrong, try again.")

client.run(config.DISCORD_TOKEN)
