# Conversational Chat History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `brain.chat()` (Wren's open-ended fallback reply) gains recent Discord conversation history as context, using the `READ_MESSAGE_HISTORY` OAuth permission, so follow-ups like "what did you mean by that" work naturally.

**Architecture:** A new pure function in `wren/discord_utils.py` converts a list of Discord messages into an LLM-ready `role`/`content` list. `brain.chat()` gains an optional `history` parameter that's spliced into the messages array sent to the LLM. `bot.py`'s existing `chat` dispatch branch fetches the last 10 messages via `channel.history()` before calling `chat()`, degrading to no history on any fetch error.

**Tech Stack:** Python 3.11+, `discord.py` (already a dependency), `pytest`. No new dependencies.

## Global Constraints

- Only `brain.chat()` gets history — `brain.detect_intent()` is untouched, so this cannot change how a message is routed.
- History is fetched live from Discord (`channel.history()`) on every chat call — no new storage, no persistence across restarts.
- History window is a hardcoded `limit=10` (last 10 messages before the triggering one).
- A history-fetch failure must degrade to a no-history `chat()` call, never break the reply.
- No new dependencies.

---

### Task 1: `discord_utils.history_to_messages` — Discord messages to LLM format

**Files:**
- Modify: `wren/discord_utils.py`
- Modify: `tests/test_discord_utils.py`

**Interfaces:**
- Produces: `discord_utils.history_to_messages(messages: list[discord.Message], bot_user_id: int) -> list[dict]` — each dict is `{"role": "user" | "assistant", "content": str}`. Input order is assumed newest-first (matching Discord's `channel.history()` default); output is chronological (oldest-first). Messages with empty/whitespace-only `content` are skipped.

- [ ] **Step 1: Write the failing tests**

In `tests/test_discord_utils.py`, add these test functions at the end of the file:

```python
def _fake_message(author_id: int, content: str):
    msg = MagicMock()
    msg.author.id = author_id
    msg.content = content
    return msg

def test_history_to_messages_empty_list():
    assert discord_utils.history_to_messages([], bot_user_id=99) == []

def test_history_to_messages_reverses_to_chronological_order():
    # channel.history() yields newest-first; input here is [newest, ..., oldest]
    newest = _fake_message(1, "second thing I said")
    oldest = _fake_message(1, "first thing I said")
    result = discord_utils.history_to_messages([newest, oldest], bot_user_id=99)
    assert result == [
        {"role": "user", "content": "first thing I said"},
        {"role": "user", "content": "second thing I said"},
    ]

def test_history_to_messages_assigns_assistant_role_to_bot_author():
    bot_msg = _fake_message(99, "Wren's reply")
    user_msg = _fake_message(1, "user's message")
    result = discord_utils.history_to_messages([user_msg, bot_msg], bot_user_id=99)
    assert result == [
        {"role": "assistant", "content": "Wren's reply"},
        {"role": "user", "content": "user's message"},
    ]

def test_history_to_messages_skips_empty_content():
    blank = _fake_message(1, "   ")
    real = _fake_message(1, "hello")
    result = discord_utils.history_to_messages([real, blank], bot_user_id=99)
    assert result == [{"role": "user", "content": "hello"}]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_discord_utils.py -k history_to_messages -v`
Expected: FAIL (`AttributeError: module 'wren.discord_utils' has no attribute 'history_to_messages'`)

- [ ] **Step 3: Implement `history_to_messages`**

In `wren/discord_utils.py`, add this function (anywhere in the module, e.g. after the imports and before `notify_id`):

```python
def history_to_messages(messages: list[discord.Message], bot_user_id: int) -> list[dict]:
    result = []
    for m in messages:
        if not m.content.strip():
            continue
        role = "assistant" if m.author.id == bot_user_id else "user"
        result.append({"role": role, "content": m.content})
    return list(reversed(result))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_discord_utils.py -v`
Expected: PASS (all tests in the file, including the 4 new ones)

- [ ] **Step 5: Commit**

```bash
git add wren/discord_utils.py tests/test_discord_utils.py
git commit -m "feat: add history_to_messages for converting Discord messages to LLM format"
```

---

### Task 2: `brain.chat()` accepts optional history

**Files:**
- Modify: `wren/brain.py`
- Modify: `tests/test_brain.py`

**Interfaces:**
- Consumes: nothing from Task 1 — this task only changes `chat()`'s signature and body; the caller (Task 3) is what actually passes `history_to_messages`'s output in.
- Produces: `brain.chat(text: str, history: list[dict] | None = None) -> str` — when `history` is a non-empty list, its entries are inserted between the system message and the final user message, in the given order.

- [ ] **Step 1: Write the failing tests**

In `tests/test_brain.py`, add these test functions after `test_chat_caps_max_tokens` (around line 213):

```python
def test_chat_without_history_matches_original_shape():
    client = _client_returning("Hello.")
    with patch.object(brain, "_get_client", return_value=client):
        brain.chat("hey")
    messages = client.chat.completions.create.call_args.kwargs["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "hey"}

def test_chat_with_history_inserts_between_system_and_final_user_message():
    client = _client_returning("Hello.")
    history = [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier reply"},
    ]
    with patch.object(brain, "_get_client", return_value=client):
        brain.chat("follow-up question", history=history)
    messages = client.chat.completions.create.call_args.kwargs["messages"]
    assert len(messages) == 4
    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "earlier question"}
    assert messages[2] == {"role": "assistant", "content": "earlier reply"}
    assert messages[3] == {"role": "user", "content": "follow-up question"}

def test_chat_with_empty_history_list_matches_no_history_shape():
    client = _client_returning("Hello.")
    with patch.object(brain, "_get_client", return_value=client):
        brain.chat("hey", history=[])
    messages = client.chat.completions.create.call_args.kwargs["messages"]
    assert len(messages) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_brain.py -k "test_chat_with_history or test_chat_without_history or test_chat_with_empty_history" -v`
Expected: FAIL (`TypeError: chat() got an unexpected keyword argument 'history'`)

- [ ] **Step 3: Update `brain.chat()`**

In `wren/brain.py`, replace:

```python
def chat(text: str) -> str:
    return _complete(
        [
            {"role": "system", "content": f"You are Wren, a personal assistant. Short, structured, ready. No filler. Answer directly, no reasoning or thinking process shown. /no_think Today is {_now()}."},
            {"role": "user", "content": text},
        ],
        temperature=0.7,
        max_tokens=400,
    )
```

with:

```python
def chat(text: str, history: list[dict] | None = None) -> str:
    messages = [
        {"role": "system", "content": f"You are Wren, a personal assistant. Short, structured, ready. No filler. Answer directly, no reasoning or thinking process shown. /no_think Today is {_now()}."},
    ]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": text})
    return _complete(messages, temperature=0.7, max_tokens=400)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_brain.py -v`
Expected: PASS (all tests in the file, including the 3 new ones — existing `test_chat_returns_string`/`test_chat_falls_back_to_second_provider_on_failure`/`test_chat_raises_when_all_providers_fail`/`test_chat_caps_max_tokens` still pass unmodified since `history` defaults to `None`)

- [ ] **Step 5: Commit**

```bash
git add wren/brain.py tests/test_brain.py
git commit -m "feat: add optional history parameter to brain.chat"
```

---

### Task 3: Wire history fetching into `bot.py`'s chat dispatch

**Files:**
- Modify: `wren/bot.py`

**Interfaces:**
- Consumes: `discord_utils.history_to_messages(messages: list[discord.Message], bot_user_id: int) -> list[dict]` (Task 1); `brain.chat(text: str, history: list[dict] | None = None) -> str` (Task 2).

- [ ] **Step 1: Update the `chat` branch in `on_message`**

In `wren/bot.py`, find:

```python
        else:  # chat
            reply = brain.chat(text)
            await message.channel.send(reply)
```

Replace with:

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

- [ ] **Step 2: Run the full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: PASS (no regressions — `bot.py` has no automated test coverage in this repo, module-level `client.run()` makes it unimportable without a real token, pre-existing condition unrelated to this change; this step confirms Tasks 1-2's tests still pass)

- [ ] **Step 3: Commit**

```bash
git add wren/bot.py
git commit -m "feat: fetch recent channel history for chat replies"
```

## Post-plan manual check

`bot.py`'s history-fetch-and-dispatch wiring has no automated test coverage. After Task 3 lands, manually verify against a running bot instance:
1. Send a casual message that falls through to chat (e.g. "tell me a fun fact"), then a follow-up referencing it (e.g. "say more about that") — confirm the second reply shows awareness of the first exchange.
2. Confirm a fresh conversation (no prior history in the DM) still gets a normal chat reply with no errors.
