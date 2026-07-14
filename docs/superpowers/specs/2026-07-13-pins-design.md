# Pin/Unpin Important Notes — Design

## Goal

"pin: wifi password is 12345" pins a message in the DM channel so it's
always easy to find via Discord's own pinned-messages UI, plus "unpin the
wifi one" and "what's pinned" to manage/view pins conversationally. Uses
the bot's `MANAGE_MESSAGES` OAuth grant.

## Verified first: does pinning actually need `MANAGE_MESSAGES` in a DM?

`discord.py`'s `Message.pin()` docstring states: "You must have
`Permissions.manage_messages` to do this in a non-private channel
context." DMs are a private channel context — pinning there works without
`MANAGE_MESSAGES` (Discord still caps at 50 pins per channel, same as
everywhere else). This is confirmed directly from the installed
`discord.py` source, not assumed. This feature genuinely exercises the
`MANAGE_MESSAGES` grant's *spirit* (managing pinned messages) even though
the specific Discord permission bit isn't actually the gate in a DM
context — same caveat pattern as the earlier `Progress` helper, which
turned out not to need the permission at all for editing.

## `wren/pins_plugin.py`: new plugin, no new storage

Discord's own pinned-messages list (`channel.pins()`) is the single source
of truth. Nothing is duplicated into `notes.py` — avoids two
representations of the same fact going out of sync (e.g. if a pin is
removed via Discord's native UI instead of through Wren).

```python
INTENTS = ["pin_note", "unpin_note", "list_pins"]

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
```

`pin_note` stays silent on success — no separate "Pinned." confirmation
message; the pinned message itself, with Discord's own pin indicator, is
the confirmation. Matches the "silent updates" framing from the original
`MANAGE_MESSAGES` idea. `unpin_note`/`list_pins` follow the exact
0-match/1-match/many-match substring pattern already established by
`discard_idea`/`expand_idea` in `notes_plugin.py`, for consistency with
the rest of the codebase.

## Wiring

- `plugins.py`: add `pins_plugin` to `PLUGINS`.
- `bot.py`'s `HELP_TEXT`: new "Pins" section documenting pin/unpin/list.

## Testing

`tests/test_pins_plugin.py`, mocking `message.channel.send`/`.pins()` and
message objects' `.pin()`/`.unpin()`:
- `pin_note`: empty-content guard; successful pin (asserts `.pin()` called
  on the sent message, no extra confirmation message sent); pin failure
  (`discord.HTTPException` from `.pin()`) sends the limit-reached message.
- `unpin_note`: empty-content guard; no match; single match (asserts
  `.unpin()` called on the right message); multiple matches (lists them,
  asks for specificity, `.unpin()` not called).
- `list_pins`: empty pins list; multiple pins (one message per pin,
  matching the `recall_ideas` precedent).

This plugin only touches `message.channel` and message objects — all
mockable — so it's genuinely testable, unlike `bot.py`.

## Out of scope

- Persisting pins to `notes.py` or any other storage (Discord's pin list
  is authoritative).
- A pin-count warning before hitting the 50-pin cap (only surfaced as an
  error message when the cap is actually hit).
- Guild/channel pin behavior — this bot is DM-only, so no guild
  permission-overwrite handling is needed.
