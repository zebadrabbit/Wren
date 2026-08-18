from .. import config
from . import shopping_store as shopping
from .. import router
from .. import flourish
from ..channel import Ctx

INTENTS = ["add_shopping_item", "remove_shopping_item", "restore_shopping_item",
           "clear_shopping", "recall_shopping", "send_shopping_list"]
PLUGIN_NAME = "Shopping List"

PROMPT_GUIDELINES = """- add_shopping_item: user wants to add an item to the shared shopping list
- remove_shopping_item: user got/bought/already has a SPECIFIC named item and wants that one item off the shopping list
- restore_shopping_item: user wants an item they just removed put BACK on the list ("undo", "put the milk back", "I still need the eggs after all")
- clear_shopping: user wants the WHOLE shopping list emptied ("clear the shopping list", "empty my shopping list", "wipe the list", "start a fresh list") — the list itself is the target, not an item on it
- recall_shopping: user wants to see the current shopping list
- send_shopping_list: user wants to send the whole shopping list to someone"""

async def handle(intent: str, ctx: Ctx) -> None:
    if intent == "add_shopping_item":
        _, was_new = shopping.add(ctx.content, added_by=config.id_to_name()[ctx.user_id])
        if was_new:
            await ctx.channel.send(flourish.flourish(f"Added {ctx.content}."))
        else:
            await ctx.channel.send("Already on the list.")

    elif intent == "remove_shopping_item":
        removed = shopping.remove(ctx.content)
        if removed:
            await ctx.channel.send(flourish.flourish(f"Got it, removed {ctx.content}."))
        else:
            await ctx.channel.send(f"{ctx.content} wasn't on the list.")

    elif intent == "restore_shopping_item":
        # the card's undo button dispatches this, and so does "put the milk
        # back" typed anywhere -- Discord and Telegram included
        if shopping.restore(ctx.content):
            await ctx.channel.send(flourish.flourish(f"Put {ctx.content} back."))
        else:
            await ctx.channel.send(f"{ctx.content} wasn't there to put back.")

    elif intent == "clear_shopping":
        count = shopping.clear()
        if count:
            await ctx.channel.send(flourish.flourish(f"Cleared the list — {count} item{'s' if count != 1 else ''} off."))
        else:
            await ctx.channel.send("Shopping list is already empty.")

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
        text = "\n".join(lines) if lines else "Shopping list is empty."
        # One send, not two: a card IS the message. The prose is the fallback
        # every surface that cannot draw one receives instead.
        await ctx.channel.send_card(
            "shopping",
            {
                "items": [{"text": i["original_text"], "added_by": i["added_by"]}
                          for i in active],
                "suggestions": suggestions,
            },
            text,
            # how this card re-reads itself later. Without these the stored card
            # has no intent and is dead the moment the page reloads.
            intent="recall_shopping",
            params={"content": ""},
        )

    elif intent == "send_shopping_list":
        target_name = (ctx.person or "").lower()
        if target_name not in config.whitelist():
            await ctx.channel.send("I don't know how to reach them.")
            return
        active = shopping.active_items()
        if not active:
            await ctx.channel.send("Nothing on the list to send.")
            return
        list_text = ", ".join(i["original_text"] for i in active)
        ok = await router.notify_name(
            target_name,
            f"Shopping list from {config.id_to_name().get(ctx.user_id, 'someone')}: {list_text}",
        )
        if ok:
            await ctx.channel.send(flourish.flourish(f"Sent to {target_name}."))
        else:
            await ctx.channel.send(f"Couldn't reach {target_name} — they may be unreachable right now.")
