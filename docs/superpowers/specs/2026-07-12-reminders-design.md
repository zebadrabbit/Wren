# Reminders / Timers — Design

## Goal

"remind me in 20 minutes to check the oven" / "remind me at 6pm to call
mom" — Wren saves a one-time, per-owner reminder and DMs the owner when it
fires. This is the first sub-project of the "expand Wren" direction (the
other two named ideas — the reaction/status UX and "other useful things" —
are separate; reactions already shipped 2026-07-12).

Project context: `notes.py`/`shopping.py` establish the per-owner vs.
shared SQLite storage pattern this follows. `email_plugin.py` establishes
the background-poll-loop pattern (`start(client)`) this reuses. Both
`discord_utils.notify()` and the plugin `INTENTS`/`PROMPT_GUIDELINES`/
`handle()` contract already exist and get extended, not replaced.

## Storage: `reminders.py` (new, per-owner — mirrors `notes.py`)

```sql
CREATE TABLE reminders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   TEXT NOT NULL,
    content    TEXT NOT NULL,
    fire_at    TEXT NOT NULL,   -- ISO 8601 UTC, timespec="seconds"
    status     TEXT NOT NULL,   -- 'pending' | 'fired' | 'cancelled'
    created_at TEXT NOT NULL
)
```

**Timestamp format is deliberately fixed to `timespec="seconds"`
everywhere** (both `fire_at` and the "now" value compared against it).
Mixing a microsecond-precision timestamp against a seconds-precision one in
a lexicographic string comparison (`fire_at <= ?`) is not reliably ordered
— `"21:00:00+00:00"` sorts *before* `"20:59:59.500000+00:00"`
lexicographically (`+` < `.`) even though the latter is earlier. Fixing
both sides to the same precision avoids this entirely.

Public interface:
```python
def init_db() -> None
def save(owner_id: int, content: str, fire_at: str) -> int
def pending(owner_id: int) -> list[dict]        # this owner's pending reminders, soonest first
def find_pending(owner_id: int, substring: str) -> list[dict]  # case-insensitive content match, pending only
def cancel(reminder_id: int) -> bool             # pending -> cancelled
def due(now_iso: str) -> list[dict]              # ALL owners' pending reminders with fire_at <= now_iso
def mark_fired(reminder_id: int) -> None
```

## `brain.py`: a new top-level `"when"` field

The JSON schema gains one field, alongside the existing `content`/`tags`/
`person` (same pattern as `person` being relevant to only some intents):

```json
{
  "intent": "...",
  "content": "...",
  "tags": [...],
  "person": "...",
  "when": "<ISO 8601 UTC datetime for set_reminder, or null>"
}
```

Guideline: "when: only set for set_reminder — an absolute ISO 8601 UTC
datetime computed from the user's relative/absolute time phrase and the
current date/time already in this prompt; null otherwise." No change to
`detect_intent`'s function signature — it already returns the whole parsed
dict; callers just read one more key from it.

## Plugin contract ripple: `handle()` gains a `when` parameter

Every plugin's `handle()` signature grows from `(intent, message, client,
user_id, content, tags, person)` to `(intent, message, client, user_id,
content, tags, person, when)` — matching the existing pattern where every
intent gets every field and most ignore what's irrelevant (e.g.
`recall_notes` already ignores `person`). `notes_plugin.py`/
`shopping_plugin.py` need this one-parameter signature update even though
neither uses `when` yet, and `bot.py`'s dispatch call passes it through.

## Sanity-checking the model-computed time

Before saving, `reminder_plugin.py` validates `when`:
- Must parse via `datetime.fromisoformat()`.
- Must be no more than 30 seconds in the past (grace window for
  request/parse round-trip) — comfortably rejects a model that got the
  date/timezone/relative-offset math wrong.

If either check fails (or `content`/`when` is empty), Wren replies asking
for a clearer time rather than silently scheduling something wrong or
firing immediately. This directly addresses the accuracy risk named when
this design was discussed — small local models can miscalculate time
arithmetic, and a caught bad reminder is much better than a reminder that
never fires (or fires immediately).

## `discord_utils.py`: extract a by-ID notify helper

The reminder poller already has the owner's raw Discord user ID (not a
whitelist *name*), so routing through `notify()`'s name→ID lookup is
backwards. Extract the shared fetch/send/exception-handling core:

```python
async def notify_id(client: discord.Client, user_id: int, text: str) -> bool:
    """Same fetch_user/send/NotFound-Forbidden-handling as notify(), but
    takes a raw Discord user ID directly instead of a whitelist name."""

async def notify(client: discord.Client, contact_name: str, text: str) -> bool:
    """Unchanged public behavior — now implemented as a WHITELIST lookup
    followed by notify_id()."""
```

## `reminder_plugin.py` (new — combines both plugin shapes)

```python
INTENTS = ["set_reminder", "recall_reminders", "cancel_reminder"]
PROMPT_GUIDELINES = "..."  # 3 bullets, as above

async def handle(intent, message, client, user_id, content, tags, person, when):
    # set_reminder: validate `when` (see above), reminders.save(...), confirm
    # recall_reminders: reminders.pending(user_id), one message per reminder
    #   (or "No reminders set."), matching the established note/idea listing UX
    # cancel_reminder: reminders.find_pending(user_id, content), 0/2+/1
    #   ambiguous-match handling identical to discard_idea's pattern

async def start(client) -> None:
    """Poll loop: every config.REMINDER_POLL_SECONDS (default 30), find
    due reminders across all owners, DM each via discord_utils.notify_id(),
    then mark_fired() regardless of delivery success — a permanently
    broken DM should not retry forever; log a warning instead."""
```

`config.py` gains `REMINDER_POLL_SECONDS = int(os.environ.get(..., "30"))`.

## Registration

`plugins.py`'s `PLUGINS` list grows to `[notes_plugin, shopping_plugin,
email_plugin, reminder_plugin]`.

## Testing

- `tests/test_reminders.py`: storage CRUD, `due()` correctly excludes
  future/fired/cancelled reminders, `find_pending()` case-insensitive
  substring match scoped to pending + owner.
- `tests/test_discord_utils.py`: add cases for `notify_id()` (mirrors
  existing `notify()` tests), confirm `notify()`'s behavior is unchanged
  after the refactor.
- `tests/test_reminder_plugin.py`: `set_reminder` valid/invalid/past-time
  cases, `recall_reminders` empty/multi (one message per reminder),
  `cancel_reminder` 0/2+/1 match cases, `start()`'s poll-once logic (mocked
  "now", injected due/not-due reminders, mocked `discord_utils.notify_id`).
- `tests/test_notes_plugin.py` / `tests/test_shopping_plugin.py`: existing
  `handle()` calls get one added trailing argument (`None` for `when`) —
  no behavior change, just the updated signature.
- `tests/test_plugins.py`: `reminder_plugin` registered, `all_intents()`
  includes the 3 new intents.
- Still no `tests/test_bot.py` (pre-existing condition).

## Out of scope

- Recurring reminders (daily/weekly) — one-time only for now.
- Timezone handling beyond UTC — Wren has never asked for the owner's
  local timezone; "6pm" is interpreted as 6pm UTC unless/until that's
  addressed separately.
- Editing an existing reminder's time/content — cancel and re-set instead.
- The Node-RED/Grafana integration and any other "expand Wren" ideas —
  separate, still-undefined future sub-projects.
