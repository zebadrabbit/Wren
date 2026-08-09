# Ideas Bucket — Design

## Goal

Wren should let a user quickly capture a free-form idea via DM ("remember
this idea: ..."), recall all their saved ideas, discard one they no longer
want, or ask Wren to expand/elaborate on one — without a new storage system,
reusing the existing per-owner `notes.py`.

This is sub-project 2 of the larger personal-assistant expansion (shopping
list already shipped; a plugin system and external integrations are queued
after this). Project context: `notes.py` (per-owner SQLite notes with
comma-tag filtering, from prior work) and `brain.py` (LLM intent detection
routing `bot.py`'s DM handler, now with 8 existing intent values after the
shopping-list work).

## Storage: extend `notes.py`, no new table

Ideas are ordinary `notes` rows tagged `["idea"]`. The tag is applied by
`bot.py` at save time (not left to the LLM's own tag guess), so recall/
discard/expand can filter reliably by exact tag rather than trusting the
model to reproduce it every time.

Two new functions on the existing module:

```python
def delete(note_id: int) -> bool:
    """Hard delete a note by id. Returns True if a row was removed, False if not found."""

def find(owner_id: int, substring: str, tags: list[str] | None = None) -> list[dict]:
    """Same owner+tag filtering as search(), further filtered to rows whose
    content contains `substring` (case-insensitive). Used to resolve a
    free-text reference ("the idea about the treehouse") to a specific row."""
```

`find()` reuses `search()` internally (call `search(owner_id, tags)`, then
filter the result by `substring.lower() in row["content"].lower()`) rather
than a new SQL query — the existing owner+tag filtering already does the
scoping `find()` needs.

Discard is a **hard delete**, not a soft-delete/status flag: unlike shopping
items, there's no frequency-tracking reason to keep a discarded idea around,
and adding a status column to `notes.py` would affect `save_note`/
`recall_notes` too for no benefit to those flows.

## Intent detection (brain.py)

`_SYSTEM`'s intent schema grows from 8 to 12 values, adding:

- `save_idea` — content: the idea text. Trigger phrases like "remember this
  idea...", "idea:...".
- `recall_ideas` — no content needed.
- `discard_idea` — content: an identifying phrase/substring to match against
  the user's saved ideas.
- `expand_idea` — content: an identifying phrase/substring, same matching
  as `discard_idea`.

New guideline bullets follow the existing style, distinguishing `save_idea`
from `save_note` (an idea is explicitly framed as an idea to revisit/expand
later, not a reminder or grocery item) and explaining that `discard_idea`/
`expand_idea`'s content is a matching phrase, not the full idea text.

`brain.py` also gets one new function, following the existing `chat()`/
`recall()` pattern (one-off call through `_complete()`'s fallback chain, no
signature or logic changes to existing functions):

```python
def expand(idea_text: str) -> str:
    """One-off LLM elaboration on a saved idea. Nothing is written back to
    storage — this is a reply, not an update."""
```

## Dispatch (bot.py)

Four new `elif` branches, parallel to the existing ones:

- **`save_idea`**: `notes.save(user_id, content, ["idea"])` → "Saved that idea."
- **`recall_ideas`**: `notes.search(user_id, tags=["idea"])` → lists them raw
  (content only, most-recent first — same ordering `search()` already
  returns). This is "show me my ideas," not a Q&A, so it does **not** call
  `brain.recall()` the way `recall_notes` does. Empty → "No ideas saved."
- **`discard_idea`** and **`expand_idea`** both start by resolving the
  target: `notes.find(user_id, content, tags=["idea"])`.
  - 0 matches → "No idea found matching that."
  - 2+ matches → "Found more than one match, be more specific." followed by
    each match's content (so the user can rephrase with a more specific
    substring).
  - exactly 1 match → `discard_idea` calls `notes.delete(id)` → "Discarded:
    {content}."; `expand_idea` calls `brain.expand(matched content)` →
    replies with the LLM's elaboration text directly.

No changes to `bot.py`'s outer `try/except` — already catches anything
unexpected from any intent branch.

## Testing

- `tests/test_notes.py`: add cases for `delete()` (deletes an existing row,
  returns `True`; returns `False` for a nonexistent id) and `find()`
  (substring match within owner+tag scope; case-insensitive; a substring
  that matches zero/one/multiple rows).
- `tests/test_brain.py`: add cases for the 4 new intents' JSON shapes
  (mirroring the shopping-intent tests from the prior feature) plus a case
  for `expand()` returning a string from a mocked provider.
- No `tests/test_bot.py`: same pre-existing condition as the shopping-list
  work — `bot.py` has no test coverage in this repo.

## Out of scope

- Editing/updating an existing idea's content in place — only save, recall,
  discard, expand (one-off, not persisted) are supported.
- Fuzzy/semantic matching for `discard_idea`/`expand_idea` targeting —
  substring match only, consistent with the exact-match philosophy used for
  shopping items.
- Any change to `save_note`/`recall_notes`/existing tag behavior — ideas are
  additive, not a restructuring of how notes work today.
- The plugin system and external integrations named in the original
  request — separate future sub-projects.
