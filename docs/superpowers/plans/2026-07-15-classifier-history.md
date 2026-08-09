# Conversation History for Intent Classification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `brain.detect_intent()` the same 10-message conversation history `brain.chat()` already receives, so intent classification can resolve context-dependent follow-ups instead of classifying each message in total isolation.

**Architecture:** `detect_intent()` gains an optional `history` parameter with the exact same shape and insertion point `chat()` already uses (system message, then history, then the current user message). In `bot.py`, the history fetch that currently lives only inside the `chat` fallback branch moves above intent classification so it runs once per message and is passed to `detect_intent()`; when the intent resolves to `chat`, the same history value is reused for `brain.chat()` instead of being fetched a second time.

**Tech Stack:** Python, pytest, `unittest.mock`, discord.py (`discord.Message`, `discord.DMChannel`).

## Global Constraints

- History window stays at 10 messages, matching `chat()`'s existing `message.channel.history(limit=10, before=message)` call — no new tunable size (per spec's "Out of scope").
- No persistence of history anywhere (DB, cache) — refetched from Discord each time, same as today.
- `detect_intent`'s classification behavior itself is not unit-tested (LLM-driven, mocked in tests, per existing project convention) — only the `messages` payload shape is tested.
- `bot.py` has no test coverage today (module-level `client.run()` makes it unimportable without a real Discord token) — this plan does not change that; the `bot.py` change is verified by manual code review, not a test.

---

### Task 1: `detect_intent` accepts and threads through `history`

**Files:**
- Modify: `wren/brain.py:92-113` (`detect_intent`)
- Test: `tests/test_brain.py`

**Interfaces:**
- Consumes: nothing new — `_complete(messages, temperature, max_tokens)` already exists and is unchanged.
- Produces: `brain.detect_intent(user_id: int, text: str, history: list[dict] | None = None) -> dict`. Later tasks (bot.py) call this with a `history` positional/keyword argument of the same `[{"role": ..., "content": ...}]` shape `chat()` already accepts.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_brain.py`, near the existing `test_register_plugins_included_in_prompt` test (which already shows the `captured["messages"]` pattern for `detect_intent`):

```python
def test_detect_intent_without_history_matches_original_shape():
    client = _client_returning(json.dumps({"intent": "chat", "content": "hi", "tags": [], "person": None}))
    with patch.object(brain, "_get_client", return_value=client):
        brain.detect_intent(1, "hello")
    messages = client.chat.completions.create.call_args.kwargs["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "hello"}

def test_detect_intent_with_history_inserts_between_system_and_final_user_message():
    client = _client_returning(json.dumps({"intent": "chat", "content": "follow-up", "tags": [], "person": None}))
    history = [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier reply"},
    ]
    with patch.object(brain, "_get_client", return_value=client):
        brain.detect_intent(1, "follow-up", history=history)
    messages = client.chat.completions.create.call_args.kwargs["messages"]
    assert len(messages) == 4
    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "earlier question"}
    assert messages[2] == {"role": "assistant", "content": "earlier reply"}
    assert messages[3] == {"role": "user", "content": "follow-up"}

def test_detect_intent_with_empty_history_list_matches_no_history_shape():
    client = _client_returning(json.dumps({"intent": "chat", "content": "hi", "tags": [], "person": None}))
    with patch.object(brain, "_get_client", return_value=client):
        brain.detect_intent(1, "hello", history=[])
    messages = client.chat.completions.create.call_args.kwargs["messages"]
    assert len(messages) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/winter/work/Wren && venv/bin/pytest tests/test_brain.py -k "detect_intent_without_history or detect_intent_with_history or detect_intent_with_empty_history" -v`
Expected: `test_detect_intent_with_history_inserts_between_system_and_final_user_message` FAILs with `TypeError: detect_intent() got an unexpected keyword argument 'history'` (the other two pass already, since they match current behavior — that's fine, they lock in the no-history shape before the change).

- [ ] **Step 3: Implement**

In `wren/brain.py`, replace the current `detect_intent` (lines 92-113):

```python
def detect_intent(user_id: int, text: str, history: list[dict] | None = None) -> dict:
    plugin_intent_enum = " | ".join(f'"{i}"' for i in _plugin_intents)
    system = _SYSTEM.format(
        date=_now(), contacts=_contacts(),
        plugin_intents=plugin_intent_enum, plugin_guidelines=_plugin_guidelines,
    )
    messages = [{"role": "system", "content": system}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": text})
    try:
        raw = _complete(messages, temperature=0.1, max_tokens=200)
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except Exception:
        return {"intent": "chat", "content": text, "tags": [], "person": None}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/winter/work/Wren && venv/bin/pytest tests/test_brain.py -v`
Expected: all PASS (the three new tests plus every pre-existing `test_detect_intent_*` test, which don't pass `history` and must still work unchanged).

- [ ] **Step 5: Commit**

```bash
git add wren/brain.py tests/test_brain.py
git commit -m "feat: detect_intent accepts optional conversation history"
```

---

### Task 2: `bot.py` fetches history once and shares it between classification and chat

**Files:**
- Modify: `wren/bot.py:119-177` (`on_message`)

**Interfaces:**
- Consumes: `brain.detect_intent(user_id, text, history)` and `brain.chat(text, history)` from Task 1 (both already accept `history: list[dict] | None`); `discord_utils.history_to_messages(raw_history, bot_user_id) -> list[dict]` (unchanged, existing function).
- Produces: nothing consumed by later tasks — this is the final integration point.

- [ ] **Step 1: Move the history fetch above classification and reuse it**

In `wren/bot.py`, the current code is:

```python
    await _react(message, "👀")

    try:
        result = brain.detect_intent(user_id, text)
        intent = result.get("intent", "chat")
```
```python
        else:  # chat
            try:
                raw_history = [m async for m in message.channel.history(limit=10, before=message)]
                history = discord_utils.history_to_messages(raw_history, client.user.id)
            except Exception as e:
                logging.warning(f"Could not fetch history: {e}")
                history = None
            reply = brain.chat(text, history)
            await message.channel.send(reply)
```

Replace both blocks. First, the top of the `try`:

```python
    await _react(message, "👀")

    try:
        try:
            raw_history = [m async for m in message.channel.history(limit=10, before=message)]
            history = discord_utils.history_to_messages(raw_history, client.user.id)
        except Exception as e:
            logging.warning(f"Could not fetch history: {e}")
            history = None

        result = brain.detect_intent(user_id, text, history)
        intent = result.get("intent", "chat")
```

Then the `chat` fallback branch at the bottom, which drops its own fetch and reuses `history`:

```python
        else:  # chat
            reply = brain.chat(text, history)
            await message.channel.send(reply)
```

- [ ] **Step 2: Verify by reading, not running**

`bot.py` has no test coverage (module-level `client.run()`, pre-existing — see Global Constraints). Verify correctness by re-reading the full modified `on_message` function top to bottom and confirming:
- `history` is defined before every branch that could use it (it now is, since the fetch moved above the `if intent in plugins.INTENT_HANDLERS:` line).
- The `try/except` around the history fetch is preserved exactly (same log message, same fallback to `None`).
- No other branch (`help`, `status`, `list_plugins`, `send_to_person`, plugin handlers) references `history` — only `detect_intent` and the `chat` fallback do, so this is a pure addition, not a behavior change for those branches.

Run: `cd /home/winter/work/Wren && venv/bin/python -c "import ast; ast.parse(open('wren/bot.py').read())"`
Expected: no output (syntax check passes).

- [ ] **Step 3: Run the full test suite**

Run: `cd /home/winter/work/Wren && venv/bin/pytest -v`
Expected: all PASS, same count as before this task (bot.py isn't imported by the test suite, so this task can't break existing tests — this just confirms Task 1's changes still hold).

- [ ] **Step 4: Commit**

```bash
git add wren/bot.py
git commit -m "feat: classify intent with conversation history, reuse for chat fallback"
```

## Out of scope

- Persisting history anywhere (DB, cache).
- Tuning the history window size.
- Filtering or summarizing history before it reaches the model.
- Adding test coverage for `bot.py`'s `on_message` dispatch (pre-existing gap, not introduced by this change).
