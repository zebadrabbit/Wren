from . import config
from . import shopping
from . import discord_utils

INTENTS = ["add_shopping_item", "remove_shopping_item", "recall_shopping", "send_shopping_list"]
PLUGIN_NAME = "Shopping List"

PROMPT_GUIDELINES = """- add_shopping_item: user wants to add an item to the shared shopping list
- remove_shopping_item: user got/bought/already has an item and wants it off the shopping list
- recall_shopping: user wants to see the current shopping list
- send_shopping_list: user wants to send the whole shopping list to someone"""

async def handle(intent, message, client, user_id, content, tags, person, when):
    if intent == "add_shopping_item":
        _, was_new = shopping.add(content, added_by=config.id_to_name()[user_id])
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
        if target_name not in config.whitelist():
            await message.channel.send("I don't know how to reach them.")
            return
        active = shopping.active_items()
        if not active:
            await message.channel.send("Nothing on the list to send.")
            return
        list_text = ", ".join(i["original_text"] for i in active)
        ok = await discord_utils.notify(
            client, target_name,
            f"Shopping list from {config.id_to_name().get(user_id, 'someone')}: {list_text}",
        )
        if ok:
            await message.channel.send(f"Sent to {target_name}.")
        else:
            await message.channel.send(f"Couldn't reach {target_name} — their DMs may be closed.")
