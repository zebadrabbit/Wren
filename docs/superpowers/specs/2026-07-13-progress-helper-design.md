# Progress Helper for Long-Running Tasks — Design

## Goal

A small, reusable helper so a future long-running operation (the
motivating case: a slower "thinking"/reasoning LLM backend, once Wren has
one configured) can show progress by editing one Discord message in
place — no repeated notification pings — then send a fresh message when
done, which triggers Discord's normal notification. Uses the bot's
`MANAGE_MESSAGES`-era OAuth grant in spirit, though editing a message the
bot itself sent does not actually require `MANAGE_MESSAGES` in Discord's
permission model (that permission gates managing *other users'* messages
and pinning — not relevant here).

Nothing in Wren's codebase today is genuinely multi-step long-running
(`web_search`/`read_page` are each one request chain that already replies
once), so this ships as standalone, tested infrastructure with no call
site — ready for whenever a reasoning-model provider is added and a
`brain.py` call is worth narrating.

## `wren/progress.py`

New module, no dependencies beyond `discord` (already a project
dependency):

```python
import discord

class Progress:
    def __init__(self, message: discord.Message):
        self._message = message

    @classmethod
    async def start(cls, channel, text: str) -> "Progress":
        msg = await channel.send(text)
        return cls(msg)

    async def update(self, text: str) -> None:
        await self._message.edit(content=text)

    async def fail(self, text: str) -> None:
        await self._message.edit(content=text)
```

`start()` is a classmethod (not `__init__` alone) because sending the
initial message is itself an async Discord call — the object can't exist
meaningfully before that message exists. `update()`/`fail()` have
identical bodies today but stay as two named methods: call sites read as
"this is a progress step" vs. "this is the failure path," and it leaves
room for `fail` to diverge later (e.g. different formatting) without a
call-site rename.

**Usage shape (not implemented in this spec, illustrative only):**
```python
progress = await Progress.start(message.channel, "Thinking…")
try:
    result = await slow_operation()
except Exception:
    await progress.fail("Something went wrong.")
    return
await progress.update("Almost done…")
final = await another_step()
await message.channel.send(final)  # new message — triggers the normal notification
```

## Testing

`tests/test_progress.py`: `Progress.start()` sends the initial text via
the channel and returns a `Progress` wrapping the returned message (not a
new object with its own state); `update()` calls `.edit(content=...)` on
that same message object, never a new `send`; `fail()` does the same.
Mocks: a `MagicMock` channel whose `send` is an `AsyncMock` returning a
`MagicMock` message whose `edit` is also an `AsyncMock` — matching the
existing `_message()`-style fixture pattern used across
`tests/test_*_plugin.py` files in this repo.

## Out of scope

- Wiring into `read_page`, `web_search`, `chat`, or any other existing
  intent — no current operation is slow enough in practice to need this
  (web scraping/search are single-digit-seconds LLM calls today).
- "Thinking mode" detection, a config flag, or any logic that decides
  *when* to use `Progress` — that decision belongs to whichever future
  feature actually consumes this module.
- Multi-step progress bars, percentages, or ETAs — `update()` takes plain
  text, matching every other user-facing string in this codebase.
