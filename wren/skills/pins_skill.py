from ..channel import Ctx
from . import pins_store as pins
from .. import flourish

INTENTS = ["pin_note", "unpin_note", "list_pins"]
DESTRUCTIVE = ["unpin_note"]
CONFIRM = {"unpin_note": 'unpin "{content}"'}
PLUGIN_NAME = "Pins"

PROMPT_GUIDELINES = """- pin_note: user wants to pin an important note (e.g. "pin: wifi password is 12345"); content is the text to pin
- unpin_note: user wants to remove a previously pinned message; content is a short phrase identifying which pin
- list_pins: user wants to see what's currently pinned"""


_last_unpinned: dict[int, str] = {}


async def _undo_unpin(ctx: Ctx) -> None:
    content = _last_unpinned.pop(ctx.user_id, None)
    if content is None:
        await ctx.channel.send("Nothing to pin back.")
        return
    pins.save(ctx.user_id, content)
    await ctx.channel.send(flourish.flourish(f"Pinned again: {content}"))


UNDO = {"unpin_note": _undo_unpin}


async def handle(intent: str, ctx: Ctx) -> None:
    if intent == "pin_note":
        if not ctx.content.strip():
            await ctx.channel.send("What should I pin?")
            return
        pins.save(ctx.user_id, ctx.content)
        await ctx.channel.send(flourish.flourish(f"Pinned: {ctx.content}"))

    elif intent == "unpin_note":
        if not ctx.content.strip():
            await ctx.channel.send("Which pin do you want to remove?")
            return
        matches = pins.find(ctx.user_id, ctx.content)
        if not matches:
            await ctx.channel.send("No pin found matching that.")
        elif len(matches) > 1:
            listing = "\n".join(f"- {m['content']}" for m in matches)
            await ctx.channel.send(f"Found more than one match, be more specific.\n{listing}")
        else:
            pins.delete(matches[0]["id"])
            _last_unpinned[ctx.user_id] = matches[0]["content"]
            await ctx.channel.send(flourish.flourish(f"Unpinned: {matches[0]['content']}"))

    elif intent == "list_pins":
        pinned = pins.all(ctx.user_id)
        if not pinned:
            await ctx.channel.send("Nothing pinned.")
        else:
            for p in pinned:
                await ctx.channel.send(f"📌 {p['content']}")
