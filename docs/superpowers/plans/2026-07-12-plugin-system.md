# Plugin System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor `bot.py`'s 12-branch if/elif intent dispatch and `brain.py`'s hardcoded prompt into a plugin architecture — `notes_plugin.py` and `shopping_plugin.py` own their intents, a `plugins.py` registry composes them, and a `discord_utils.py` helper de-duplicates the whitelist/DM-send logic. Also adds the extension point (`start()` hook) future event-driven plugins (email, Node-RED, Grafana) will use.

**Architecture:** Six tasks, ordered so nothing depends on a file that doesn't exist yet: `discord_utils.py` (standalone) → `brain.py` registration support (standalone) → `notes_plugin.py` (needs `notes.py`+`brain.py`, both stable) → `shopping_plugin.py` (needs `shopping.py`+`discord_utils.py`) → `plugins.py` registry (needs both plugin modules) → `bot.py` wiring (needs everything).

**Tech Stack:** Python 3.11+, `discord.py`, `pytest`, stdlib `asyncio`/`unittest.mock` for testing async functions (no `pytest-asyncio` — running a coroutine via `asyncio.run(...)` inside a plain sync test function needs no new dependency). No new dependencies anywhere in this plan.

## Global Constraints

- A plugin module exposes `INTENTS: list[str]`, `PROMPT_GUIDELINES: str`, `async def handle(intent, message, client, user_id, content, tags, person) -> None`, and optionally `async def start(client) -> None`. No base class — duck-typed.
- `brain.py` never imports `plugins.py` (avoids a cycle through `notes_plugin.py` → `brain.py`). Plugin-contributed intents/guidelines reach `brain.py` via `brain.register_plugins(intents, guidelines)`, called once by `bot.py` at module level.
- `send_to_person` and `chat` remain hardcoded in `bot.py`/`brain.py` — they are not plugin-owned.
- Existing branch bodies (notes/ideas/shopping intents) move verbatim into plugin modules — no behavior change, this is a relocation, not a rewrite. This includes the empty-content guards added during the ideas-bucket work.
- `notes.py`/`shopping.py` storage modules are untouched.
- `discord_utils.notify()` replaces the duplicated whitelist-lookup/`fetch_user`/DM-exception-handling in both `send_to_person` (bot.py) and `send_shopping_list` (shopping_plugin.py).
- Still no `tests/test_bot.py` — `bot.py`'s module-level `client.run(...)` call still makes it unimportable without a real token; verify Task 6 by running the full suite plus a manual read-back.

---

### Task 1: `discord_utils.py` — shared DM-notify helper

**Files:**
- Create: `discord_utils.py`
- Test: `tests/test_discord_utils.py`

**Interfaces:**
- Produces: `async def notify(client: discord.Client, contact_name: str, text: str) -> bool` — looks up `contact_name` (case-insensitive) in `config.WHITELIST`; `False` immediately if unknown (no `fetch_user` attempt); on a known contact, `fetch_user` + DM `text`, returning `True` on success or `False` if `discord.NotFound`/`discord.Forbidden` is raised (logged via `logging.error`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_discord_utils.py`:

```python
import os, asyncio
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("HUSBAND_ID", "2")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import MagicMock, AsyncMock
import discord
import discord_utils

def test_notify_success():
    client = MagicMock()
    target_user = MagicMock()
    target_user.send = AsyncMock()
    client.fetch_user = AsyncMock(return_value=target_user)

    result = asyncio.run(discord_utils.notify(client, "owner", "hello"))

    assert result is True
    target_user.send.assert_awaited_once_with("hello")

def test_notify_unknown_contact_returns_false():
    client = MagicMock()
    client.fetch_user = AsyncMock()

    result = asyncio.run(discord_utils.notify(client, "stranger", "hello"))

    assert result is False
    client.fetch_user.assert_not_called()

def test_notify_dm_forbidden_returns_false():
    client = MagicMock()
    fake_response = MagicMock(status=403, reason="Forbidden")
    client.fetch_user = AsyncMock(side_effect=discord.Forbidden(fake_response, "Cannot send messages to this user"))

    result = asyncio.run(discord_utils.notify(client, "owner", "hello"))

    assert result is False

def test_notify_dm_not_found_returns_false():
    client = MagicMock()
    fake_response = MagicMock(status=404, reason="Not Found")
    client.fetch_user = AsyncMock(side_effect=discord.NotFound(fake_response, "Unknown user"))

    result = asyncio.run(discord_utils.notify(client, "husband", "hello"))

    assert result is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_discord_utils.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'discord_utils'`

- [ ] **Step 3: Write `discord_utils.py`**

```python
import logging
import discord
import config

async def notify(client: discord.Client, contact_name: str, text: str) -> bool:
    target_id = config.WHITELIST.get(contact_name.lower())
    if not target_id:
        return False
    try:
        target_user = await client.fetch_user(target_id)
        await target_user.send(text)
        return True
    except (discord.NotFound, discord.Forbidden) as e:
        logging.error(f"Could not DM {contact_name}: {e}")
        return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_discord_utils.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add discord_utils.py tests/test_discord_utils.py
git commit -m "feat: add discord_utils.notify() shared DM-send helper"
```

---

### Task 2: `brain.py` — plugin registration support

**Files:**
- Modify: `brain.py` (the `_SYSTEM` template, plus new module state and `register_plugins()`)
- Test: `tests/test_brain.py` (add one case)

**Interfaces:**
- Produces: `brain.register_plugins(intents: list[str], guidelines: str) -> None` — stores plugin-contributed data as module state, used by `detect_intent()`. No signature change to `detect_intent`/`recall`/`chat`/`expand`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_brain.py` (uses the existing `_mock_completion` helper already defined in that file):

```python
def test_register_plugins_included_in_prompt():
    brain.register_plugins(["custom_intent"], "- custom_intent: does a custom thing")
    captured = {}

    def fake_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        return _mock_completion(json.dumps({"intent": "chat", "content": "hi", "tags": [], "person": None}))

    client = MagicMock()
    client.chat.completions.create.side_effect = fake_create
    with patch.object(brain, "_get_client", return_value=client):
        brain.detect_intent(1, "hello")

    system_content = captured["messages"][0]["content"]
    assert "custom_intent" in system_content
    assert "does a custom thing" in system_content
    brain.register_plugins([], "")  # reset so later tests aren't affected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_brain.py::test_register_plugins_included_in_prompt -v`
Expected: FAIL with `AttributeError: module 'brain' has no attribute 'register_plugins'`

- [ ] **Step 3: Update `_SYSTEM` and add `register_plugins()` in `brain.py`**

Replace the entire `_SYSTEM` string with:

```python
_SYSTEM = """You are Wren, a private personal assistant. You are short, structured, and ready. No filler, no affirmations.

Today is {date}.

When classifying intent, respond ONLY with valid JSON matching this schema:
{{
  "intent": "send_to_person" | {plugin_intents} | "chat",
  "content": "<extracted note or message content>",
  "tags": ["<tag1>", "<tag2>"],
  "person": "<name from whitelist or null>"
}}

Known contacts: {contacts}

Guidelines:
- send_to_person: user wants to send a message or note to someone
{plugin_guidelines}
- chat: anything else (questions, casual conversation)
- tags: 1-3 lowercase single-word tags relevant to the content
- person: only set when the intent is about contacting or sending something to someone else, use the contact name as given
"""
```

Add module state and the registration function, right after `_contacts()`:

```python
_plugin_intents: list[str] = []
_plugin_guidelines: str = ""

def register_plugins(intents: list[str], guidelines: str) -> None:
    global _plugin_intents, _plugin_guidelines
    _plugin_intents = intents
    _plugin_guidelines = guidelines
```

Update `detect_intent`'s prompt formatting (the `system = _SYSTEM.format(...)` line) to:

```python
def detect_intent(user_id: int, text: str) -> dict:
    plugin_intent_enum = " | ".join(f'"{i}"' for i in _plugin_intents)
    system = _SYSTEM.format(
        date=_now(), contacts=_contacts(),
        plugin_intents=plugin_intent_enum, plugin_guidelines=_plugin_guidelines,
    )
    try:
        raw = _complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            temperature=0.1,
        )
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except Exception:
        return {"intent": "chat", "content": text, "tags": [], "person": None}
```

(Only the `system = ...` line and the function's first line change — the rest of `detect_intent`'s body is unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_brain.py -v`
Expected: PASS (all tests, including the new one). Note: with no `register_plugins()` call, `_plugin_intents`/`_plugin_guidelines` default to `[]`/`""`, so every pre-existing test (which mocks the LLM response and never inspects prompt content) continues to pass unaffected.

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add brain.py tests/test_brain.py
git commit -m "feat: add brain.register_plugins() for plugin-contributed prompt content"
```

---

### Task 3: `notes_plugin.py` — notes/ideas intent handling

**Files:**
- Create: `notes_plugin.py`
- Test: `tests/test_notes_plugin.py`

**Interfaces:**
- Consumes: `notes.save/search/find/delete` (existing, unchanged), `brain.recall(notes, query) -> str` / `brain.expand(idea_text) -> str` (existing, unchanged)
- Produces: `notes_plugin.INTENTS`, `notes_plugin.PROMPT_GUIDELINES`, `async def notes_plugin.handle(intent, message, client, user_id, content, tags, person) -> None` — the plugin contract from this plan's Global Constraints.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_notes_plugin.py`:

```python
import os, asyncio, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("HUSBAND_ID", "2")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import MagicMock, AsyncMock, patch
import notes
import brain
import notes_plugin

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
    asyncio.run(notes_plugin.handle("save_note", message, None, 1, "buy milk", ["grocery"], None))
    message.channel.send.assert_awaited_once_with("Saved.")
    assert notes.list_recent(1)[0]["content"] == "buy milk"

def test_recall_notes_no_matches():
    message = _message()
    asyncio.run(notes_plugin.handle("recall_notes", message, None, 1, "milk", [], None))
    message.channel.send.assert_awaited_once_with("No notes found.")

def test_recall_notes_with_matches():
    notes.save(1, "buy milk", ["grocery"])
    message = _message()
    with patch.object(brain, "recall", return_value="You need milk."):
        asyncio.run(notes_plugin.handle("recall_notes", message, None, 1, "what groceries", [], None))
    message.channel.send.assert_awaited_once_with("You need milk.")

def test_save_idea_empty_content_guarded():
    message = _message()
    asyncio.run(notes_plugin.handle("save_idea", message, None, 1, "  ", [], None))
    message.channel.send.assert_awaited_once_with("What idea should I save?")
    assert notes.search(1, tags=["idea"]) == []

def test_save_idea():
    message = _message()
    asyncio.run(notes_plugin.handle("save_idea", message, None, 1, "build a treehouse", [], None))
    message.channel.send.assert_awaited_once_with("Saved that idea.")
    assert notes.search(1, tags=["idea"])[0]["content"] == "build a treehouse"

def test_recall_ideas_empty():
    message = _message()
    asyncio.run(notes_plugin.handle("recall_ideas", message, None, 1, "", [], None))
    message.channel.send.assert_awaited_once_with("No ideas saved.")

def test_recall_ideas_with_items():
    notes.save(1, "build a treehouse", ["idea"])
    message = _message()
    asyncio.run(notes_plugin.handle("recall_ideas", message, None, 1, "", [], None))
    message.channel.send.assert_awaited_once_with("- build a treehouse")

def test_discard_idea_empty_content_guarded():
    message = _message()
    asyncio.run(notes_plugin.handle("discard_idea", message, None, 1, "", [], None))
    message.channel.send.assert_awaited_once_with("Which idea do you want to discard?")

def test_discard_idea_no_match():
    message = _message()
    asyncio.run(notes_plugin.handle("discard_idea", message, None, 1, "treehouse", [], None))
    message.channel.send.assert_awaited_once_with("No idea found matching that.")

def test_discard_idea_single_match():
    notes.save(1, "build a treehouse", ["idea"])
    message = _message()
    asyncio.run(notes_plugin.handle("discard_idea", message, None, 1, "treehouse", [], None))
    message.channel.send.assert_awaited_once_with("Discarded: build a treehouse.")
    assert notes.search(1, tags=["idea"]) == []

def test_discard_idea_multiple_matches():
    notes.save(1, "treehouse plan one", ["idea"])
    notes.save(1, "treehouse plan two", ["idea"])
    message = _message()
    asyncio.run(notes_plugin.handle("discard_idea", message, None, 1, "treehouse", [], None))
    sent_text = message.channel.send.call_args[0][0]
    assert "Found more than one match" in sent_text

def test_expand_idea_empty_content_guarded():
    message = _message()
    asyncio.run(notes_plugin.handle("expand_idea", message, None, 1, "", [], None))
    message.channel.send.assert_awaited_once_with("Which idea do you want to expand on?")

def test_expand_idea_single_match():
    notes.save(1, "build a treehouse", ["idea"])
    message = _message()
    with patch.object(brain, "expand", return_value="Here's how..."):
        asyncio.run(notes_plugin.handle("expand_idea", message, None, 1, "treehouse", [], None))
    message.channel.send.assert_awaited_once_with("Here's how...")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_notes_plugin.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'notes_plugin'`

- [ ] **Step 3: Write `notes_plugin.py`**

```python
import notes
import brain

INTENTS = ["save_note", "recall_notes", "save_idea", "recall_ideas", "discard_idea", "expand_idea"]

PROMPT_GUIDELINES = """- save_note: user is capturing something for later (grocery item, plan, reminder)
- recall_notes: user wants to retrieve or search past notes
- save_idea: user explicitly wants to remember/capture an idea to revisit or expand later (e.g. "remember this idea...", "idea:..."), distinct from save_note's reminders/grocery items/plans
- recall_ideas: user wants to see all their saved ideas
- discard_idea: user wants to delete a previously saved idea; content is a short phrase identifying which idea, not the full idea text
- expand_idea: user wants Wren to elaborate/brainstorm further on a previously saved idea; content is a short phrase identifying which idea, not the full idea text"""

async def handle(intent, message, client, user_id, content, tags, person):
    if intent == "save_note":
        notes.save(user_id, content, tags)
        await message.channel.send("Saved.")

    elif intent == "recall_notes":
        matches = notes.search(user_id, tags=tags if tags else None)
        if not matches:
            await message.channel.send("No notes found.")
        else:
            summary = brain.recall(matches, content)
            await message.channel.send(summary)

    elif intent == "save_idea":
        if not content.strip():
            await message.channel.send("What idea should I save?")
        else:
            notes.save(user_id, content, ["idea"])
            await message.channel.send("Saved that idea.")

    elif intent == "recall_ideas":
        ideas = notes.search(user_id, tags=["idea"])
        if not ideas:
            await message.channel.send("No ideas saved.")
        else:
            await message.channel.send("\n".join(f"- {i['content']}" for i in ideas))

    elif intent == "discard_idea":
        if not content.strip():
            await message.channel.send("Which idea do you want to discard?")
        else:
            matches = notes.find(user_id, content, tags=["idea"])
            if not matches:
                await message.channel.send("No idea found matching that.")
            elif len(matches) > 1:
                listing = "\n".join(f"- {m['content']}" for m in matches)
                await message.channel.send(f"Found more than one match, be more specific.\n{listing}")
            else:
                notes.delete(matches[0]["id"])
                await message.channel.send(f"Discarded: {matches[0]['content']}.")

    elif intent == "expand_idea":
        if not content.strip():
            await message.channel.send("Which idea do you want to expand on?")
        else:
            matches = notes.find(user_id, content, tags=["idea"])
            if not matches:
                await message.channel.send("No idea found matching that.")
            elif len(matches) > 1:
                listing = "\n".join(f"- {m['content']}" for m in matches)
                await message.channel.send(f"Found more than one match, be more specific.\n{listing}")
            else:
                expansion = brain.expand(matches[0]["content"])
                await message.channel.send(expansion)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_notes_plugin.py -v`
Expected: PASS (13 tests)

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add notes_plugin.py tests/test_notes_plugin.py
git commit -m "feat: extract notes/ideas intent handling into notes_plugin.py"
```

---

### Task 4: `shopping_plugin.py` — shopping intent handling

**Files:**
- Create: `shopping_plugin.py`
- Test: `tests/test_shopping_plugin.py`

**Interfaces:**
- Consumes: `shopping.add/remove/active_items/common_items` (existing, unchanged), `discord_utils.notify(client, contact_name, text) -> bool` (Task 1), `config.WHITELIST`/`config.ID_TO_NAME` (existing)
- Produces: `shopping_plugin.INTENTS`, `shopping_plugin.PROMPT_GUIDELINES`, `async def shopping_plugin.handle(intent, message, client, user_id, content, tags, person) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_shopping_plugin.py`:

```python
import os, asyncio, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("HUSBAND_ID", "2")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import MagicMock, AsyncMock, patch
import shopping
import shopping_plugin
import discord_utils

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
    asyncio.run(shopping_plugin.handle("add_shopping_item", message, None, 1, "potatoes", [], None))
    message.channel.send.assert_awaited_once_with("Added potatoes.")

def test_add_shopping_item_dedup():
    shopping.add("potatoes", "owner")
    message = _message()
    asyncio.run(shopping_plugin.handle("add_shopping_item", message, None, 1, "potatoes", [], None))
    message.channel.send.assert_awaited_once_with("Already on the list.")

def test_remove_shopping_item_found():
    shopping.add("milk", "owner")
    message = _message()
    asyncio.run(shopping_plugin.handle("remove_shopping_item", message, None, 1, "milk", [], None))
    message.channel.send.assert_awaited_once_with("Got it, removed milk.")

def test_remove_shopping_item_not_found():
    message = _message()
    asyncio.run(shopping_plugin.handle("remove_shopping_item", message, None, 1, "milk", [], None))
    message.channel.send.assert_awaited_once_with("milk wasn't on the list.")

def test_recall_shopping_empty():
    message = _message()
    asyncio.run(shopping_plugin.handle("recall_shopping", message, None, 1, "", [], None))
    message.channel.send.assert_awaited_once_with("Shopping list is empty.")

def test_recall_shopping_with_items():
    shopping.add("potatoes", "owner")
    message = _message()
    asyncio.run(shopping_plugin.handle("recall_shopping", message, None, 1, "", [], None))
    message.channel.send.assert_awaited_once_with("potatoes")

def test_send_shopping_list_unknown_contact():
    message = _message()
    asyncio.run(shopping_plugin.handle("send_shopping_list", message, None, 1, "", [], "stranger"))
    message.channel.send.assert_awaited_once_with("I don't know how to reach them.")

def test_send_shopping_list_empty_list():
    message = _message()
    asyncio.run(shopping_plugin.handle("send_shopping_list", message, None, 1, "", [], "husband"))
    message.channel.send.assert_awaited_once_with("Nothing on the list to send.")

def test_send_shopping_list_success():
    shopping.add("potatoes", "owner")
    message = _message()
    with patch.object(discord_utils, "notify", new=AsyncMock(return_value=True)):
        asyncio.run(shopping_plugin.handle("send_shopping_list", message, None, 1, "", [], "husband"))
    message.channel.send.assert_awaited_once_with("Sent to husband.")

def test_send_shopping_list_dm_failure():
    shopping.add("potatoes", "owner")
    message = _message()
    with patch.object(discord_utils, "notify", new=AsyncMock(return_value=False)):
        asyncio.run(shopping_plugin.handle("send_shopping_list", message, None, 1, "", [], "husband"))
    message.channel.send.assert_awaited_once_with("Couldn't reach husband — their DMs may be closed.")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_shopping_plugin.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shopping_plugin'`

- [ ] **Step 3: Write `shopping_plugin.py`**

```python
import config
import shopping
import discord_utils

INTENTS = ["add_shopping_item", "remove_shopping_item", "recall_shopping", "send_shopping_list"]

PROMPT_GUIDELINES = """- add_shopping_item: user wants to add an item to the shared shopping list
- remove_shopping_item: user got/bought/already has an item and wants it off the shopping list
- recall_shopping: user wants to see the current shopping list
- send_shopping_list: user wants to send the whole shopping list to someone"""

async def handle(intent, message, client, user_id, content, tags, person):
    if intent == "add_shopping_item":
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
        if target_name not in config.WHITELIST:
            await message.channel.send("I don't know how to reach them.")
            return
        active = shopping.active_items()
        if not active:
            await message.channel.send("Nothing on the list to send.")
            return
        list_text = ", ".join(i["original_text"] for i in active)
        ok = await discord_utils.notify(
            client, target_name,
            f"Shopping list from {config.ID_TO_NAME.get(user_id, 'someone')}: {list_text}",
        )
        if ok:
            await message.channel.send(f"Sent to {target_name}.")
        else:
            await message.channel.send(f"Couldn't reach {target_name} — their DMs may be closed.")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_shopping_plugin.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add shopping_plugin.py tests/test_shopping_plugin.py
git commit -m "feat: extract shopping intent handling into shopping_plugin.py"
```

---

### Task 5: `plugins.py` — plugin registry

**Files:**
- Create: `plugins.py`
- Test: `tests/test_plugins.py`

**Interfaces:**
- Consumes: `notes_plugin.INTENTS`/`PROMPT_GUIDELINES` (Task 3), `shopping_plugin.INTENTS`/`PROMPT_GUIDELINES` (Task 4)
- Produces: `plugins.PLUGINS: list[module]`, `plugins.INTENT_HANDLERS: dict[str, module]`, `plugins.all_intents() -> list[str]`, `plugins.all_guidelines() -> str`, `async def plugins.start_all(client) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_plugins.py`:

```python
import os, asyncio
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("HUSBAND_ID", "2")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

import plugins
import notes_plugin
import shopping_plugin

def test_all_intents_includes_both_plugins():
    intents = plugins.all_intents()
    for intent in notes_plugin.INTENTS + shopping_plugin.INTENTS:
        assert intent in intents

def test_intent_handlers_maps_to_correct_plugin():
    assert plugins.INTENT_HANDLERS["save_note"] is notes_plugin
    assert plugins.INTENT_HANDLERS["add_shopping_item"] is shopping_plugin

def test_all_guidelines_includes_both_plugins_text():
    guidelines = plugins.all_guidelines()
    assert "save_note" in guidelines
    assert "add_shopping_item" in guidelines

def test_start_all_noop_when_no_plugin_defines_start():
    # neither notes_plugin nor shopping_plugin defines start() yet
    asyncio.run(plugins.start_all(None))  # should not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_plugins.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'plugins'`

- [ ] **Step 3: Write `plugins.py`**

```python
import notes_plugin
import shopping_plugin

PLUGINS = [notes_plugin, shopping_plugin]

INTENT_HANDLERS = {intent: plugin for plugin in PLUGINS for intent in plugin.INTENTS}

def all_intents() -> list[str]:
    return [intent for plugin in PLUGINS for intent in plugin.INTENTS]

def all_guidelines() -> str:
    return "\n".join(plugin.PROMPT_GUIDELINES for plugin in PLUGINS)

async def start_all(client) -> None:
    for plugin in PLUGINS:
        if hasattr(plugin, "start"):
            await plugin.start(client)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_plugins.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add plugins.py tests/test_plugins.py
git commit -m "feat: add plugins.py registry composing notes_plugin and shopping_plugin"
```

---

### Task 6: `bot.py` — wire the plugin registry into dispatch

**Files:**
- Modify: `bot.py` (imports, module-level `register_plugins()` call, `on_ready`, `on_message`)

**Interfaces:**
- Consumes: `plugins.INTENT_HANDLERS`, `plugins.all_intents()`, `plugins.all_guidelines()`, `plugins.start_all(client)` (Task 5), `brain.register_plugins(intents, guidelines)` (Task 2), `discord_utils.notify(client, contact_name, text)` (Task 1)
- Produces: no new functions — this is the final integration point.

There is no automated test for this task — same pre-existing condition as prior `bot.py` work (module-level `client.run(...)` makes it unimportable without a real token). Verification is: run the full suite, then a careful manual read-back.

- [ ] **Step 1: Replace `bot.py` in full**

Read the current `bot.py` first to confirm you're replacing the whole file (it's short, ~172 lines). Replace its entire contents with:

```python
import logging
import discord
import config
import brain
import notes
import shopping
import plugins
import discord_utils

logging.basicConfig(level=logging.INFO)

brain.register_plugins(plugins.all_intents(), plugins.all_guidelines())

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    notes.init_db()
    shopping.init_db()
    await plugins.start_all(client)
    logging.info(f"Wren online as {client.user}")

@client.event
async def on_message(message: discord.Message):
    # ignore own messages and non-DMs
    if message.author == client.user:
        return
    if not isinstance(message.channel, discord.DMChannel):
        return

    user_id = message.author.id

    # whitelist gate
    if user_id not in config.ID_TO_NAME:
        return

    text = message.content.strip()
    if not text:
        return

    try:
        result = brain.detect_intent(user_id, text)
        intent = result.get("intent", "chat")
        content = result.get("content", text)
        tags = result.get("tags", [])
        person = result.get("person")

        if intent in plugins.INTENT_HANDLERS:
            await plugins.INTENT_HANDLERS[intent].handle(
                intent, message, client, user_id, content, tags, person
            )

        elif intent == "send_to_person":
            target_name = (person or "").lower()
            if target_name not in config.WHITELIST:
                await message.channel.send("I don't know how to reach them.")
                return
            notes.save(user_id, content, tags)
            ok = await discord_utils.notify(
                client, target_name,
                f"From {config.ID_TO_NAME.get(user_id, 'someone')}: {content}",
            )
            if ok:
                await message.channel.send(f"Sent to {target_name}.")
            else:
                await message.channel.send(f"Couldn't reach {target_name} — their DMs may be closed.")

        else:  # chat
            reply = brain.chat(text)
            await message.channel.send(reply)
    except Exception as e:
        logging.error(f"Error handling message from {user_id}: {e}")
        await message.channel.send("Something went wrong, try again.")

client.run(config.DISCORD_TOKEN)
```

- [ ] **Step 2: Run the full test suite**

Run: `pytest -q`
Expected: all tests pass

- [ ] **Step 3: Manual read-back verification**

Re-read the new `bot.py` in full and confirm:
- `save_note`/`recall_notes`/`add_shopping_item`/`remove_shopping_item`/`recall_shopping`/`send_shopping_list`/`save_idea`/`recall_ideas`/`discard_idea`/`expand_idea` are no longer hardcoded branches — they're all reached via `plugins.INTENT_HANDLERS`.
- `send_to_person` still works exactly as before, just via `discord_utils.notify()` instead of inline `fetch_user`/DM/except code.
- `chat` is unchanged.
- `on_ready` still calls `notes.init_db()`/`shopping.init_db()`, plus the new `plugins.start_all(client)`.
- The outer `try/except Exception` in `on_message` still wraps everything, unchanged.

- [ ] **Step 4: Commit**

```bash
git add bot.py
git commit -m "refactor: wire plugin registry into bot.py dispatch"
```

---

## Post-plan verification

- [ ] Run `pytest -q` — expect all tests (existing + new) passing.
- [ ] Manually confirm `bot.py`'s dispatch reaches every intent via `plugins.INTENT_HANDLERS` or the two remaining core branches, by reading the file once more, since there is no automated test exercising `on_message` directly.
- [ ] Confirm no module imports `plugins.py` from `brain.py` (grep for `import plugins` — it should appear only in `bot.py` and `tests/test_plugins.py`), preserving the import-cycle fix this plan relies on.
