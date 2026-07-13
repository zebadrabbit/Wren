# Reminders / Timers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One-time reminders — "remind me in 20 minutes to check the oven" saves a per-owner reminder; a background poll loop DMs the owner when it's due.

**Architecture:** Six tasks. Task 1 (discord_utils.py) and Task 2 (brain.py) and Task 3 (reminders.py) are independent of each other. Task 4 wires a new `when` field through the existing dispatch pipeline (bot.py + the two existing plugins), which must land before Task 5's `reminder_plugin.py` can consume `when` via the same `handle()` signature. Task 6 registers the new plugin.

**Tech Stack:** Python 3.11+, stdlib `sqlite3`/`datetime`/`asyncio`, `pytest`, `asyncio.run(...)` from plain sync test functions (established pattern, no `pytest-asyncio`). No new dependencies.

## Global Constraints

- One-time reminders only — no recurrence (daily/weekly is explicitly out of scope).
- All reminder timestamps (`fire_at`, and the "now" value compared against it) use `datetime.isoformat(timespec="seconds")` consistently — mixing microsecond-precision and seconds-precision ISO strings breaks lexicographic `<=` comparison (`"21:00:00+00:00"` sorts before `"20:59:59.5+00:00"` because `+` < `.`).
- The model-computed `when` value is never trusted blindly: it must parse via `datetime.fromisoformat()` and be no more than 30 seconds in the past (grace window). Anything else — unparseable, too far in the past, missing — gets a clarifying reply instead of a saved reminder.
- Every plugin's `handle()` signature gains a trailing `when` parameter (matching the existing pattern where every intent receives every field and most ignore what's irrelevant, e.g. `recall_notes` already ignores `person`). This is a breaking signature change applied uniformly to `notes_plugin.py` and `shopping_plugin.py`, not just the new plugin.
- A reminder that fails to deliver (DM forbidden/not found) is still marked `fired`, not retried — logged as a warning instead. A one-time reminder retried forever against a permanently broken DM is worse than a dropped one with a log entry.
- Still no `tests/test_bot.py` (pre-existing condition, `bot.py`'s module-level `client.run()` call).

---

### Task 1: `discord_utils.py` — extract a by-ID notify helper

**Files:**
- Modify: `wren/discord_utils.py`
- Test: `tests/test_discord_utils.py` (add cases)

**Interfaces:**
- Produces: `async def notify_id(client: discord.Client, user_id: int, text: str) -> bool` — fetches the user by raw Discord ID and DMs them `text`; `True` on success, `False` on `discord.NotFound`/`discord.Forbidden` (logged).
- `notify(client, contact_name, text) -> bool` keeps its exact existing signature and behavior, now implemented as a `WHITELIST` lookup followed by `notify_id()`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_discord_utils.py`:

```python
def test_notify_id_success():
    client = MagicMock()
    target_user = MagicMock()
    target_user.send = AsyncMock()
    client.fetch_user = AsyncMock(return_value=target_user)

    result = asyncio.run(discord_utils.notify_id(client, 42, "hello"))

    assert result is True
    target_user.send.assert_awaited_once_with("hello")
    client.fetch_user.assert_awaited_once_with(42)

def test_notify_id_forbidden_returns_false():
    client = MagicMock()
    fake_response = MagicMock(status=403, reason="Forbidden")
    client.fetch_user = AsyncMock(side_effect=discord.Forbidden(fake_response, "Cannot send messages to this user"))

    result = asyncio.run(discord_utils.notify_id(client, 42, "hello"))

    assert result is False

def test_notify_id_not_found_returns_false():
    client = MagicMock()
    fake_response = MagicMock(status=404, reason="Not Found")
    client.fetch_user = AsyncMock(side_effect=discord.NotFound(fake_response, "Unknown user"))

    result = asyncio.run(discord_utils.notify_id(client, 42, "hello"))

    assert result is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_discord_utils.py -v`
Expected: FAIL with `AttributeError: module 'wren.discord_utils' has no attribute 'notify_id'`

- [ ] **Step 3: Update `wren/discord_utils.py`**

Replace the whole file:

```python
import logging
import discord
from . import config

async def notify_id(client: discord.Client, user_id: int, text: str) -> bool:
    try:
        target_user = await client.fetch_user(user_id)
        await target_user.send(text)
        return True
    except (discord.NotFound, discord.Forbidden) as e:
        logging.error(f"Could not DM user {user_id}: {e}")
        return False

async def notify(client: discord.Client, contact_name: str, text: str) -> bool:
    target_id = config.WHITELIST.get(contact_name.lower())
    if not target_id:
        return False
    return await notify_id(client, target_id, text)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_discord_utils.py -v`
Expected: PASS (7 tests — 4 existing `notify()` tests unaffected, plus 3 new `notify_id()` tests)

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add wren/discord_utils.py tests/test_discord_utils.py
git commit -m "refactor: extract discord_utils.notify_id() for by-ID DM delivery"
```

---

### Task 2: `brain.py` — add the `when` field

**Files:**
- Modify: `wren/brain.py` (the `_SYSTEM` template only)
- Test: `tests/test_brain.py` (add one case)

**Interfaces:**
- No function signature changes. `detect_intent()`'s returned dict may now include a `"when"` key (a string or `None`); callers read it via `.get("when")` same as any other optional field.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_brain.py`:

```python
def test_detect_intent_prompt_includes_when_field():
    payload = json.dumps({"intent": "chat", "content": "hi", "tags": [], "person": None, "when": None})
    captured = {}

    def fake_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        return _mock_completion(payload)

    client = MagicMock()
    client.chat.completions.create.side_effect = fake_create
    with patch.object(brain, "_get_client", return_value=client):
        brain.detect_intent(1, "hello")

    system_content = captured["messages"][0]["content"]
    assert '"when"' in system_content
    assert "set_reminder" in system_content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_brain.py::test_detect_intent_prompt_includes_when_field -v`
Expected: FAIL — `'"when"' not found` (current `_SYSTEM` has no `when` field or `set_reminder` mention)

- [ ] **Step 3: Update `_SYSTEM` in `wren/brain.py`**

Replace the whole `_SYSTEM` string:

```python
_SYSTEM = """You are Wren, a private personal assistant. You are short, structured, and ready. No filler, no affirmations.

Answer directly. Do not show reasoning, planning, or a thinking process —
no <think> tags, no step-by-step deliberation, just the final output. /no_think

Today is {date}.

When classifying intent, respond ONLY with valid JSON matching this schema:
{{
  "intent": "send_to_person" | "help" | {plugin_intents} | "chat",
  "content": "<extracted note or message content>",
  "tags": ["<tag1>", "<tag2>"],
  "person": "<name from whitelist or null>",
  "when": "<ISO 8601 UTC datetime for set_reminder, or null>"
}}

Known contacts: {contacts}

Guidelines:
- send_to_person: user wants to send a message or note to someone
- help: user wants to know what Wren can do, asks for help, or asks to see available commands
{plugin_guidelines}
- chat: anything else (questions, casual conversation)
- tags: 1-3 lowercase single-word tags relevant to the content
- person: only set when the intent is about contacting or sending something to someone else, use the contact name as given
- when: only set for set_reminder — an absolute ISO 8601 UTC datetime computed from the user's relative/absolute time phrase and the current date/time above; null otherwise
"""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_brain.py -v`
Expected: PASS (all tests, including the new one)

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add wren/brain.py tests/test_brain.py
git commit -m "feat: add when field to brain.py's intent-detection schema"
```

---

### Task 3: `reminders.py` — storage

**Files:**
- Create: `wren/reminders.py`
- Test: `tests/test_reminders.py`

**Interfaces:**
- Produces:
  - `init_db() -> None`
  - `save(owner_id: int, content: str, fire_at: str) -> int` — `fire_at` is a caller-supplied ISO string (already normalized to `timespec="seconds"` by the caller); returns the new row id.
  - `pending(owner_id: int) -> list[dict]` — this owner's `status='pending'` reminders, ordered by `fire_at` ascending.
  - `find_pending(owner_id: int, substring: str) -> list[dict]` — case-insensitive substring match on `content`, scoped to this owner's pending reminders.
  - `cancel(reminder_id: int) -> bool` — `pending` → `cancelled`; `True` if a row was actually changed.
  - `due(now_iso: str) -> list[dict]` — ALL owners' `status='pending'` reminders with `fire_at <= now_iso`, ordered by `fire_at` ascending.
  - `mark_fired(reminder_id: int) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reminders.py`:

```python
import os, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("HUSBAND_ID", "2")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from wren import reminders

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(reminders, "DB_PATH", str(tmp_path / "test.db"))
    reminders.init_db()

def test_save_and_pending():
    reminders.save(1, "check the oven", "2026-07-12T21:00:00+00:00")
    items = reminders.pending(1)
    assert len(items) == 1
    assert items[0]["content"] == "check the oven"
    assert items[0]["status"] == "pending"

def test_pending_ordered_soonest_first():
    reminders.save(1, "later", "2026-07-12T22:00:00+00:00")
    reminders.save(1, "sooner", "2026-07-12T21:00:00+00:00")
    items = reminders.pending(1)
    assert [i["content"] for i in items] == ["sooner", "later"]

def test_pending_scoped_to_owner():
    reminders.save(1, "mine", "2026-07-12T21:00:00+00:00")
    reminders.save(2, "theirs", "2026-07-12T21:00:00+00:00")
    assert len(reminders.pending(1)) == 1
    assert len(reminders.pending(2)) == 1

def test_find_pending_case_insensitive_substring():
    reminders.save(1, "Check the Oven", "2026-07-12T21:00:00+00:00")
    results = reminders.find_pending(1, "oven")
    assert len(results) == 1

def test_find_pending_excludes_cancelled():
    reminder_id = reminders.save(1, "check the oven", "2026-07-12T21:00:00+00:00")
    reminders.cancel(reminder_id)
    assert reminders.find_pending(1, "oven") == []

def test_cancel_returns_true_and_marks_cancelled():
    reminder_id = reminders.save(1, "check the oven", "2026-07-12T21:00:00+00:00")
    assert reminders.cancel(reminder_id) is True
    assert reminders.pending(1) == []

def test_cancel_nonexistent_returns_false():
    assert reminders.cancel(9999) is False

def test_cancel_already_cancelled_returns_false():
    reminder_id = reminders.save(1, "check the oven", "2026-07-12T21:00:00+00:00")
    reminders.cancel(reminder_id)
    assert reminders.cancel(reminder_id) is False

def test_due_returns_pending_at_or_before_now():
    reminders.save(1, "past", "2026-07-12T20:00:00+00:00")
    reminders.save(1, "future", "2026-07-12T23:00:00+00:00")
    due = reminders.due("2026-07-12T21:00:00+00:00")
    assert [d["content"] for d in due] == ["past"]

def test_due_excludes_fired_and_cancelled():
    fired_id = reminders.save(1, "already fired", "2026-07-12T20:00:00+00:00")
    reminders.mark_fired(fired_id)
    cancelled_id = reminders.save(1, "cancelled one", "2026-07-12T20:00:00+00:00")
    reminders.cancel(cancelled_id)
    due = reminders.due("2026-07-12T21:00:00+00:00")
    assert due == []

def test_mark_fired_removes_from_due():
    reminder_id = reminders.save(1, "check the oven", "2026-07-12T20:00:00+00:00")
    reminders.mark_fired(reminder_id)
    assert reminders.due("2026-07-12T21:00:00+00:00") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_reminders.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'wren.reminders'`

- [ ] **Step 3: Write `wren/reminders.py`**

```python
import sqlite3
from datetime import datetime, timezone

DB_PATH = "wren.db"

def _conn():
    return sqlite3.connect(DB_PATH)

def init_db() -> None:
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id   TEXT NOT NULL,
                content    TEXT NOT NULL,
                fire_at    TEXT NOT NULL,
                status     TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

def save(owner_id: int, content: str, fire_at: str) -> int:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO reminders (owner_id, content, fire_at, status, created_at) VALUES (?,?,?,?,?)",
            (str(owner_id), content, fire_at, "pending", ts),
        )
        return cur.lastrowid

def pending(owner_id: int) -> list[dict]:
    with _conn() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM reminders WHERE owner_id=? AND status='pending' ORDER BY fire_at ASC",
            (str(owner_id),),
        ).fetchall()
    return [dict(r) for r in rows]

def find_pending(owner_id: int, substring: str) -> list[dict]:
    needle = substring.lower()
    return [r for r in pending(owner_id) if needle in r["content"].lower()]

def cancel(reminder_id: int) -> bool:
    with _conn() as con:
        cur = con.execute(
            "UPDATE reminders SET status='cancelled' WHERE id=? AND status='pending'",
            (reminder_id,),
        )
        return cur.rowcount > 0

def due(now_iso: str) -> list[dict]:
    with _conn() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM reminders WHERE status='pending' AND fire_at<=? ORDER BY fire_at ASC",
            (now_iso,),
        ).fetchall()
    return [dict(r) for r in rows]

def mark_fired(reminder_id: int) -> None:
    with _conn() as con:
        con.execute("UPDATE reminders SET status='fired' WHERE id=?", (reminder_id,))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_reminders.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add wren/reminders.py tests/test_reminders.py
git commit -m "feat: add reminders.py per-owner reminder storage"
```

---

### Task 4: Wire `when` through the existing dispatch pipeline

**Files:**
- Modify: `wren/bot.py` (extract `when`, pass to `handle()` calls)
- Modify: `wren/notes_plugin.py` (signature only)
- Modify: `wren/shopping_plugin.py` (signature only)
- Modify: `tests/test_notes_plugin.py`, `tests/test_shopping_plugin.py` (every `.handle(...)` call gains a trailing `None` argument)

**Interfaces:**
- `notes_plugin.handle` / `shopping_plugin.handle` signatures become `(intent, message, client, user_id, content, tags, person, when)` — `when` is accepted and ignored by both; no behavior change.
- `bot.py` extracts `when = result.get("when")` alongside the existing `content`/`tags`/`person` extraction and passes it as the 8th positional argument to `plugins.INTENT_HANDLERS[intent].handle(...)`.

- [ ] **Step 1: Update `wren/bot.py`**

Change:

```python
        result = brain.detect_intent(user_id, text)
        intent = result.get("intent", "chat")
        content = result.get("content", text)
        tags = result.get("tags", [])
        person = result.get("person")

        if intent in plugins.INTENT_HANDLERS:
            await plugins.INTENT_HANDLERS[intent].handle(
                intent, message, client, user_id, content, tags, person
            )
```

to:

```python
        result = brain.detect_intent(user_id, text)
        intent = result.get("intent", "chat")
        content = result.get("content", text)
        tags = result.get("tags", [])
        person = result.get("person")
        when = result.get("when")

        if intent in plugins.INTENT_HANDLERS:
            await plugins.INTENT_HANDLERS[intent].handle(
                intent, message, client, user_id, content, tags, person, when
            )
```

Nothing else in `bot.py` changes.

- [ ] **Step 2: Update `wren/notes_plugin.py`'s signature**

Change only:

```python
async def handle(intent, message, client, user_id, content, tags, person):
```

to:

```python
async def handle(intent, message, client, user_id, content, tags, person, when):
```

The function body is completely unchanged — `when` is simply accepted and unused.

- [ ] **Step 3: Update `wren/shopping_plugin.py`'s signature**

Same change:

```python
async def handle(intent, message, client, user_id, content, tags, person):
```

to:

```python
async def handle(intent, message, client, user_id, content, tags, person, when):
```

- [ ] **Step 4: Replace `tests/test_notes_plugin.py` in full**

Every `.handle(...)` call gains one trailing `None` argument (for `when`); nothing else changes. Replace the whole file:

```python
import os, asyncio, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("HUSBAND_ID", "2")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import MagicMock, AsyncMock, patch
from wren import notes
from wren import brain
from wren import notes_plugin

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(notes, "DB_PATH", str(tmp_path / "test.db"))
    notes.init_db()

def _message():
    message = MagicMock()
    message.channel.send = AsyncMock()
    return message

def test_save_note():
    message = _message()
    asyncio.run(notes_plugin.handle("save_note", message, None, 1, "buy milk", ["grocery"], None, None))
    message.channel.send.assert_awaited_once_with("Saved.")
    assert notes.list_recent(1)[0]["content"] == "buy milk"

def test_recall_notes_no_matches():
    message = _message()
    asyncio.run(notes_plugin.handle("recall_notes", message, None, 1, "milk", [], None, None))
    message.channel.send.assert_awaited_once_with("No notes found.")

def test_recall_notes_with_matches():
    notes.save(1, "buy milk", ["grocery"])
    message = _message()
    with patch.object(brain, "recall", return_value="You need milk."):
        asyncio.run(notes_plugin.handle("recall_notes", message, None, 1, "what groceries", [], None, None))
    message.channel.send.assert_awaited_once_with("You need milk.")

def test_recall_notes_falls_back_when_guessed_tag_does_not_match():
    # Regression test: the LLM may guess a tag for the recall query itself
    # (e.g. "notes") that doesn't match the tags actually used when the note
    # was saved (e.g. "self,motivation") — recall_notes must not report "No
    # notes found." just because that guessed tag doesn't overlap.
    notes.save(1, "how awesome you are", ["self", "motivation"])
    message = _message()
    with patch.object(brain, "recall", return_value="You're awesome."):
        asyncio.run(notes_plugin.handle("recall_notes", message, None, 1, "tell me something nice", ["notes"], None, None))
    message.channel.send.assert_awaited_once_with("You're awesome.")

def test_recall_notes_plain_listing_sends_one_message_per_note():
    # A bare "show my notes" (empty content) lists notes directly, one
    # Discord message per note, instead of routing through the LLM.
    notes.save(1, "buy milk", ["grocery"])
    notes.save(1, "how awesome you are", ["self", "motivation"])
    message = _message()
    asyncio.run(notes_plugin.handle("recall_notes", message, None, 1, "", [], None, None))
    assert message.channel.send.await_count == 2
    sent_texts = [c.args[0] for c in message.channel.send.await_args_list]
    assert any("buy milk" in t and "(tags: grocery)" in t for t in sent_texts)
    assert any("how awesome you are" in t and "(tags: self,motivation)" in t for t in sent_texts)

def test_recall_notes_respects_real_tag_match():
    notes.save(1, "buy milk", ["grocery"])
    notes.save(1, "fix the fence", ["home"])
    message = _message()
    with patch.object(brain, "recall", return_value="You need milk.") as mock_recall:
        asyncio.run(notes_plugin.handle("recall_notes", message, None, 1, "groceries", ["grocery"], None, None))
    message.channel.send.assert_awaited_once_with("You need milk.")
    # only the grocery-tagged note should have been passed to brain.recall
    passed_notes = mock_recall.call_args[0][0]
    assert len(passed_notes) == 1
    assert passed_notes[0]["content"] == "buy milk"

def test_save_idea_empty_content_guarded():
    message = _message()
    asyncio.run(notes_plugin.handle("save_idea", message, None, 1, "  ", [], None, None))
    message.channel.send.assert_awaited_once_with("What idea should I save?")
    assert notes.search(1, tags=["idea"]) == []

def test_save_idea():
    message = _message()
    asyncio.run(notes_plugin.handle("save_idea", message, None, 1, "build a treehouse", [], None, None))
    message.channel.send.assert_awaited_once_with("Saved that idea.")
    assert notes.search(1, tags=["idea"])[0]["content"] == "build a treehouse"

def test_recall_ideas_empty():
    message = _message()
    asyncio.run(notes_plugin.handle("recall_ideas", message, None, 1, "", [], None, None))
    message.channel.send.assert_awaited_once_with("No ideas saved.")

def test_recall_ideas_with_items():
    notes.save(1, "build a treehouse", ["idea"])
    message = _message()
    asyncio.run(notes_plugin.handle("recall_ideas", message, None, 1, "", [], None, None))
    message.channel.send.assert_awaited_once_with("- build a treehouse")

def test_recall_ideas_multiple_items_sends_one_message_each():
    notes.save(1, "build a treehouse", ["idea"])
    notes.save(1, "learn to bake bread", ["idea"])
    message = _message()
    asyncio.run(notes_plugin.handle("recall_ideas", message, None, 1, "", [], None, None))
    assert message.channel.send.await_count == 2
    sent_texts = [c.args[0] for c in message.channel.send.await_args_list]
    assert "- build a treehouse" in sent_texts
    assert "- learn to bake bread" in sent_texts

def test_discard_idea_empty_content_guarded():
    message = _message()
    asyncio.run(notes_plugin.handle("discard_idea", message, None, 1, "", [], None, None))
    message.channel.send.assert_awaited_once_with("Which idea do you want to discard?")

def test_discard_idea_no_match():
    message = _message()
    asyncio.run(notes_plugin.handle("discard_idea", message, None, 1, "treehouse", [], None, None))
    message.channel.send.assert_awaited_once_with("No idea found matching that.")

def test_discard_idea_single_match():
    notes.save(1, "build a treehouse", ["idea"])
    message = _message()
    asyncio.run(notes_plugin.handle("discard_idea", message, None, 1, "treehouse", [], None, None))
    message.channel.send.assert_awaited_once_with("Discarded: build a treehouse.")
    assert notes.search(1, tags=["idea"]) == []

def test_discard_idea_multiple_matches():
    notes.save(1, "treehouse plan one", ["idea"])
    notes.save(1, "treehouse plan two", ["idea"])
    message = _message()
    asyncio.run(notes_plugin.handle("discard_idea", message, None, 1, "treehouse", [], None, None))
    sent_text = message.channel.send.call_args[0][0]
    assert "Found more than one match" in sent_text

def test_expand_idea_empty_content_guarded():
    message = _message()
    asyncio.run(notes_plugin.handle("expand_idea", message, None, 1, "", [], None, None))
    message.channel.send.assert_awaited_once_with("Which idea do you want to expand on?")

def test_expand_idea_single_match():
    notes.save(1, "build a treehouse", ["idea"])
    message = _message()
    with patch.object(brain, "expand", return_value="Here's how..."):
        asyncio.run(notes_plugin.handle("expand_idea", message, None, 1, "treehouse", [], None, None))
    message.channel.send.assert_awaited_once_with("Here's how...")
```

- [ ] **Step 5: Replace `tests/test_shopping_plugin.py` in full**

Same treatment — every `.handle(...)` call gains a trailing `None`:

```python
import os, asyncio, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("HUSBAND_ID", "2")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import MagicMock, AsyncMock, patch
from wren import shopping
from wren import shopping_plugin
from wren import discord_utils

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(shopping, "DB_PATH", str(tmp_path / "test.db"))
    shopping.init_db()

def _message():
    message = MagicMock()
    message.channel.send = AsyncMock()
    return message

def test_add_shopping_item_new():
    message = _message()
    asyncio.run(shopping_plugin.handle("add_shopping_item", message, None, 1, "potatoes", [], None, None))
    message.channel.send.assert_awaited_once_with("Added potatoes.")

def test_add_shopping_item_dedup():
    shopping.add("potatoes", "owner")
    message = _message()
    asyncio.run(shopping_plugin.handle("add_shopping_item", message, None, 1, "potatoes", [], None, None))
    message.channel.send.assert_awaited_once_with("Already on the list.")

def test_remove_shopping_item_found():
    shopping.add("milk", "owner")
    message = _message()
    asyncio.run(shopping_plugin.handle("remove_shopping_item", message, None, 1, "milk", [], None, None))
    message.channel.send.assert_awaited_once_with("Got it, removed milk.")

def test_remove_shopping_item_not_found():
    message = _message()
    asyncio.run(shopping_plugin.handle("remove_shopping_item", message, None, 1, "milk", [], None, None))
    message.channel.send.assert_awaited_once_with("milk wasn't on the list.")

def test_recall_shopping_empty():
    message = _message()
    asyncio.run(shopping_plugin.handle("recall_shopping", message, None, 1, "", [], None, None))
    message.channel.send.assert_awaited_once_with("Shopping list is empty.")

def test_recall_shopping_with_items():
    shopping.add("potatoes", "owner")
    message = _message()
    asyncio.run(shopping_plugin.handle("recall_shopping", message, None, 1, "", [], None, None))
    message.channel.send.assert_awaited_once_with("potatoes")

def test_send_shopping_list_unknown_contact():
    message = _message()
    asyncio.run(shopping_plugin.handle("send_shopping_list", message, None, 1, "", [], "stranger", None))
    message.channel.send.assert_awaited_once_with("I don't know how to reach them.")

def test_send_shopping_list_empty_list():
    message = _message()
    asyncio.run(shopping_plugin.handle("send_shopping_list", message, None, 1, "", [], "husband", None))
    message.channel.send.assert_awaited_once_with("Nothing on the list to send.")

def test_send_shopping_list_success():
    shopping.add("potatoes", "owner")
    message = _message()
    with patch.object(discord_utils, "notify", new=AsyncMock(return_value=True)):
        asyncio.run(shopping_plugin.handle("send_shopping_list", message, None, 1, "", [], "husband", None))
    message.channel.send.assert_awaited_once_with("Sent to husband.")

def test_send_shopping_list_dm_failure():
    shopping.add("potatoes", "owner")
    message = _message()
    with patch.object(discord_utils, "notify", new=AsyncMock(return_value=False)):
        asyncio.run(shopping_plugin.handle("send_shopping_list", message, None, 1, "", [], "husband", None))
    message.channel.send.assert_awaited_once_with("Couldn't reach husband — their DMs may be closed.")
```

- [ ] **Step 6: Run the full suite**

Run: `pytest -q`
Expected: all tests pass — the signature change plus the trailing `None` added to every call site should be behavior-neutral. If any test fails with a `TypeError` about argument count, you missed a call site — find it (`grep -n "\.handle(" tests/test_notes_plugin.py tests/test_shopping_plugin.py`) and add the missing `None`.

- [ ] **Step 7: Commit**

```bash
git add wren/bot.py wren/notes_plugin.py wren/shopping_plugin.py tests/test_notes_plugin.py tests/test_shopping_plugin.py
git commit -m "refactor: thread a when field through the plugin dispatch contract"
```

---

### Task 5: `reminder_plugin.py` — reminder intents + background poller

**Files:**
- Create: `wren/reminder_plugin.py`
- Modify: `wren/config.py` (append `REMINDER_POLL_SECONDS`)
- Test: `tests/test_reminder_plugin.py`

**Interfaces:**
- Consumes: `reminders.save/pending/find_pending/cancel/due/mark_fired` (Task 3), `discord_utils.notify_id(client, user_id, text) -> bool` (Task 1), `config.REMINDER_POLL_SECONDS` (this task)
- Produces: `reminder_plugin.INTENTS`, `reminder_plugin.PROMPT_GUIDELINES`, `async def reminder_plugin.handle(intent, message, client, user_id, content, tags, person, when) -> None`, `async def reminder_plugin.start(client) -> None`

- [ ] **Step 1: Append to `wren/config.py`**

Add after the existing `EMAIL_WATCH` line:

```python
REMINDER_POLL_SECONDS = int(os.environ.get("REMINDER_POLL_SECONDS", "30"))
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_reminder_plugin.py`:

```python
import os, asyncio, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("HUSBAND_ID", "2")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, AsyncMock, patch
from wren import reminders
from wren import reminder_plugin
from wren import discord_utils

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(reminders, "DB_PATH", str(tmp_path / "test.db"))
    reminders.init_db()

def _message():
    message = MagicMock()
    message.channel.send = AsyncMock()
    return message

def _future_iso(seconds=120):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")

def test_set_reminder_valid():
    message = _message()
    when = _future_iso()
    asyncio.run(reminder_plugin.handle("set_reminder", message, None, 1, "check the oven", [], None, when))
    sent = message.channel.send.call_args[0][0]
    assert "Reminder set for" in sent
    assert len(reminders.pending(1)) == 1

def test_set_reminder_missing_content():
    message = _message()
    asyncio.run(reminder_plugin.handle("set_reminder", message, None, 1, "", [], None, _future_iso()))
    message.channel.send.assert_awaited_once_with("I couldn't figure out when — try again with a specific time.")
    assert reminders.pending(1) == []

def test_set_reminder_unparseable_when():
    message = _message()
    asyncio.run(reminder_plugin.handle("set_reminder", message, None, 1, "check the oven", [], None, "not-a-real-date"))
    message.channel.send.assert_awaited_once_with("I couldn't figure out when — try again with a specific time.")
    assert reminders.pending(1) == []

def test_set_reminder_past_time_rejected():
    message = _message()
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    asyncio.run(reminder_plugin.handle("set_reminder", message, None, 1, "check the oven", [], None, past))
    message.channel.send.assert_awaited_once_with("I couldn't figure out when — try again with a specific time.")
    assert reminders.pending(1) == []

def test_set_reminder_within_grace_window_accepted():
    message = _message()
    almost_now = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(timespec="seconds")
    asyncio.run(reminder_plugin.handle("set_reminder", message, None, 1, "check the oven", [], None, almost_now))
    assert len(reminders.pending(1)) == 1

def test_recall_reminders_empty():
    message = _message()
    asyncio.run(reminder_plugin.handle("recall_reminders", message, None, 1, "", [], None, None))
    message.channel.send.assert_awaited_once_with("No reminders set.")

def test_recall_reminders_lists_one_message_each():
    reminders.save(1, "check the oven", _future_iso(60))
    reminders.save(1, "call mom", _future_iso(120))
    message = _message()
    asyncio.run(reminder_plugin.handle("recall_reminders", message, None, 1, "", [], None, None))
    assert message.channel.send.await_count == 2

def test_cancel_reminder_empty_content_guarded():
    message = _message()
    asyncio.run(reminder_plugin.handle("cancel_reminder", message, None, 1, "", [], None, None))
    message.channel.send.assert_awaited_once_with("Which reminder do you want to cancel?")

def test_cancel_reminder_no_match():
    message = _message()
    asyncio.run(reminder_plugin.handle("cancel_reminder", message, None, 1, "oven", [], None, None))
    message.channel.send.assert_awaited_once_with("No reminder found matching that.")

def test_cancel_reminder_single_match():
    reminders.save(1, "check the oven", _future_iso())
    message = _message()
    asyncio.run(reminder_plugin.handle("cancel_reminder", message, None, 1, "oven", [], None, None))
    message.channel.send.assert_awaited_once_with("Cancelled: check the oven.")
    assert reminders.pending(1) == []

def test_cancel_reminder_multiple_matches():
    reminders.save(1, "check the oven at noon", _future_iso())
    reminders.save(1, "check the oven at night", _future_iso())
    message = _message()
    asyncio.run(reminder_plugin.handle("cancel_reminder", message, None, 1, "oven", [], None, None))
    sent = message.channel.send.call_args[0][0]
    assert "Found more than one match" in sent

def test_start_fires_due_reminder_and_marks_fired():
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(timespec="seconds")
    reminders.save(1, "check the oven", past)

    async def run_one_iteration():
        with patch.object(discord_utils, "notify_id", new=AsyncMock(return_value=True)) as mock_notify, \
             patch("wren.reminder_plugin.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
            try:
                await reminder_plugin.start(None)
            except asyncio.CancelledError:
                pass
        return mock_notify

    mock_notify = asyncio.run(run_one_iteration())
    mock_notify.assert_awaited_once()
    assert reminders.pending(1) == []

def test_start_does_not_fire_future_reminder():
    reminders.save(1, "check the oven", _future_iso(3600))

    async def run_one_iteration():
        with patch.object(discord_utils, "notify_id", new=AsyncMock(return_value=True)) as mock_notify, \
             patch("wren.reminder_plugin.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
            try:
                await reminder_plugin.start(None)
            except asyncio.CancelledError:
                pass
        return mock_notify

    mock_notify = asyncio.run(run_one_iteration())
    mock_notify.assert_not_called()
    assert len(reminders.pending(1)) == 1
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_reminder_plugin.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'wren.reminder_plugin'`

- [ ] **Step 4: Write `wren/reminder_plugin.py`**

```python
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from . import config
from . import reminders
from . import discord_utils

INTENTS = ["set_reminder", "recall_reminders", "cancel_reminder"]

PROMPT_GUIDELINES = """- set_reminder: user wants to be reminded of something at a specific time; content is what to remind them of, and you must compute "when" as an absolute ISO 8601 UTC datetime (e.g. 2026-07-12T21:00:00+00:00) based on the current date/time and the relative or absolute time they gave (e.g. "in 20 minutes", "at 6pm", "tomorrow morning")
- recall_reminders: user wants to see their upcoming reminders
- cancel_reminder: user wants to cancel a previously set reminder; content is a short phrase identifying which one, not the full reminder text"""

_GRACE = timedelta(seconds=30)

def _parse_when(when):
    if not when:
        return None
    try:
        parsed = datetime.fromisoformat(when)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed < datetime.now(timezone.utc) - _GRACE:
        return None
    return parsed

async def handle(intent, message, client, user_id, content, tags, person, when):
    if intent == "set_reminder":
        parsed = _parse_when(when)
        if not content.strip() or parsed is None:
            await message.channel.send("I couldn't figure out when — try again with a specific time.")
        else:
            fire_at = parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
            reminders.save(user_id, content, fire_at)
            display = fire_at[:16].replace("T", " ")
            await message.channel.send(f"Reminder set for {display} UTC.")

    elif intent == "recall_reminders":
        items = reminders.pending(user_id)
        if not items:
            await message.channel.send("No reminders set.")
        else:
            for r in items:
                display = r["fire_at"][:16].replace("T", " ")
                await message.channel.send(f"[{display} UTC] {r['content']}")

    elif intent == "cancel_reminder":
        if not content.strip():
            await message.channel.send("Which reminder do you want to cancel?")
        else:
            matches = reminders.find_pending(user_id, content)
            if not matches:
                await message.channel.send("No reminder found matching that.")
            elif len(matches) > 1:
                listing = "\n".join(f"- {m['content']}" for m in matches)
                await message.channel.send(f"Found more than one match, be more specific.\n{listing}")
            else:
                reminders.cancel(matches[0]["id"])
                await message.channel.send(f"Cancelled: {matches[0]['content']}.")

async def start(client) -> None:
    while True:
        try:
            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for r in reminders.due(now_iso):
                ok = await discord_utils.notify_id(client, int(r["owner_id"]), f"⏰ Reminder: {r['content']}")
                if not ok:
                    logging.warning(f"Could not deliver reminder {r['id']} to {r['owner_id']}")
                reminders.mark_fired(r["id"])
        except Exception as e:
            logging.warning(f"reminder poll failed: {e}")
        await asyncio.sleep(config.REMINDER_POLL_SECONDS)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_reminder_plugin.py -v`
Expected: PASS (13 tests)

- [ ] **Step 6: Run the full suite**

Run: `pytest -q`
Expected: all tests pass

- [ ] **Step 7: Commit**

```bash
git add wren/reminder_plugin.py wren/config.py tests/test_reminder_plugin.py
git commit -m "feat: add reminder_plugin.py with set/recall/cancel intents and a poll loop"
```

---

### Task 6: Register `reminder_plugin` in `plugins.py`

**Files:**
- Modify: `wren/plugins.py:2-6` (imports and `PLUGINS` list)
- Test: `tests/test_plugins.py` (add cases)

**Interfaces:**
- Consumes: `reminder_plugin` (Task 5), the existing relaxed plugin contract in `plugins.py` (`getattr`-based `all_intents`/`all_guidelines`/`INTENT_HANDLERS`, non-blocking `start_all`)
- Produces: no new functions — `plugins.PLUGINS` now includes `reminder_plugin`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_plugins.py` (add `from wren import reminder_plugin` to the top imports):

```python
def test_reminder_plugin_registered():
    assert reminder_plugin in plugins.PLUGINS

def test_all_intents_includes_reminder_intents():
    intents = plugins.all_intents()
    for intent in reminder_plugin.INTENTS:
        assert intent in intents
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_plugins.py -v`
Expected: FAIL — `test_reminder_plugin_registered` fails with `AssertionError`

- [ ] **Step 3: Update `wren/plugins.py`**

Change:

```python
import asyncio
from . import notes_plugin
from . import shopping_plugin
from . import email_plugin

PLUGINS = [notes_plugin, shopping_plugin, email_plugin]
```

to:

```python
import asyncio
from . import notes_plugin
from . import shopping_plugin
from . import email_plugin
from . import reminder_plugin

PLUGINS = [notes_plugin, shopping_plugin, email_plugin, reminder_plugin]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_plugins.py -v`
Expected: PASS (all tests, including the 2 new ones)

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add wren/plugins.py tests/test_plugins.py
git commit -m "feat: register reminder_plugin in the plugin registry"
```

---

## Post-plan verification

- [ ] Run `pytest -q` — expect all tests (existing + new) passing.
- [ ] Manually confirm `.env.example` and `README.md` document `REMINDER_POLL_SECONDS` and the new reminder commands (`remind me...`, `what are my reminders`, `cancel the ... reminder`) — not part of any task above, add as a small doc update if missed.
- [ ] Manually confirm no module imports `plugins.py` from `brain.py` or `reminder_plugin.py` (grep for `import plugins`), preserving the import-cycle fix from the plugin-system work.
