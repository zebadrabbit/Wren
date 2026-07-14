# Dynamic Contact Whitelist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The owner can add, remove, and list whitelisted Discord contacts via a DM command, with changes persisted in SQLite and effective immediately — replacing the static `HUSBAND_ID` env var, which is removed.

**Architecture:** A new `wren/contacts.py` module (parallel to `notes.py`/`shopping.py`) owns a `contacts` SQLite table. `wren/config.py`'s `WHITELIST` shrinks to `{"owner": ...}` only; two new functions (`config.whitelist()`, `config.id_to_name()`) merge that static base with `contacts.all()` on every call — no caching, so adds/removes take effect immediately. All call sites that read `config.WHITELIST`/`config.ID_TO_NAME` as attributes switch to calling these functions. A new `wren/contacts_plugin.py` exposes `add_contact`/`remove_contact`/`list_contacts` intents, gated to the owner only inside the handler.

**Tech Stack:** Python 3.11+, `sqlite3` (stdlib), `pytest`. No new dependencies.

## Global Constraints

- Alias keys are always lowercased before storage/lookup, matching the existing `"owner"`/`"husband"` convention.
- `config.whitelist()`/`config.id_to_name()` are computed fresh on every call (a SQLite read) — never cached at module scope, so there's no stale-after-add/remove window.
- Owner-only enforcement for `add_contact`/`remove_contact`/`list_contacts` lives inside `contacts_plugin.handle`, not in `bot.py`'s whitelist gate (that gate answers "can this person talk to Wren," not "can this person manage contacts").
- `HUSBAND_ID` is removed entirely — no legacy/bootstrap path. Owner re-adds any prior second contact via the new command after upgrading.
- No new dependencies.

---

### Task 1: `wren/contacts.py` — contact storage

**Files:**
- Create: `wren/contacts.py`
- Test: `tests/test_contacts.py`

**Interfaces:**
- Produces:
  - `contacts.init_db() -> None`
  - `contacts.add(alias: str, discord_id: int) -> None` — lowercases `alias`; raises `sqlite3.IntegrityError` if `alias` or `discord_id` already exists.
  - `contacts.remove(alias: str) -> bool` — lowercases `alias` before lookup; `True` if a row was deleted, `False` if no such alias.
  - `contacts.all() -> dict[str, int]` — `{alias: discord_id}` for every stored contact.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_contacts.py`:

```python
import os, sqlite3, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from wren import contacts

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(contacts, "DB_PATH", str(tmp_path / "test.db"))
    contacts.init_db()

def test_add_and_all():
    contacts.add("hubby", 222222222222222222)
    assert contacts.all() == {"hubby": 222222222222222222}

def test_add_lowercases_alias():
    contacts.add("Kevin", 333333333333333333)
    assert contacts.all() == {"kevin": 333333333333333333}

def test_add_duplicate_alias_raises():
    contacts.add("hubby", 222222222222222222)
    with pytest.raises(sqlite3.IntegrityError):
        contacts.add("hubby", 444444444444444444)

def test_add_duplicate_discord_id_raises():
    contacts.add("hubby", 222222222222222222)
    with pytest.raises(sqlite3.IntegrityError):
        contacts.add("kevin", 222222222222222222)

def test_remove_existing_returns_true():
    contacts.add("hubby", 222222222222222222)
    assert contacts.remove("hubby") is True
    assert contacts.all() == {}

def test_remove_is_case_insensitive():
    contacts.add("hubby", 222222222222222222)
    assert contacts.remove("HUBBY") is True

def test_remove_nonexistent_returns_false():
    assert contacts.remove("nobody") is False

def test_all_empty_by_default():
    assert contacts.all() == {}

def test_all_returns_multiple():
    contacts.add("hubby", 222222222222222222)
    contacts.add("kevin", 333333333333333333)
    assert contacts.all() == {"hubby": 222222222222222222, "kevin": 333333333333333333}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_contacts.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'wren.contacts'` or similar import error)

- [ ] **Step 3: Write the implementation**

Create `wren/contacts.py`:

```python
import sqlite3
from datetime import datetime, timezone

DB_PATH = "wren.db"

def _conn():
    return sqlite3.connect(DB_PATH)

def init_db() -> None:
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                alias      TEXT PRIMARY KEY,
                discord_id TEXT NOT NULL UNIQUE,
                added_at   TEXT NOT NULL
            )
        """)

def add(alias: str, discord_id: int) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        con.execute(
            "INSERT INTO contacts (alias, discord_id, added_at) VALUES (?,?,?)",
            (alias.strip().lower(), str(discord_id), ts),
        )

def remove(alias: str) -> bool:
    with _conn() as con:
        cur = con.execute("DELETE FROM contacts WHERE alias=?", (alias.strip().lower(),))
        return cur.rowcount > 0

def all() -> dict[str, int]:
    with _conn() as con:
        rows = con.execute("SELECT alias, discord_id FROM contacts").fetchall()
    return {alias: int(discord_id) for alias, discord_id in rows}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_contacts.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add wren/contacts.py tests/test_contacts.py
git commit -m "feat: add contacts storage module"
```

---

### Task 2: `wren/config.py` — dynamic `whitelist()`/`id_to_name()`, drop `HUSBAND_ID`

**Files:**
- Modify: `wren/config.py`
- Modify: `tests/test_config.py`
- Modify: `.env.example`

**Interfaces:**
- Consumes: `contacts.all() -> dict[str, int]` (Task 1)
- Produces:
  - `config.WHITELIST: dict[str, int]` — now just `{"owner": OWNER_ID}`, static.
  - `config.whitelist() -> dict[str, int]` — `{**WHITELIST, **contacts.all()}`, recomputed every call.
  - `config.id_to_name() -> dict[int, str]` — inverse of `whitelist()`, recomputed every call.
  - `config._build_whitelist(owner_raw: str) -> dict[str, int]` — single-argument now (drops `husband_raw`).

- [ ] **Step 1: Write the failing tests**

In `tests/test_config.py`, replace the husband-related tests and the `os.environ.setdefault("HUSBAND_ID", "2")` line at the top:

```python
import os
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")
os.environ.setdefault("TIMEZONE", "UTC")

import pytest
from wren import config, contacts

def test_build_whitelist_owner_only():
    assert config._build_whitelist("1") == {"owner": 1}

def test_build_whitelist_invalid_owner_raises():
    with pytest.raises(RuntimeError):
        config._build_whitelist("abc")
```

(Remove `test_build_whitelist_with_husband` and `test_build_whitelist_invalid_husband_raises` entirely — `HUSBAND_ID` no longer exists.)

Then add, after the existing `test_web_lookup_defaults_empty` test:

```python
@pytest.fixture
def tmp_contacts_db(tmp_path, monkeypatch):
    monkeypatch.setattr(contacts, "DB_PATH", str(tmp_path / "test.db"))
    contacts.init_db()

def test_whitelist_is_owner_only_with_no_contacts(tmp_contacts_db):
    assert config.whitelist() == {"owner": 1}

def test_whitelist_merges_in_dynamic_contacts(tmp_contacts_db):
    contacts.add("hubby", 222222222222222222)
    assert config.whitelist() == {"owner": 1, "hubby": 222222222222222222}

def test_whitelist_reflects_removal_immediately(tmp_contacts_db):
    contacts.add("hubby", 222222222222222222)
    contacts.remove("hubby")
    assert config.whitelist() == {"owner": 1}

def test_id_to_name_is_inverse_of_whitelist(tmp_contacts_db):
    contacts.add("hubby", 222222222222222222)
    assert config.id_to_name() == {1: "owner", 222222222222222222: "hubby"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config.py -v`
Expected: FAIL — `test_build_whitelist_owner_only` fails with a `TypeError` (`_build_whitelist()` still requires 2 args), and the new `whitelist`/`id_to_name` tests fail with `AttributeError: module 'wren.config' has no attribute 'whitelist'`.

- [ ] **Step 3: Update `wren/config.py`**

In `wren/config.py`, replace the whitelist-building section:

```python
from . import providers
```

becomes:

```python
from . import providers
from . import contacts
```

Replace:

```python
def _build_whitelist(owner_raw: str, husband_raw: str) -> dict[str, int]:
    if not owner_raw.isdigit():
        raise RuntimeError("OWNER_ID must be a numeric Discord user ID.")
    whitelist = {"owner": int(owner_raw)}
    if husband_raw:
        if not husband_raw.isdigit():
            raise RuntimeError("HUSBAND_ID must be a numeric Discord user ID.")
        whitelist["husband"] = int(husband_raw)
    return whitelist


_owner_raw = _require("OWNER_ID")
_husband_raw = os.environ.get("HUSBAND_ID", "")

WHITELIST: dict[str, int] = _build_whitelist(_owner_raw, _husband_raw)

ID_TO_NAME: dict[int, str] = {v: k for k, v in WHITELIST.items()}
```

with:

```python
def _build_whitelist(owner_raw: str) -> dict[str, int]:
    if not owner_raw.isdigit():
        raise RuntimeError("OWNER_ID must be a numeric Discord user ID.")
    return {"owner": int(owner_raw)}


_owner_raw = _require("OWNER_ID")

WHITELIST: dict[str, int] = _build_whitelist(_owner_raw)


def whitelist() -> dict[str, int]:
    return {**WHITELIST, **contacts.all()}


def id_to_name() -> dict[int, str]:
    return {v: k for k, v in whitelist().items()}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Remove `HUSBAND_ID` from `.env.example`**

In `.env.example`, delete these lines:

```
# Optional second whitelisted contact. Leave unset for owner-only use.
# (Planned: a DM command to add/manage additional contacts at runtime,
# instead of hand-editing this.)
# HUSBAND_ID=222222222222222222
```

- [ ] **Step 6: Commit**

```bash
git add wren/config.py tests/test_config.py .env.example
git commit -m "feat: make config.WHITELIST dynamic, drop HUSBAND_ID"
```

---

### Task 3: Switch call sites from attribute access to function calls

**Files:**
- Modify: `wren/bot.py`
- Modify: `wren/shopping_plugin.py`
- Modify: `wren/discord_utils.py`
- Modify: `wren/brain.py`
- Modify: `tests/test_shopping_plugin.py`

**Interfaces:**
- Consumes: `config.whitelist()`, `config.id_to_name()` (Task 2)

- [ ] **Step 1: Update `wren/discord_utils.py`**

Change:

```python
def notify(client: discord.Client, contact_name: str, text: str) -> bool:
    target_id = config.WHITELIST.get(contact_name.lower())
```

to:

```python
def notify(client: discord.Client, contact_name: str, text: str) -> bool:
    target_id = config.whitelist().get(contact_name.lower())
```

- [ ] **Step 2: Update `wren/brain.py`**

Change:

```python
def _contacts() -> str:
    return ", ".join(config.WHITELIST.keys())
```

to:

```python
def _contacts() -> str:
    return ", ".join(config.whitelist().keys())
```

- [ ] **Step 3: Update `wren/shopping_plugin.py`**

Change the `send_shopping_list` branch's whitelist check:

```python
        target_name = (person or "").lower()
        if target_name not in config.WHITELIST:
```

to:

```python
        target_name = (person or "").lower()
        if target_name not in config.whitelist():
```

Change `config.ID_TO_NAME.get(user_id, 'someone')` (in the `send_shopping_list` message) to `config.id_to_name().get(user_id, 'someone')`.

- [ ] **Step 4: Update `wren/bot.py`**

Change the whitelist gate:

```python
    # whitelist gate
    if user_id not in config.ID_TO_NAME:
        return
```

to:

```python
    # whitelist gate
    if user_id not in config.id_to_name():
        return
```

Change the `send_to_person` branch:

```python
        elif intent == "send_to_person":
            target_name = (person or "").lower()
            if target_name not in config.WHITELIST:
                await message.channel.send("I don't know how to reach them.")
            else:
                notes.save(user_id, content, tags)
                ok = await discord_utils.notify(
                    client, target_name,
                    f"From {config.ID_TO_NAME.get(user_id, 'someone')}: {content}",
                )
```

to:

```python
        elif intent == "send_to_person":
            target_name = (person or "").lower()
            if target_name not in config.whitelist():
                await message.channel.send("I don't know how to reach them.")
            else:
                notes.save(user_id, content, tags)
                ok = await discord_utils.notify(
                    client, target_name,
                    f"From {config.id_to_name().get(user_id, 'someone')}: {content}",
                )
```

- [ ] **Step 5: Fix `tests/test_shopping_plugin.py`'s "husband" fixture**

`send_shopping_list` tests target `"husband"`, which previously resolved via the `HUSBAND_ID` env var baked into `config.WHITELIST` at import time. Now that `config.whitelist()` reads from `contacts.py`, the fixture must seed a `"husband"` contact explicitly.

Replace the imports and fixture at the top of `tests/test_shopping_plugin.py`:

```python
from unittest.mock import MagicMock, AsyncMock, patch
from wren import shopping
from wren import shopping_plugin
from wren import discord_utils

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(shopping, "DB_PATH", str(tmp_path / "test.db"))
    shopping.init_db()
```

with:

```python
from unittest.mock import MagicMock, AsyncMock, patch
from wren import shopping
from wren import shopping_plugin
from wren import discord_utils
from wren import contacts

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(shopping, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(contacts, "DB_PATH", str(tmp_path / "test_contacts.db"))
    shopping.init_db()
    contacts.init_db()
    contacts.add("husband", 2)
```

- [ ] **Step 6: Run the full test suite**

Run: `pytest tests/ -v`
Expected: PASS (all tests, including `test_shopping_plugin.py`, `test_discord_utils.py`, `test_brain.py`)

- [ ] **Step 7: Commit**

```bash
git add wren/bot.py wren/shopping_plugin.py wren/discord_utils.py wren/brain.py tests/test_shopping_plugin.py
git commit -m "feat: read whitelist through config.whitelist()/id_to_name() everywhere"
```

---

### Task 4: `wren/contacts_plugin.py` — add/remove/list contact commands

**Files:**
- Create: `wren/contacts_plugin.py`
- Test: `tests/test_contacts_plugin.py`

**Interfaces:**
- Consumes: `contacts.add`, `contacts.remove`, `contacts.all` (Task 1); `config.whitelist`, `config.WHITELIST` (Task 2)
- Produces:
  - `contacts_plugin.INTENTS = ["add_contact", "remove_contact", "list_contacts"]`
  - `contacts_plugin.PROMPT_GUIDELINES: str`
  - `contacts_plugin.handle(intent, message, client, user_id, content, tags, person, when) -> None` (async) — same signature as every other plugin's `handle`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_contacts_plugin.py`:

```python
import os, asyncio, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import MagicMock, AsyncMock
from wren import contacts
from wren import contacts_plugin

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(contacts, "DB_PATH", str(tmp_path / "test.db"))
    contacts.init_db()

def _message():
    message = MagicMock()
    message.channel.send = AsyncMock()
    return message

# user_id=1 is OWNER_ID; user_id=2 is a non-owner whitelisted contact
def test_non_owner_cannot_add_contact():
    message = _message()
    asyncio.run(contacts_plugin.handle("add_contact", message, None, 2, "222222222222222222", [], "hubby", None))
    message.channel.send.assert_awaited_once_with("Only the owner can manage contacts.")
    assert contacts.all() == {}

def test_owner_adds_contact():
    message = _message()
    asyncio.run(contacts_plugin.handle("add_contact", message, None, 1, "222222222222222222", [], "hubby", None))
    message.channel.send.assert_awaited_once_with("Added hubby.")
    assert contacts.all() == {"hubby": 222222222222222222}

def test_owner_add_contact_non_numeric_id():
    message = _message()
    asyncio.run(contacts_plugin.handle("add_contact", message, None, 1, "not-a-number", [], "hubby", None))
    message.channel.send.assert_awaited_once_with("That doesn't look like a Discord ID.")
    assert contacts.all() == {}

def test_owner_add_contact_taken_alias():
    contacts.add("hubby", 222222222222222222)
    message = _message()
    asyncio.run(contacts_plugin.handle("add_contact", message, None, 1, "333333333333333333", [], "hubby", None))
    message.channel.send.assert_awaited_once_with("That name's already taken.")

def test_owner_add_contact_alias_matches_owner():
    message = _message()
    asyncio.run(contacts_plugin.handle("add_contact", message, None, 1, "222222222222222222", [], "owner", None))
    message.channel.send.assert_awaited_once_with("That name's already taken.")

def test_owner_removes_contact():
    contacts.add("hubby", 222222222222222222)
    message = _message()
    asyncio.run(contacts_plugin.handle("remove_contact", message, None, 1, "", [], "hubby", None))
    message.channel.send.assert_awaited_once_with("Removed hubby.")
    assert contacts.all() == {}

def test_owner_removes_unknown_contact():
    message = _message()
    asyncio.run(contacts_plugin.handle("remove_contact", message, None, 1, "", [], "nobody", None))
    message.channel.send.assert_awaited_once_with("No contact named nobody.")

def test_non_owner_cannot_remove_contact():
    contacts.add("hubby", 222222222222222222)
    message = _message()
    asyncio.run(contacts_plugin.handle("remove_contact", message, None, 2, "", [], "hubby", None))
    message.channel.send.assert_awaited_once_with("Only the owner can manage contacts.")
    assert contacts.all() == {"hubby": 222222222222222222}

def test_owner_lists_contacts_empty():
    message = _message()
    asyncio.run(contacts_plugin.handle("list_contacts", message, None, 1, "", [], None, None))
    message.channel.send.assert_awaited_once_with("No contacts yet.")

def test_owner_lists_contacts():
    contacts.add("hubby", 222222222222222222)
    contacts.add("kevin", 333333333333333333)
    message = _message()
    asyncio.run(contacts_plugin.handle("list_contacts", message, None, 1, "", [], None, None))
    message.channel.send.assert_awaited_once_with("hubby, kevin")

def test_non_owner_cannot_list_contacts():
    message = _message()
    asyncio.run(contacts_plugin.handle("list_contacts", message, None, 2, "", [], None, None))
    message.channel.send.assert_awaited_once_with("Only the owner can manage contacts.")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_contacts_plugin.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'wren.contacts_plugin'`)

- [ ] **Step 3: Write the implementation**

Create `wren/contacts_plugin.py`:

```python
from . import config
from . import contacts

INTENTS = ["add_contact", "remove_contact", "list_contacts"]

PROMPT_GUIDELINES = """- add_contact: owner wants to add a new whitelisted contact (e.g. "add 123456789012345678 as hubby"); content is the raw numeric Discord ID, person is the alias
- remove_contact: owner wants to remove a whitelisted contact (e.g. "remove hubby"); person is the alias to remove
- list_contacts: owner wants to see who's currently whitelisted (e.g. "who's whitelisted", "list contacts")"""


async def handle(intent, message, client, user_id, content, tags, person, when):
    if user_id != config.WHITELIST["owner"]:
        await message.channel.send("Only the owner can manage contacts.")
        return

    if intent == "add_contact":
        alias = (person or "").strip().lower()
        discord_id_raw = (content or "").strip()
        if not discord_id_raw.isdigit():
            await message.channel.send("That doesn't look like a Discord ID.")
            return
        if alias in config.whitelist():
            await message.channel.send("That name's already taken.")
            return
        contacts.add(alias, int(discord_id_raw))
        await message.channel.send(f"Added {alias}.")

    elif intent == "remove_contact":
        alias = (person or "").strip().lower()
        if contacts.remove(alias):
            await message.channel.send(f"Removed {alias}.")
        else:
            await message.channel.send(f"No contact named {alias}.")

    elif intent == "list_contacts":
        names = [name for name in config.whitelist() if name != "owner"]
        if not names:
            await message.channel.send("No contacts yet.")
        else:
            await message.channel.send(", ".join(names))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_contacts_plugin.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add wren/contacts_plugin.py tests/test_contacts_plugin.py
git commit -m "feat: add contacts_plugin with add/remove/list contact commands"
```

---

### Task 5: Wire the plugin into the bot, update help text

**Files:**
- Modify: `wren/plugins.py`
- Modify: `wren/bot.py`
- Modify: `tests/test_plugins.py`

**Interfaces:**
- Consumes: `contacts_plugin.INTENTS`, `contacts_plugin.PROMPT_GUIDELINES`, `contacts_plugin.handle` (Task 4); `contacts.init_db` (Task 1)

- [ ] **Step 1: Write the failing test**

In `tests/test_plugins.py`, add the import and a new test at the end:

```python
from wren import contacts_plugin
```

(add alongside the existing plugin imports at the top)

```python
def test_contacts_plugin_registered():
    assert contacts_plugin in plugins.PLUGINS

def test_all_intents_includes_contacts_intents():
    intents = plugins.all_intents()
    for intent in contacts_plugin.INTENTS:
        assert intent in intents
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_plugins.py -v`
Expected: FAIL (`AssertionError` — `contacts_plugin` not in `plugins.PLUGINS`)

- [ ] **Step 3: Register the plugin in `wren/plugins.py`**

Change:

```python
from . import notes_plugin
from . import shopping_plugin
from . import email_plugin
from . import reminder_plugin
from . import web_plugin

PLUGINS = [notes_plugin, shopping_plugin, email_plugin, reminder_plugin, web_plugin]
```

to:

```python
from . import notes_plugin
from . import shopping_plugin
from . import email_plugin
from . import reminder_plugin
from . import web_plugin
from . import contacts_plugin

PLUGINS = [notes_plugin, shopping_plugin, email_plugin, reminder_plugin, web_plugin, contacts_plugin]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_plugins.py -v`
Expected: PASS

- [ ] **Step 5: Init the contacts DB in `wren/bot.py`'s `on_ready`, update `HELP_TEXT`**

Change:

```python
from . import notes
from . import shopping
from . import reminders
from . import plugins
from . import discord_utils
```

to:

```python
from . import notes
from . import shopping
from . import reminders
from . import contacts
from . import plugins
from . import discord_utils
```

Change:

```python
@client.event
async def on_ready():
    notes.init_db()
    shopping.init_db()
    reminders.init_db()
    await plugins.start_all(client)
    logging.info(f"Wren online as {client.user}")
```

to:

```python
@client.event
async def on_ready():
    notes.init_db()
    shopping.init_db()
    reminders.init_db()
    contacts.init_db()
    await plugins.start_all(client)
    logging.info(f"Wren online as {client.user}")
```

In `HELP_TEXT`, change:

```
**Messaging**
- "tell husband dinner's at 7" — DMs the other whitelisted contact
```

to:

```
**Messaging**
- "tell hubby dinner's at 7" — DMs a whitelisted contact by name

**Contacts** (owner only)
- "add 123456789012345678 as hubby" — whitelists a new contact
- "remove hubby" — un-whitelists a contact
- "who's whitelisted" — lists current contacts
```

And change:

```
- "send shopping to husband"
```

to:

```
- "send shopping to hubby"
```

- [ ] **Step 6: Run the full test suite**

Run: `pytest tests/ -v`
Expected: PASS (all tests)

- [ ] **Step 7: Commit**

```bash
git add wren/plugins.py wren/bot.py tests/test_plugins.py
git commit -m "feat: register contacts_plugin, init contacts DB, update help text"
```

---

## Post-plan manual check

`bot.py` has no automated test coverage in this repo (module-level `client.run()` makes it unimportable without a real Discord token — pre-existing condition, unrelated to this feature). After all tasks land, manually verify against a running bot instance:
1. DM as the owner: `add 123456789012345678 as testcontact` → confirms "Added testcontact."
2. DM as the owner: `who's whitelisted` → confirms `testcontact` appears.
3. DM as a non-owner whitelisted contact (if one exists): `add 999 as someone` → confirms the owner-only rejection.
4. DM as the owner: `remove testcontact` → confirms "Removed testcontact."
5. Restart the bot process, DM as the owner: `who's whitelisted` → confirms the list is empty again (persistence didn't retain the removed contact, and survives restart for anything not removed).
