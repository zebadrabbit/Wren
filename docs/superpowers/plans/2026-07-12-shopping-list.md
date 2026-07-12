# Shopping List with Common-Item Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wren supports adding/removing items on one shared household shopping list via short DMs, recalling the current list (with "you often get" suggestions for frequently-requested items), and sending the whole list to another whitelisted contact.

**Architecture:** A new `shopping.py` module (parallel to `notes.py`, not built on it) owns a new `shopping_items` SQLite table with no per-owner scoping. `brain.py`'s intent-detection prompt grows four new intent values. `bot.py`'s `on_message` dispatch grows four new branches that call `shopping.py`'s functions.

**Tech Stack:** Python 3.11+, `sqlite3` (stdlib), `pytest`. No new dependencies.

## Global Constraints

- One shared list — no `owner_id`/per-user scoping (unlike `notes.py`).
- Item matching is normalized-exact (`strip().lower()`), never fuzzy/LLM-based.
- Adding an item with an active duplicate is a no-op, not a second row.
- Removal sets `status='removed'`, never deletes the row — historical adds (including removed ones) count toward the "common" frequency total.
- "Common" items are computed on read (`GROUP BY` + `HAVING COUNT(*) >= threshold`), never a stored/denormalized flag.
- `brain.detect_intent`/`recall`/`chat` keep their existing signatures — this plan only changes the system prompt text and the set of valid `intent` values, not the function contract.
- No `tests/test_bot.py` — `bot.py` has no existing test coverage in this repo (it's a thin `discord.py` dispatch layer whose module-level `client.run(...)` call makes it unimportable without a real bot token); this plan doesn't change that.

---

### Task 1: `shopping.py` — shared shopping list storage

**Files:**
- Create: `shopping.py`
- Test: `tests/test_shopping.py`

**Interfaces:**
- Produces:
  - `shopping.init_db() -> None`
  - `shopping.add(item_text: str, added_by: str) -> tuple[int, bool]` — `(row id, was_newly_added)`; `was_newly_added` is `False` when an active duplicate already existed (returns that row's id).
  - `shopping.remove(item_text: str) -> bool` — `True` if an active match was found and marked removed, `False` otherwise.
  - `shopping.active_items() -> list[dict]` — rows with `status='active'`, ordered by `added_at` ascending (oldest first — the order items were added).
  - `shopping.common_items(threshold: int = 3, limit: int = 5) -> list[dict]` — `[{"item": str, "count": int}, ...]`, ordered by count descending.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_shopping.py`:

```python
import os, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("HUSBAND_ID", "2")

import shopping

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(shopping, "DB_PATH", str(tmp_path / "test.db"))
    shopping.init_db()

def test_add_and_active_items():
    _, was_new = shopping.add("Potatoes", "owner")
    assert was_new is True
    items = shopping.active_items()
    assert len(items) == 1
    assert items[0]["item"] == "potatoes"
    assert items[0]["original_text"] == "Potatoes"
    assert items[0]["added_by"] == "owner"

def test_add_dedupes_active_item():
    id1, new1 = shopping.add("potatoes", "owner")
    id2, new2 = shopping.add("Potatoes ", "husband")
    assert new1 is True
    assert new2 is False
    assert id1 == id2
    assert len(shopping.active_items()) == 1

def test_remove_active_item():
    shopping.add("milk", "owner")
    removed = shopping.remove("Milk")
    assert removed is True
    assert shopping.active_items() == []

def test_remove_not_active_returns_false():
    removed = shopping.remove("nonexistent")
    assert removed is False

def test_removed_item_can_be_readded():
    shopping.add("eggs", "owner")
    shopping.remove("eggs")
    _, was_new = shopping.add("eggs", "owner")
    assert was_new is True
    assert len(shopping.active_items()) == 1

def test_common_items_threshold():
    shopping.add("bread", "owner")
    shopping.remove("bread")
    shopping.add("bread", "owner")
    shopping.remove("bread")
    shopping.add("bread", "owner")
    common = shopping.common_items(threshold=3)
    assert len(common) == 1
    assert common[0]["item"] == "bread"
    assert common[0]["count"] == 3

def test_common_items_below_threshold_excluded():
    shopping.add("rare item", "owner")
    common = shopping.common_items(threshold=3)
    assert common == []

def test_common_items_counts_removed_items():
    for _ in range(3):
        shopping.add("butter", "owner")
        shopping.remove("butter")
    common = shopping.common_items(threshold=3)
    assert any(c["item"] == "butter" and c["count"] == 3 for c in common)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_shopping.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shopping'`

- [ ] **Step 3: Write `shopping.py`**

```python
import sqlite3
from datetime import datetime, timezone

DB_PATH = "wren.db"

def _conn():
    return sqlite3.connect(DB_PATH)

def init_db() -> None:
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS shopping_items (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                item          TEXT NOT NULL,
                original_text TEXT NOT NULL,
                added_by      TEXT NOT NULL,
                status        TEXT NOT NULL,
                added_at      TEXT NOT NULL
            )
        """)

def add(item_text: str, added_by: str) -> tuple[int, bool]:
    normalized = item_text.strip().lower()
    with _conn() as con:
        con.row_factory = sqlite3.Row
        existing = con.execute(
            "SELECT id FROM shopping_items WHERE item=? AND status='active'",
            (normalized,),
        ).fetchone()
        if existing:
            return existing["id"], False
        ts = datetime.now(timezone.utc).isoformat()
        cur = con.execute(
            "INSERT INTO shopping_items (item, original_text, added_by, status, added_at) VALUES (?,?,?,?,?)",
            (normalized, item_text.strip(), added_by, "active", ts),
        )
        return cur.lastrowid, True

def remove(item_text: str) -> bool:
    normalized = item_text.strip().lower()
    with _conn() as con:
        cur = con.execute(
            "UPDATE shopping_items SET status='removed' WHERE item=? AND status='active'",
            (normalized,),
        )
        return cur.rowcount > 0

def active_items() -> list[dict]:
    with _conn() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM shopping_items WHERE status='active' ORDER BY added_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]

def common_items(threshold: int = 3, limit: int = 5) -> list[dict]:
    with _conn() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT item, COUNT(*) as count
            FROM shopping_items
            GROUP BY item
            HAVING COUNT(*) >= ?
            ORDER BY count DESC
            LIMIT ?
            """,
            (threshold, limit),
        ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_shopping.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: all tests pass (existing tests untouched by this task still pass)

- [ ] **Step 6: Commit**

```bash
git add shopping.py tests/test_shopping.py
git commit -m "feat: add shared shopping list storage with common-item tracking"
```

---

### Task 2: `brain.py` — add shopping intents to the detection prompt

**Files:**
- Modify: `brain.py:11-27` (`_SYSTEM` prompt: intent enum line and guidelines)
- Test: `tests/test_brain.py` (add cases)

**Interfaces:**
- Consumes: nothing new (no dependency on `shopping.py` — this task only changes prompt text)
- Produces: no new functions. `detect_intent`'s possible `result["intent"]` values grow to include `"add_shopping_item"`, `"remove_shopping_item"`, `"recall_shopping"`, `"send_shopping_list"`. Signature and JSON-parsing/fallback logic unchanged.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_brain.py` (uses the existing `_client_returning` helper already defined in that file):

```python
def test_detect_intent_add_shopping_item():
    payload = json.dumps({
        "intent": "add_shopping_item",
        "content": "potatoes",
        "tags": [],
        "person": None
    })
    with patch.object(brain, "_get_client", return_value=_client_returning(payload)):
        result = brain.detect_intent(1, "add potatoes to shopping")
    assert result["intent"] == "add_shopping_item"
    assert result["content"] == "potatoes"

def test_detect_intent_remove_shopping_item():
    payload = json.dumps({
        "intent": "remove_shopping_item",
        "content": "potatoes",
        "tags": [],
        "person": None
    })
    with patch.object(brain, "_get_client", return_value=_client_returning(payload)):
        result = brain.detect_intent(1, "got the potatoes")
    assert result["intent"] == "remove_shopping_item"
    assert result["content"] == "potatoes"

def test_detect_intent_recall_shopping():
    payload = json.dumps({
        "intent": "recall_shopping",
        "content": "",
        "tags": [],
        "person": None
    })
    with patch.object(brain, "_get_client", return_value=_client_returning(payload)):
        result = brain.detect_intent(1, "what's on the shopping list")
    assert result["intent"] == "recall_shopping"

def test_detect_intent_send_shopping_list():
    payload = json.dumps({
        "intent": "send_shopping_list",
        "content": "",
        "tags": [],
        "person": "husband"
    })
    with patch.object(brain, "_get_client", return_value=_client_returning(payload)):
        result = brain.detect_intent(1, "send shopping to husband")
    assert result["intent"] == "send_shopping_list"
    assert result["person"] == "husband"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_brain.py -v`
Expected: FAIL — these 4 new tests fail with `KeyError` or an assertion mismatch (the current code's `detect_intent` will actually still parse these payloads successfully, since it doesn't validate the `intent` field against an enum — but write them first and confirm you understand any failure before moving on; if they unexpectedly pass already, say so in your report rather than skipping to Step 3, since the point of this task is prompt-text coverage going forward, not new parsing logic).

- [ ] **Step 3: Update `_SYSTEM` in `brain.py`**

Replace lines 11-27 (from `When classifying intent...` through the last guideline bullet):

```python
When classifying intent, respond ONLY with valid JSON matching this schema:
{{
  "intent": "save_note" | "recall_notes" | "send_to_person" | "add_shopping_item" | "remove_shopping_item" | "recall_shopping" | "send_shopping_list" | "chat",
  "content": "<extracted note or message content>",
  "tags": ["<tag1>", "<tag2>"],
  "person": "<name from whitelist or null>"
}}

Known contacts: {contacts}

Guidelines:
- save_note: user is capturing something for later (grocery item, plan, idea, reminder)
- recall_notes: user wants to retrieve or search past notes
- send_to_person: user wants to send a message or note to someone
- add_shopping_item: user wants to add an item to the shared shopping list
- remove_shopping_item: user got/bought/already has an item and wants it off the shopping list
- recall_shopping: user wants to see the current shopping list
- send_shopping_list: user wants to send the whole shopping list to someone
- chat: anything else (questions, casual conversation)
- tags: 1-3 lowercase single-word tags relevant to the content
- person: only set for send_to_person and send_shopping_list intents, use the contact name as given
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_brain.py -v`
Expected: PASS (all tests, including the 4 new ones)

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add brain.py tests/test_brain.py
git commit -m "feat: add shopping-list intents to LLM intent-detection prompt"
```

---

### Task 3: `bot.py` — dispatch shopping intents

**Files:**
- Modify: `bot.py:1-5` (imports), `bot.py`'s `on_ready` (add `shopping.init_db()`), `bot.py`'s `on_message` (4 new `elif` branches, inserted between the existing `send_to_person` branch and the final `else: # chat`)

**Interfaces:**
- Consumes: `shopping.add(item_text, added_by) -> tuple[int, bool]`, `shopping.remove(item_text) -> bool`, `shopping.active_items() -> list[dict]` (each dict has `item`, `original_text`, `added_by`, `status`, `added_at`), `shopping.common_items(threshold=3, limit=5) -> list[dict]` (each dict has `item`, `count`) — all from Task 1. Prompt intent values `add_shopping_item`/`remove_shopping_item`/`recall_shopping`/`send_shopping_list` from Task 2.
- Produces: no new functions — this task only extends `on_message`'s existing if/elif chain.

There is no automated test for this task (see Global Constraints — `bot.py` has no test coverage in this repo and its module-level `client.run(...)` call makes it unimportable without a real Discord token). Verification is manual: read the diff back against the task's code below, and run the full test suite to confirm nothing else broke.

- [ ] **Step 1: Add the `shopping` import**

At the top of `bot.py`, change:

```python
import logging
import discord
import config
import brain
import notes
```

to:

```python
import logging
import discord
import config
import brain
import notes
import shopping
```

- [ ] **Step 2: Initialize the shopping DB on startup**

In `on_ready`, change:

```python
@client.event
async def on_ready():
    notes.init_db()
    logging.info(f"Wren online as {client.user}")
```

to:

```python
@client.event
async def on_ready():
    notes.init_db()
    shopping.init_db()
    logging.info(f"Wren online as {client.user}")
```

- [ ] **Step 3: Add the four dispatch branches**

In `on_message`, insert these branches immediately after the existing `elif intent == "send_to_person":` block (which ends with the `except (discord.NotFound, discord.Forbidden) as e:` handler) and before `else:  # chat`:

```python
        elif intent == "add_shopping_item":
            _, was_new = shopping.add(content, added_by=config.ID_TO_NAME[user_id])
            if was_new:
                await message.channel.send(f"Added {content}.")
            else:
                await message.channel.send("Already on the list.")

        elif intent == "remove_shopping_item":
            removed = shopping.remove(content)
            if removed:
                await message.channel.send(f"Got it, removed {content}.")
            else:
                await message.channel.send(f"{content} wasn't on the list.")

        elif intent == "recall_shopping":
            active = shopping.active_items()
            common = shopping.common_items()
            active_normalized = {i["item"] for i in active}
            suggestions = [c["item"] for c in common if c["item"] not in active_normalized]
            lines = []
            if active:
                lines.append(", ".join(i["original_text"] for i in active))
            if suggestions:
                lines.append("You often get: " + ", ".join(suggestions) + ".")
            if lines:
                await message.channel.send("\n".join(lines))
            else:
                await message.channel.send("Shopping list is empty.")

        elif intent == "send_shopping_list":
            target_name = (person or "").lower()
            target_id = config.WHITELIST.get(target_name)
            if not target_id:
                await message.channel.send("I don't know how to reach them.")
                return
            active = shopping.active_items()
            if not active:
                await message.channel.send("Nothing on the list to send.")
                return
            list_text = ", ".join(i["original_text"] for i in active)
            try:
                target_user = await client.fetch_user(target_id)
                await target_user.send(f"Shopping list from {config.ID_TO_NAME.get(user_id, 'someone')}: {list_text}")
                await message.channel.send(f"Sent to {target_name}.")
            except (discord.NotFound, discord.Forbidden) as e:
                logging.error(f"Could not DM {target_name}: {e}")
                await message.channel.send(f"Couldn't reach {target_name} — their DMs may be closed.")
                return
```

- [ ] **Step 4: Run the full test suite**

Run: `pytest -q`
Expected: all tests pass (this task adds no new automated tests — it's verifying the rest of the suite still passes after editing `bot.py`)

- [ ] **Step 5: Manual read-back verification**

Re-read the modified `bot.py` in full and confirm: `shopping` is imported; `shopping.init_db()` is called in `on_ready`; all four new `elif` branches are present, correctly indented at the same level as the existing `if intent == "save_note":` chain, and placed before `else:  # chat`; the existing `save_note`/`recall_notes`/`send_to_person`/`chat` branches are unchanged.

- [ ] **Step 6: Commit**

```bash
git add bot.py
git commit -m "feat: dispatch shopping-list intents in Discord message handler"
```

---

## Post-plan verification

- [ ] Run `pytest -q` — expect all tests (existing + new) passing.
- [ ] Manually confirm `bot.py`'s four new branches are reachable (correct indentation/placement) by reading the file once more, since there is no automated test exercising `on_message` directly.
