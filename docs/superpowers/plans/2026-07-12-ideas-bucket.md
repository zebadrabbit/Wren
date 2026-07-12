# Ideas Bucket Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wren lets a user save a free-form idea ("remember this idea: ..."), recall all their saved ideas, discard one by a matching phrase, or have Wren elaborate on one — reusing the existing per-owner `notes.py` storage with an `"idea"` tag, no new table.

**Architecture:** `notes.py` gains `delete()` and `find()`. `brain.py`'s intent-detection prompt grows four new intent values and gains one new function, `expand()`. `bot.py`'s `on_message` dispatch grows four new branches.

**Tech Stack:** Python 3.11+, `sqlite3` (stdlib), `pytest`. No new dependencies.

## Global Constraints

- Ideas are `notes` rows tagged `["idea"]`, forced by `bot.py` at save time — not a new table, not left to the LLM's own tag guess.
- Discard is a hard delete (`DELETE FROM notes`), no soft-delete/status column.
- Targeting a specific idea for discard/expand is case-insensitive substring match on `content`, scoped by owner + `"idea"` tag — never fuzzy/LLM-based matching.
- Ambiguous targeting (0 or 2+ matches) must not guess: 0 matches → "No idea found matching that."; 2+ matches → list them and ask the user to be more specific.
- `expand_idea` is a one-off LLM reply — nothing is written back to the matched note.
- `notes.save`/`notes.search`/`notes.list_recent`, `brain.detect_intent`/`recall`/`chat` keep their existing signatures — this plan only adds new functions/intents, it doesn't change existing ones.
- No `tests/test_bot.py` — same pre-existing condition as prior work (`bot.py`'s module-level `client.run(...)` makes it unimportable without a real token).

---

### Task 1: `notes.py` — `delete()` and `find()`

**Files:**
- Modify: `notes.py` (add two functions after `list_recent`)
- Test: `tests/test_notes.py` (add cases)

**Interfaces:**
- Produces:
  - `notes.delete(note_id: int) -> bool` — `True` if a row was removed, `False` if `note_id` didn't match any row.
  - `notes.find(owner_id: int, substring: str, tags: list[str] | None = None) -> list[dict]` — same owner+tag scoping as `search()`, further filtered to rows whose `content` contains `substring` case-insensitively.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_notes.py` (the file already has a `tmp_db` autouse fixture and `import notes` at the top — these tests use both, following the existing pattern in that file):

```python
def test_delete_existing_note():
    note_id = notes.save(1, "buy milk", ["grocery"])
    assert notes.delete(note_id) is True
    assert notes.list_recent(1) == []

def test_delete_nonexistent_returns_false():
    assert notes.delete(9999) is False

def test_find_matches_substring_case_insensitive():
    notes.save(1, "Build a treehouse for the kids", ["idea"])
    notes.save(1, "Learn to bake bread", ["idea"])
    results = notes.find(1, "TREEHOUSE", tags=["idea"])
    assert len(results) == 1
    assert "treehouse" in results[0]["content"].lower()

def test_find_no_match_returns_empty():
    notes.save(1, "Build a treehouse", ["idea"])
    results = notes.find(1, "spaceship", tags=["idea"])
    assert results == []

def test_find_multiple_matches():
    notes.save(1, "treehouse idea one", ["idea"])
    notes.save(1, "treehouse idea two", ["idea"])
    results = notes.find(1, "treehouse", tags=["idea"])
    assert len(results) == 2

def test_find_respects_owner_scope():
    notes.save(1, "treehouse for me", ["idea"])
    notes.save(2, "treehouse for them", ["idea"])
    results = notes.find(1, "treehouse", tags=["idea"])
    assert len(results) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_notes.py -v`
Expected: FAIL with `AttributeError: module 'notes' has no attribute 'delete'` (and similarly for `find`)

- [ ] **Step 3: Add `delete()` and `find()` to `notes.py`**

Append after the existing `list_recent` function:

```python
def delete(note_id: int) -> bool:
    with _conn() as con:
        cur = con.execute("DELETE FROM notes WHERE id=?", (note_id,))
        return cur.rowcount > 0

def find(owner_id: int, substring: str, tags: list[str] | None = None) -> list[dict]:
    results = search(owner_id, tags=tags)
    needle = substring.lower()
    return [r for r in results if needle in r["content"].lower()]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_notes.py -v`
Expected: PASS (10 tests — 5 existing + 5 new)

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add notes.py tests/test_notes.py
git commit -m "feat: add delete() and find() to notes.py for idea targeting"
```

---

### Task 2: `brain.py` — idea intents and `expand()`

**Files:**
- Modify: `brain.py:11-32` (`_SYSTEM` prompt: intent enum line and guidelines), append a new `expand()` function after `chat()`
- Test: `tests/test_brain.py` (add cases)

**Interfaces:**
- Consumes: `brain._complete(messages, temperature) -> str` (existing, from prior work)
- Produces: `brain.expand(idea_text: str) -> str` — one-off LLM elaboration, same pattern as `chat()`/`recall()`. `detect_intent`'s possible `result["intent"]` values grow to include `"save_idea"`, `"recall_ideas"`, `"discard_idea"`, `"expand_idea"`. No signature changes to any existing function.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_brain.py` (uses the existing `_client_returning` helper already defined in that file):

```python
def test_detect_intent_save_idea():
    payload = json.dumps({
        "intent": "save_idea",
        "content": "build a treehouse",
        "tags": [],
        "person": None
    })
    with patch.object(brain, "_get_client", return_value=_client_returning(payload)):
        result = brain.detect_intent(1, "remember this idea: build a treehouse")
    assert result["intent"] == "save_idea"
    assert result["content"] == "build a treehouse"

def test_detect_intent_recall_ideas():
    payload = json.dumps({
        "intent": "recall_ideas",
        "content": "",
        "tags": [],
        "person": None
    })
    with patch.object(brain, "_get_client", return_value=_client_returning(payload)):
        result = brain.detect_intent(1, "what ideas have I saved")
    assert result["intent"] == "recall_ideas"

def test_detect_intent_discard_idea():
    payload = json.dumps({
        "intent": "discard_idea",
        "content": "treehouse",
        "tags": [],
        "person": None
    })
    with patch.object(brain, "_get_client", return_value=_client_returning(payload)):
        result = brain.detect_intent(1, "discard the treehouse idea")
    assert result["intent"] == "discard_idea"
    assert result["content"] == "treehouse"

def test_detect_intent_expand_idea():
    payload = json.dumps({
        "intent": "expand_idea",
        "content": "treehouse",
        "tags": [],
        "person": None
    })
    with patch.object(brain, "_get_client", return_value=_client_returning(payload)):
        result = brain.detect_intent(1, "expand on the treehouse idea")
    assert result["intent"] == "expand_idea"
    assert result["content"] == "treehouse"

def test_expand_returns_string():
    with patch.object(brain, "_get_client", return_value=_client_returning("Here's how to build it...")):
        result = brain.expand("build a treehouse")
    assert isinstance(result, str)
    assert len(result) > 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_brain.py -v`
Expected: the 4 `detect_intent` tests may already pass (as with the shopping-list work, `detect_intent` doesn't validate `intent` against an enum) — report honestly what you observe, same as before. `test_expand_returns_string` WILL fail with `AttributeError: module 'brain' has no attribute 'expand'`.

- [ ] **Step 3: Update `_SYSTEM` in `brain.py`**

Replace lines 11-32 (from `When classifying intent...` through the last guideline bullet) with:

```python
When classifying intent, respond ONLY with valid JSON matching this schema:
{{
  "intent": "save_note" | "recall_notes" | "send_to_person" | "add_shopping_item" | "remove_shopping_item" | "recall_shopping" | "send_shopping_list" | "save_idea" | "recall_ideas" | "discard_idea" | "expand_idea" | "chat",
  "content": "<extracted note or message content>",
  "tags": ["<tag1>", "<tag2>"],
  "person": "<name from whitelist or null>"
}}

Known contacts: {contacts}

Guidelines:
- save_note: user is capturing something for later (grocery item, plan, reminder)
- recall_notes: user wants to retrieve or search past notes
- send_to_person: user wants to send a message or note to someone
- add_shopping_item: user wants to add an item to the shared shopping list
- remove_shopping_item: user got/bought/already has an item and wants it off the shopping list
- recall_shopping: user wants to see the current shopping list
- send_shopping_list: user wants to send the whole shopping list to someone
- save_idea: user explicitly wants to remember/capture an idea to revisit or expand later (e.g. "remember this idea...", "idea:..."), distinct from save_note's reminders/grocery items/plans
- recall_ideas: user wants to see all their saved ideas
- discard_idea: user wants to delete a previously saved idea; content is a short phrase identifying which idea, not the full idea text
- expand_idea: user wants Wren to elaborate/brainstorm further on a previously saved idea; content is a short phrase identifying which idea, not the full idea text
- chat: anything else (questions, casual conversation)
- tags: 1-3 lowercase single-word tags relevant to the content
- person: only set for send_to_person and send_shopping_list intents, use the contact name as given
```

(Note: `save_note`'s guideline bullet drops "idea" from its example list — ideas now have their own dedicated intent, `save_idea` — this is a small wording fix as part of the same edit, not a separate change.)

- [ ] **Step 4: Add `expand()` to `brain.py`**

Append after the existing `chat()` function:

```python
def expand(idea_text: str) -> str:
    return _complete(
        [
            {"role": "system", "content": "You are Wren. Short, structured, ready. No filler. Elaborate on the user's idea with concrete next steps or angles they might not have considered."},
            {"role": "user", "content": f"Idea: {idea_text}\n\nExpand on this."},
        ],
        temperature=0.5,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_brain.py -v`
Expected: PASS (all tests, including the 5 new ones)

- [ ] **Step 6: Run the full suite**

Run: `pytest -q`
Expected: all tests pass

- [ ] **Step 7: Commit**

```bash
git add brain.py tests/test_brain.py
git commit -m "feat: add idea intents and expand() to brain.py"
```

---

### Task 3: `bot.py` — dispatch idea intents

**Files:**
- Modify: `bot.py`'s `on_message` (4 new `elif` branches, inserted between the existing `send_shopping_list` branch and the final `else: # chat`)

**Interfaces:**
- Consumes: `notes.save(owner_id, content, tags) -> int`, `notes.search(owner_id, tags=None) -> list[dict]`, `notes.find(owner_id, substring, tags=None) -> list[dict]`, `notes.delete(note_id) -> bool` (Task 1), `brain.expand(idea_text) -> str` (Task 2). Intent values `save_idea`/`recall_ideas`/`discard_idea`/`expand_idea` (Task 2).
- Produces: no new functions — extends `on_message`'s existing if/elif chain.

There is no automated test for this task — same pre-existing condition as the shopping-list dispatch task (`bot.py`'s module-level `client.run(...)` call). Verification is: run the full suite to confirm nothing else broke, then a manual read-back of the edited file.

- [ ] **Step 1: Add the four dispatch branches**

In `on_message`, insert these branches immediately after the existing `elif intent == "send_shopping_list":` block (which ends with the `except (discord.NotFound, discord.Forbidden) as e:` handler) and before `else:  # chat`:

```python
        elif intent == "save_idea":
            notes.save(user_id, content, ["idea"])
            await message.channel.send("Saved that idea.")

        elif intent == "recall_ideas":
            ideas = notes.search(user_id, tags=["idea"])
            if not ideas:
                await message.channel.send("No ideas saved.")
            else:
                await message.channel.send("\n".join(f"- {i['content']}" for i in ideas))

        elif intent == "discard_idea":
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

- [ ] **Step 2: Run the full test suite**

Run: `pytest -q`
Expected: all tests pass (this task adds no new automated tests — it's verifying the rest of the suite still passes after editing `bot.py`)

- [ ] **Step 3: Manual read-back verification**

Re-read the modified `bot.py` in full and confirm: all four new `elif` branches are present, correctly indented at the same level as the existing `if intent == "save_note":` chain, placed after `send_shopping_list` and before `else:  # chat`; the existing branches are unchanged; no import changes were needed (this task only uses `notes` and `brain`, both already imported).

- [ ] **Step 4: Commit**

```bash
git add bot.py
git commit -m "feat: dispatch idea-bucket intents in Discord message handler"
```

---

## Post-plan verification

- [ ] Run `pytest -q` — expect all tests (existing + new) passing.
- [ ] Manually confirm `bot.py`'s four new branches are reachable (correct indentation/placement) by reading the file once more, since there is no automated test exercising `on_message` directly.
