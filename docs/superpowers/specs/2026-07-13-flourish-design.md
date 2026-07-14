# Cute Emote Flourish — Design

## Goal

Give Wren's success confirmations a small personality "pop" via a random
bird/cute-themed emoji suffix (e.g. "Saved. 🐦"), fitting the bot's
bird-themed name. Scoped narrowly to avoid two real problems identified
during design: decorating error/edge-case messages feels odd, and
decorating messages that echo back stored or generated content (note
listings, pin listings, `pin_note`'s own text, chat replies, web
summaries) would either look arbitrary or — in `pin_note`'s case — bake
the emoji permanently into a saved pinned message.

## `wren/flourish.py`: the helper

```python
import random

EMOTES = ["🐦", "🐦‍⬛", "🪶", "🐤", "🐣", "✨"]

def flourish(text: str) -> str:
    return f"{text} {random.choice(EMOTES)}"
```

`EMOTES` is a module-level list (not a private `_EMOTES`) so tests can
assert membership without reaching into internals.

## Scope: exactly 12 call sites, success confirmations only

Everything else — errors, guards ("What should I pin?"), empty-states
("No notes found."), listings that echo stored content (`recall_notes`,
`recall_ideas`, `list_pins`, `recall_shopping`, `recall_reminders`,
`list_contacts`), LLM-generated content (`chat`, `web_search`/`read_page`
summaries, `expand_idea`), and `pin_note`'s own content send — stays
exactly as-is, unwrapped.

| File | Intent | Message wrapped |
|---|---|---|
| `wren/bot.py` | `send_to_person` | `f"Sent to {target_name}."` |
| `wren/shopping_plugin.py` | `add_shopping_item` | `f"Added {content}."` |
| `wren/shopping_plugin.py` | `remove_shopping_item` | `f"Got it, removed {content}."` |
| `wren/shopping_plugin.py` | `send_shopping_list` | `f"Sent to {target_name}."` |
| `wren/notes_plugin.py` | `save_note` | `"Saved."` |
| `wren/notes_plugin.py` | `save_idea` | `"Saved that idea."` |
| `wren/notes_plugin.py` | `discard_idea` | `f"Discarded: {matches[0]['content']}."` |
| `wren/pins_plugin.py` | `unpin_note` | `f"Unpinned: {matches[0].content}"` |
| `wren/reminder_plugin.py` | `set_reminder` | `f"Reminder set for {_format_local(fire_at)}."` |
| `wren/reminder_plugin.py` | `cancel_reminder` | `f"Cancelled: {matches[0]['content']}."` |
| `wren/contacts_plugin.py` | `add_contact` | `f"Added {alias}."` |
| `wren/contacts_plugin.py` | `remove_contact` | `f"Removed {alias}."` |

Each becomes `await message.channel.send(flourish(f"..."))` — a one-line
wrap around the existing string, no other logic touched.

## Testing

`tests/test_flourish.py`: `flourish("Saved.")` returns a string starting
with `"Saved. "` whose trailing token (after the last space) is a member
of `flourish.EMOTES`; called many times, returns more than one distinct
emote across the pool (basic randomness sanity check, not a statistical
proof).

The 12 affected existing tests across `tests/test_shopping_plugin.py`,
`tests/test_notes_plugin.py`, `tests/test_pins_plugin.py`,
`tests/test_reminder_plugin.py`, `tests/test_contacts_plugin.py` switch
from `assert_awaited_once_with("exact text")` to asserting the sent
string starts with the original text plus a space, and the trailing token
is in `flourish.EMOTES` — avoids monkeypatching `random.choice` in a
dozen places. `bot.py`'s one affected call site
(`send_to_person`/`"Sent to {name}."`) has no test coverage, consistent
with the rest of that file.

## Out of scope

- Errors, guards, empty-states, and any message echoing stored/generated
  content — never flourished.
- `pin_note`'s content — never flourished (would corrupt the saved pin).
- Configurable/toggle-able flourish (always on for the 12 listed sites,
  no env var or per-user preference).
- Weighted/non-uniform emote selection (`random.choice` is uniform).
