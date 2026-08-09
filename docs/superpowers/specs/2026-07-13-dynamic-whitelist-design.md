# Dynamic Contact Whitelist — Design

## Goal

Replace the static `HUSBAND_ID` env var with a DM command so the owner can
add, remove, and list whitelisted contacts at runtime, without editing
`.env` or restarting. Contacts persist across restarts.

## `wren/contacts.py`: storage

New module, same shape as `notes.py`/`shopping.py` (own SQLite table,
module-level `DB_PATH` for test override):

```sql
CREATE TABLE IF NOT EXISTS contacts (
    alias      TEXT PRIMARY KEY,
    discord_id TEXT NOT NULL UNIQUE,
    added_at   TEXT NOT NULL
)
```

```python
def init_db() -> None: ...
def add(alias: str, discord_id: int) -> None: ...   # upsert-or-raise on alias collision
def remove(alias: str) -> bool: ...                  # True if a row was deleted
def all() -> dict[str, int]: ...                     # {alias: discord_id}
```

`alias` is lowercased before storage/lookup, matching the existing
`WHITELIST` convention (`"husband"`, `"owner"` are always lowercase keys).

## `wren/config.py`: WHITELIST becomes a live merge, not a static dict

Drop `HUSBAND_ID`, `_husband_raw`, and `_build_whitelist`'s husband branch.
`WHITELIST` shrinks to the owner-only static base:

```python
WHITELIST: dict[str, int] = {"owner": int(_owner_raw)}
```

(`_build_whitelist` keeps validating `OWNER_ID` but loses its second
argument — `test_config.py`'s husband-related cases are removed
accordingly.)

Two new functions replace what call sites used to read as static attributes:

```python
def whitelist() -> dict[str, int]:
    return {**WHITELIST, **contacts.all()}

def id_to_name() -> dict[int, str]:
    return {v: k for k, v in whitelist().items()}
```

Recomputed on every call (a SQLite read, not cached) so there's no
stale-after-add/remove window to reason about. `config.py` imports
`contacts` at module level; `contacts.py` does not import `config`, so no
cycle.

**Call sites switching from attribute to function call** (7 total):
- `bot.py`: whitelist gate (`user_id not in config.ID_TO_NAME` →
  `config.id_to_name()`), `send_to_person` branch (2 uses).
- `shopping_plugin.py`: `send_shopping_list` branch (2 uses).
- `discord_utils.py`: `notify()`.
- `brain.py`: `_contacts()`.

## `wren/contacts_plugin.py`: the command surface

```python
INTENTS = ["add_contact", "remove_contact", "list_contacts"]
```

Guidelines teach the brain to extract `content` = the raw numeric Discord
ID and `person` = the alias, from phrasing like "add 123456789012345678 as
hubby" or "remove hubby" or "who's whitelisted".

**Owner-only enforcement lives in the handler**, not the whitelist gate in
`bot.py` (that gate answers "can this person talk to Wren at all," not
"can this person manage contacts"):

```python
async def handle(intent, message, client, user_id, content, tags, person, when):
    if user_id != config.WHITELIST["owner"]:
        await message.channel.send("Only the owner can manage contacts.")
        return
    ...
```

- `add_contact`: validate `content` is numeric digits, `person` is
  non-empty; reject if alias already in `config.whitelist()` (covers both
  the static `"owner"` key and existing dynamic contacts) with "That name's
  already taken." Reject if the ID isn't parseable with "That doesn't look
  like a Discord ID."
- `remove_contact`: `person` names the alias; `contacts.remove()` returns
  `False` for "no such contact" (owner can't be removed since it's not in
  the `contacts` table at all — attempting it naturally falls into "no such
  contact").
- `list_contacts`: reply with each alias from `config.whitelist()` except
  `"owner"` (owner already knows they're the owner), one line each, or "No
  contacts yet" if `contacts.all()` is empty.

## Wiring

- `plugins.py`: add `contacts_plugin` to `PLUGINS`.
- `bot.py`'s `on_ready()`: add `contacts.init_db()` alongside
  `notes.init_db()` / `shopping.init_db()` / `reminders.init_db()`.
- `HELP_TEXT`: drop the hardcoded "husband" examples
  ("tell husband dinner's at 7", "send shopping to husband") in favor of
  generic phrasing, and add an owner-only "Contacts" section documenting
  add/remove/list.

## Cleanup

- `.env.example`: remove `HUSBAND_ID`.
- `setup.sh`: already doesn't prompt for `HUSBAND_ID` (made optional
  2026-07-12); no change needed there beyond confirming it stays absent.
- `test_config.py`: remove `test_build_whitelist_with_husband` and
  `test_build_whitelist_invalid_husband_raises`; update
  `test_build_whitelist_owner_only`-style calls to `_build_whitelist`'s new
  single-argument signature.
- Any other test file that sets `os.environ.setdefault("HUSBAND_ID", "2")`
  (several do, per existing convention) can leave it — an unused env var
  read by nothing is harmless, not worth a sweep.

## Testing

- `tests/test_contacts.py`, mirroring `test_notes.py`'s `tmp_db` fixture
  pattern (`monkeypatch.setattr(contacts, "DB_PATH", ...)`, never touches
  real `wren.db`): add/remove/all, alias case-insensitivity, duplicate
  alias rejection, duplicate discord_id rejection, remove of nonexistent
  alias returns `False`.
- `tests/test_config.py`: `whitelist()`/`id_to_name()` merge the static
  owner with `contacts.all()` (monkeypatched), reflect an add/remove
  immediately (no caching).
- `tests/test_contacts_plugin.py`, mirroring `test_shopping_plugin.py`'s
  handler-test pattern: owner-only rejection for a non-owner `user_id`;
  add/remove/list happy paths; add with a taken alias; add with a
  non-numeric ID; remove of an unknown alias.

## Out of scope

- Per-contact permissions/roles beyond "owner" vs "everyone else" (no
  concept of a contact who can manage other contacts but isn't the owner).
- Rate-limiting or confirmation prompts on add/remove (owner-only gate is
  the only guard, matching the trust model of the rest of the bot).
- Migrating existing `HUSBAND_ID` deployments automatically — owner
  re-adds via the new command after upgrading, per the "remove it"
  decision.
