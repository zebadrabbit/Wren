import asyncio
import re

from .. import brain
from .. import config
from . import shopping_store as shopping
from .. import router
from .. import flourish
from ..channel import Ctx

INTENTS = ["add_shopping_item", "remove_shopping_item", "restore_shopping_item",
           "clear_shopping", "recall_shopping", "send_shopping_list"]
DESTRUCTIVE = ["remove_shopping_item", "clear_shopping"]
# "add these to the list" with a photo of a receipt, a fridge or a handwritten
# list: the items are read off the picture (brain.items_in)
ACCEPTS_FILES = ["add_shopping_item"]
# "the list", not "the shopping list": the template cannot see which named
# list the sentence was about, and confirming the wrong name is worse than
# confirming no name.
CONFIRM = {"remove_shopping_item": 'remove "{content}" from the list',
           "clear_shopping": "clear the whole list"}
PLUGIN_NAME = "Shopping List"


def SPACES() -> list[dict]:
    """One sidebar entry per list. A function, not a constant: a packing
    list shows up the day it is created."""
    return [{"key": f"list:{name}",
             "label": "Shopping" if name == "shopping" else name.capitalize(),
             "intent": "recall_shopping",
             "params": {"content": "", "list": name}}
            for name in shopping.list_names()]


PROMPT_GUIDELINES = """- add_shopping_item: user wants to add one or more items to the shared shopping list, or to another named list ("add a tent to the packing list") — same intents, content is the items only, comma-separated ("milk, eggs" for "add milk and eggs")
- remove_shopping_item: user got/bought/already has SPECIFIC named items and wants those off the shopping list, content is the items only, comma-separated
- restore_shopping_item: user wants an item they just removed put BACK on the list ("undo", "put the milk back", "I still need the eggs after all")
- clear_shopping: user wants the WHOLE shopping list emptied ("clear the shopping list", "empty my shopping list", "wipe the list", "start a fresh list") — the list itself is the target, not an item on it
- recall_shopping: user wants to see the current shopping list
- send_shopping_list: user wants to send the whole shopping list to someone"""

# "…to my packing list", "…off the hardware store list", "clear the packing
# list": a determiner, a one- or two-word name, the word "list". Two words at
# most so "take the tent off the packing list" reads "packing", not "tent off
# the packing" (ponytail: a three-word list name falls back to shopping). Parsed here from the user's words, not by the classifier, for the
# reason reminders parse "in 20 minutes" themselves: the six shopping intents
# stay as they are and the model is not asked to grow its JSON. "list of" is
# excluded so "the christmas list of guests" is not a list called christmas.
_LIST = re.compile(
    r"\b(?:my|the|our)\s+([a-z]+(?:\s+[a-z]+)?)\s+list\b(?!\s+of\b)", re.I)
_SHOPPING_NAMES = {"shopping", "grocery", "groceries", "the"}

def _list_name(ctx: Ctx) -> str:
    """Which list this turn is about. The shopping list unless the words name
    another; a card re-reading itself has no words and says so in list_name."""
    if ctx.text:
        m = _LIST.search(ctx.text)
        name = m.group(1).strip().lower() if m else ""
    else:
        name = (ctx.list_name or "").strip().lower()
    return "shopping" if not name or name in _SHOPPING_NAMES else name

# "milk, eggs and bread" -> three items. ponytail: splitting on " and " also
# splits "mac and cheese"; the upgrade is a JSON list from the classifier.
_SPLIT = re.compile(r"\s*,\s*|\s+and\s+", re.I)

def _items(content: str) -> list[str]:
    return [i for i in _SPLIT.split(content or "") if i]

def _names(items: list[str]) -> str:
    return ", ".join(items[:-1]) + (" and " if len(items) > 1 else "") + items[-1]

def _on(name: str) -> str:
    """The suffix that names a list in a reply, empty for the default one."""
    return "" if name == "shopping" else f" the {name} list"

async def _undo_remove(ctx: Ctx) -> None:
    lst = _list_name(ctx)
    back = [i for i in _items(ctx.content) if shopping.restore(i, list_name=lst)]
    if back:
        await ctx.channel.send(flourish.flourish(f"Put {_names(back)} back."))
    else:
        await ctx.channel.send(f"{ctx.content} is already back on the list.")


async def _undo_clear(ctx: Ctx) -> None:
    lst = _list_name(ctx)
    n = shopping.restore_cleared(lst)
    await ctx.channel.send(flourish.flourish(f"Put {n} item{'' if n == 1 else 's'} back.") if n
                           else "Nothing to put back.")


# "undo that" after one of these runs the inverse with the same Ctx (core)
UNDO = {"remove_shopping_item": _undo_remove, "clear_shopping": _undo_clear}


async def handle(intent: str, ctx: Ctx) -> None:
    lst = _list_name(ctx)
    if intent == "add_shopping_item" and ctx.files:
        who = config.id_to_name()[ctx.user_id]
        # to_thread: a vision call takes seconds, and inline it stalls every surface
        seen = await asyncio.to_thread(brain.items_in, ctx.files, ctx.content)
        added = [item for item in seen
                 if shopping.add(item, added_by=who, list_name=lst)[1]]
        if not added:
            await ctx.channel.send("I couldn't read any items off that picture.")
        else:
            await ctx.channel.send(flourish.flourish(
                f"Added {_names(added)}{' to' + _on(lst) if lst != 'shopping' else ''}."))

    elif intent == "add_shopping_item":
        who = config.id_to_name()[ctx.user_id]
        added = [i for i in _items(ctx.content) if shopping.add(i, added_by=who, list_name=lst)[1]]
        if added:
            await ctx.channel.send(flourish.flourish(
                f"Added {_names(added)}{' to' + _on(lst) if lst != 'shopping' else ''}."))
        else:
            await ctx.channel.send("Already on the list.")

    elif intent == "remove_shopping_item":
        removed = [i for i in _items(ctx.content) if shopping.remove(i, list_name=lst)]
        if removed:
            await ctx.channel.send(flourish.flourish(
                f"Got it, removed {_names(removed)}{' from' + _on(lst) if lst != 'shopping' else ''}."))
        else:
            await ctx.channel.send(f"{ctx.content} wasn't on the list.")

    elif intent == "restore_shopping_item":
        # the card's undo button dispatches this, and so does "put the milk
        # back" typed anywhere -- Discord and Telegram included
        if shopping.restore(ctx.content, list_name=lst):
            await ctx.channel.send(flourish.flourish(f"Put {ctx.content} back."))
        else:
            await ctx.channel.send(f"{ctx.content} wasn't there to put back.")

    elif intent == "clear_shopping":
        count = shopping.clear(list_name=lst)
        label = "the list" if lst == "shopping" else f"the {lst} list"
        if count:
            await ctx.channel.send(flourish.flourish(f"Cleared {label} — {count} item{'s' if count != 1 else ''} off."))
        else:
            await ctx.channel.send("Shopping list is already empty." if lst == "shopping"
                                   else f"The {lst} list is already empty.")

    elif intent == "recall_shopping":
        active = shopping.active_items(list_name=lst)
        common = shopping.common_items(list_name=lst)
        active_normalized = {i["item"] for i in active}
        suggestions = [c["item"] for c in common if c["item"] not in active_normalized]
        lines = []
        if active:
            lines.append(", ".join(i["original_text"] for i in active))
        if suggestions:
            lines.append("You often get: " + ", ".join(suggestions) + ".")
        text = "\n".join(lines) if lines else (
            "Shopping list is empty." if lst == "shopping" else f"The {lst} list is empty.")
        # One send, not two: a card IS the message. The prose is the fallback
        # every surface that cannot draw one receives instead.
        await ctx.channel.send_card(
            "shopping",
            {
                "items": [{"text": i["original_text"], "added_by": i["added_by"]}
                          for i in active],
                "suggestions": suggestions,
                "list": lst,
            },
            text,
            # how this card re-reads itself later. Without these the stored card
            # has no intent and is dead the moment the page reloads; without
            # the list name it would come back as the shopping list.
            intent="recall_shopping",
            params={"content": "", "list": lst},
        )

    elif intent == "send_shopping_list":
        target_name = (ctx.person or "").lower()
        if target_name not in config.whitelist():
            await ctx.channel.send("I don't know how to reach them.")
            return
        active = shopping.active_items(list_name=lst)
        if not active:
            await ctx.channel.send("Nothing on the list to send.")
            return
        list_text = ", ".join(i["original_text"] for i in active)
        ok = await router.notify_name(
            target_name,
            f"{'Shopping' if lst == 'shopping' else lst.capitalize()} list from "
            f"{config.id_to_name().get(ctx.user_id, 'someone')}: {list_text}",
        )
        if ok:
            await ctx.channel.send(flourish.flourish(f"Sent to {target_name}."))
        else:
            await ctx.channel.send(f"Couldn't reach {target_name} — they may be unreachable right now.")
