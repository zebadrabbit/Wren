# Shopping List with Common-Request Tracking — Design

## Goal

Wren should let anyone in the household whitelist quickly add/remove shopping
items via short DMs ("add potatoes to shopping", "got the potatoes"), see the
current list on request, and send the whole list to another whitelisted
contact. Items requested repeatedly over time should surface as "common"
suggestions when the list is recalled.

This is sub-project 1 of a larger idea (personal-assistant expansion: ideas
bucket, a plugin system, and external integrations like an email watcher).
Those are out of scope here and will each get their own design after this one
ships. See project context: this repo already has `notes.py` (per-owner
SQLite notes) and `brain.py` (LLM intent detection routing `bot.py`'s DM
handler) from prior work.

## Storage

New table, new module `shopping.py` (parallel to `notes.py`, not built on top
of it — the two have different scoping and lifecycle requirements that would
otherwise tangle):

```sql
CREATE TABLE shopping_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    item          TEXT NOT NULL,   -- normalized (lowercase, trimmed) — used for matching
    original_text TEXT NOT NULL,   -- as the user typed it — used for display
    added_by      TEXT NOT NULL,   -- contact name (config.WHITELIST key)
    status        TEXT NOT NULL,   -- 'active' | 'removed'
    added_at      TEXT NOT NULL
)
```

- **One shared list**, not per-owner: unlike `notes.py`, there is no
  `owner_id` column. Both whitelisted contacts see and modify the same list —
  this is a household shopping list.
- **Dedupe on add:** adding an item that already has an `active` row with the
  same normalized `item` is a no-op (returns the existing row rather than
  inserting a duplicate).
- **Normalization:** `item = original_text.strip().lower()`. Matching for
  removal and frequency counting is exact-normalized-string match — no fuzzy
  or LLM-based matching. `"Potatoes"` and `"potatoes "` are the same item;
  `"potato"` and `"potatoes"` are not.
- **Removal is a status change, not a delete:** `remove()` sets `status =
  'removed'` on the matching active row rather than deleting it, so removed
  items still count toward historical frequency (see below).

## "Common" items — computed, not stored

No stored flag. `common_items(threshold=3, limit=5)` runs:

```sql
SELECT item, COUNT(*) as n
FROM shopping_items
GROUP BY item
HAVING COUNT(*) >= ?
ORDER BY n DESC
LIMIT ?
```

This counts every historical add for that normalized item, active or
removed — an item bought and re-added many times over months should surface
as common even though most of those rows are now `removed`. Threshold and
limit are function parameters with the defaults above; no config/env knob is
being added for this in this phase (YAGNI — revisit if the fixed default
proves wrong in practice).

`shopping.py`'s public interface:

```python
def init_db() -> None: ...
def add(item_text: str, added_by: str) -> tuple[int, bool]:  # (row id, was_newly_added) — was_newly_added is False on dedupe
def remove(item_text: str) -> bool:  # True if an active match was found and removed, False otherwise
def active_items() -> list[dict]:  # rows with status='active', ordered by added_at
def common_items(threshold: int = 3, limit: int = 5) -> list[dict]:  # [{"item": str, "count": int}, ...]
```

## Intent detection (brain.py)

`_SYSTEM`'s intent schema grows from 4 to 8 values:

- `add_shopping_item` — content: the item text (e.g. "potatoes")
- `remove_shopping_item` — content: the item text
- `recall_shopping` — no content needed
- `send_shopping_list` — person: target contact name (same whitelist lookup as `send_to_person`)
- (existing: `save_note`, `recall_notes`, `send_to_person`, `chat`)

The prompt's guidelines section gets four new bullet points describing each
new intent, following the existing style. No changes to `detect_intent`'s
signature or its JSON-parsing/fallback logic — only the system prompt text
and the set of valid `intent` values change.

## Dispatch (bot.py)

Four new `elif` branches in `on_message`, parallel to the existing ones:

- **`add_shopping_item`**: `_, was_new = shopping.add(content, added_by=config.ID_TO_NAME[user_id])` →
  reply "Added {item}." if `was_new`, else "Already on the list."
- **`remove_shopping_item`**: `shopping.remove(content)` → "Got it, removed
  {item}." if `True`, else "{item} wasn't on the list."
- **`recall_shopping`**: build a message from `shopping.active_items()` and
  `shopping.common_items()`. Empty list and no common items → "Shopping list
  is empty." Active items always shown first; common items not currently
  active are appended as "You often get: x, y, z." (common items that are
  already active are not repeated in that line).
- **`send_shopping_list`**: same target-lookup/whitelist-miss handling as
  today's `send_to_person` (unknown/unreachable contact → same error
  messages), but the DM body is the joined list of `active_items()` instead
  of `content`. Empty list → reply "Nothing on the list to send." and don't
  DM anyone.

No changes to `bot.py`'s outer `try/except` — it already catches anything
unexpected from any intent branch, matching the existing pattern for
`save_note`/`recall_notes`/`send_to_person`.

## Testing

- `tests/test_shopping.py` (new, mirrors `tests/test_notes.py`'s tmp-db
  fixture pattern):
  - add + active_items returns it
  - re-adding the same (normalized) item is a no-op — no duplicate active row
  - remove an active item → status becomes 'removed', no longer in active_items()
  - remove a not-currently-active item → returns False, no row changed
  - common_items: an item added 3 times (default threshold) is included; an
    item added 2 times is not
  - common_items: a removed item still counts toward its historical total
- `tests/test_brain.py`: add cases for the 4 new intents' JSON shapes
  (`detect_intent` returning `add_shopping_item`/`remove_shopping_item`/
  `recall_shopping`/`send_shopping_list` with expected `content`/`person`
  fields), following the existing mocked-provider test pattern from the LLM
  fallback work.
- No `tests/test_bot.py`: `bot.py` has no existing test coverage in this
  repo (it's a thin dispatch layer over `discord.py`); not introducing bot-level
  tests here is consistent with the current codebase, not a gap specific to
  this feature.

## Out of scope

- Multiple items in one message ("add potatoes and milk to shopping") — one
  item per message, matching how `save_note` already handles one note per
  message.
- Per-user private shopping lists — explicitly one shared list per the
  household-list decision above.
- Configurable common-item threshold via env/config — fixed function
  defaults for now.
- Fuzzy/semantic item matching ("spuds" = "potatoes") — exact normalized
  string match only.
- The plugin system, ideas bucket, and external integrations named in the
  original request — separate future sub-projects.
