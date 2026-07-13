# Status Command Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new core `status` intent ("show model" / "what backend are you using") reports the last successfully-used LLM provider (not just the configured primary — Wren may have silently fallen back), process uptime, and token usage accumulated since last restart.

**Architecture:** Two tasks. Task 1 (`brain.py`) adds the tracking state (`_last_provider`, `_token_usage`), the `status()` accessor, and the prompt changes. Task 2 (`bot.py`) adds the uptime clock, a formatter, and the dispatch branch that combines both into one reply. Task 2 depends on Task 1's `brain.status()`.

**Tech Stack:** Python 3.11+, stdlib `time` (no new dependency), `pytest`.

## Global Constraints

- No persistence — `_last_provider`/`_token_usage` reset on restart (matches the "session" framing agreed in the design).
- `_last_provider` reflects the provider that **actually succeeded most recently**, not `config.LLM_CHAIN[0]` — this must update correctly even after a fallback (primary fails, secondary succeeds → `_last_provider` becomes the secondary).
- `status()` falls back to `config.LLM_CHAIN[0]` only when no LLM call has succeeded yet since startup (`_last_provider is None`).
- Token accumulation must tolerate a backend response with no `.usage` field at all (not every OpenAI-compatible server populates it) — must not crash, must not increment.
- **`tests/test_brain.py`'s shared `_mock_completion()` helper must be updated to set `resp.usage = None` explicitly.** Without this, every existing test using it gets a `MagicMock` auto-attribute for `.usage` (truthy, not `None`), and the new token-accumulation code would try to add a `MagicMock` object to an `int` and crash on tests that have nothing to do with this feature. This is not optional polish — it's required for the existing suite to keep passing.
- Still no `tests/test_bot.py` (pre-existing condition, `bot.py`'s module-level `client.run()` call).

---

### Task 1: `brain.py` — track last-used provider and token usage

**Files:**
- Modify: `wren/brain.py`
- Test: `tests/test_brain.py` (update shared helper + add cases)

**Interfaces:**
- Produces: `brain.status() -> dict` — `{"provider": {"name", "base_url", "model", ...}, "tokens": {"prompt": int, "completion": int, "total": int}}`. `_complete()`'s signature and return type are unchanged; it gains internal side effects only (updating `_last_provider`/`_token_usage` on success).

- [ ] **Step 1: Update the shared test helper and write the failing tests**

In `tests/test_brain.py`, change `_mock_completion` from:

```python
def _mock_completion(content: str):
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp
```

to:

```python
def _mock_completion(content: str):
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = None
    return resp
```

Then add these new tests:

```python
def test_complete_tracks_token_usage(monkeypatch):
    monkeypatch.setattr(brain, "_token_usage", {"prompt": 0, "completion": 0, "total": 0})
    monkeypatch.setattr(brain, "_last_provider", None)
    resp = _mock_completion("hi")
    resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    client = MagicMock()
    client.chat.completions.create.return_value = resp
    with patch.object(brain, "_get_client", return_value=client):
        brain.chat("hello")
    assert brain._token_usage == {"prompt": 10, "completion": 5, "total": 15}

def test_complete_handles_missing_usage_gracefully(monkeypatch):
    monkeypatch.setattr(brain, "_token_usage", {"prompt": 0, "completion": 0, "total": 0})
    monkeypatch.setattr(brain, "_last_provider", None)
    with patch.object(brain, "_get_client", return_value=_client_returning("hi")):
        brain.chat("hello")
    assert brain._token_usage == {"prompt": 0, "completion": 0, "total": 0}

def test_complete_updates_last_provider_on_success(monkeypatch):
    monkeypatch.setattr(brain, "_last_provider", None)
    with patch.object(brain, "_get_client", return_value=_client_returning("hi")):
        brain.chat("hello")
    assert brain._last_provider["name"] == "lmstudio"

def test_complete_updates_last_provider_after_fallback(monkeypatch):
    monkeypatch.setattr(brain, "_last_provider", None)
    primary = _client_raising(RuntimeError("primary down"))
    secondary = _client_returning("fallback reply")

    def fake_get_client(provider):
        return primary if provider["name"] == "lmstudio" else secondary

    with patch.object(brain, "_get_client", side_effect=fake_get_client):
        brain.chat("hey")
    assert brain._last_provider["name"] == "ollama"

def test_status_before_any_call_uses_configured_primary(monkeypatch):
    monkeypatch.setattr(brain, "_last_provider", None)
    monkeypatch.setattr(brain, "_token_usage", {"prompt": 0, "completion": 0, "total": 0})
    result = brain.status()
    assert result["provider"]["name"] == config.LLM_CHAIN[0]["name"]
    assert result["tokens"] == {"prompt": 0, "completion": 0, "total": 0}

def test_status_after_call_uses_last_provider(monkeypatch):
    monkeypatch.setattr(brain, "_last_provider", None)
    monkeypatch.setattr(brain, "_token_usage", {"prompt": 0, "completion": 0, "total": 0})
    with patch.object(brain, "_get_client", return_value=_client_returning("hi")):
        brain.chat("hello")
    result = brain.status()
    assert result["provider"]["name"] == "lmstudio"

def test_detect_intent_prompt_includes_status_intent():
    payload = json.dumps({"intent": "chat", "content": "hi", "tags": [], "person": None})
    captured = {}

    def fake_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        return _mock_completion(payload)

    client = MagicMock()
    client.chat.completions.create.side_effect = fake_create
    with patch.object(brain, "_get_client", return_value=client):
        brain.detect_intent(1, "hello")

    system_content = captured["messages"][0]["content"]
    assert '"status"' in system_content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_brain.py -v`
Expected: FAIL — `_token_usage`/`_last_provider`/`status` don't exist yet (`AttributeError`); `test_detect_intent_prompt_includes_status_intent` fails because `"status"` isn't in the schema yet.

- [ ] **Step 3: Update `wren/brain.py`**

Change the intent enum line in `_SYSTEM` from:

```python
  "intent": "send_to_person" | "help" | {plugin_intents} | "chat",
```

to:

```python
  "intent": "send_to_person" | "help" | "status" | {plugin_intents} | "chat",
```

Add a new guideline bullet right after the `help` one:

```python
- help: user wants to know what Wren can do, asks for help, or asks to see available commands
- status: user wants to know Wren's operational status — active LLM backend/model/endpoint, uptime, token usage
```

Add module state right after `register_plugins`:

```python
_last_provider: dict | None = None
_token_usage = {"prompt": 0, "completion": 0, "total": 0}
```

Replace `_complete`:

```python
def _complete(messages: list[dict], temperature: float, max_tokens: int = 400) -> str:
    global _last_provider
    last_exc: Exception | None = None
    for provider in config.LLM_CHAIN:
        try:
            client = _get_client(provider)
            resp = client.chat.completions.create(
                model=provider["model"],
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            usage = getattr(resp, "usage", None)
            if usage is not None:
                _token_usage["prompt"] += usage.prompt_tokens or 0
                _token_usage["completion"] += usage.completion_tokens or 0
                _token_usage["total"] += usage.total_tokens or 0
            _last_provider = provider
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logging.warning(f"LLM provider '{provider['name']}' failed: {e}")
            last_exc = e
    raise last_exc
```

Add a new function after `_complete`:

```python
def status() -> dict:
    return {
        "provider": _last_provider or config.LLM_CHAIN[0],
        "tokens": dict(_token_usage),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_brain.py -v`
Expected: PASS (all tests, including the 7 new ones)

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: all tests pass — this specifically confirms the `_mock_completion` helper change (Step 1) didn't break any pre-existing test that uses it.

- [ ] **Step 6: Commit**

```bash
git add wren/brain.py tests/test_brain.py
git commit -m "feat: track last-used LLM provider and token usage in brain.py"
```

---

### Task 2: `bot.py` — uptime + the `status` dispatch branch

**Files:**
- Modify: `wren/bot.py`

**Interfaces:**
- Consumes: `brain.status() -> dict` (Task 1)
- Produces: no new functions exported — `_format_uptime(seconds: float) -> str` is an internal helper. No changes to any existing function's signature.

There is no automated test for this task — same pre-existing condition as all prior `bot.py` work (module-level `client.run(...)` makes it unimportable without a real token). Verification is: run the full suite (confirms nothing else broke) plus a manual read-back with hand-computed expected output for the formatter.

- [ ] **Step 1: Update `wren/bot.py`**

Add `import time` to the top imports (alongside `import logging`).

Add `_START_TIME = time.monotonic()` right after the `HELP_TEXT` constant, before `brain.register_plugins(...)`:

```python
_START_TIME = time.monotonic()
```

Add `_format_uptime` right after `_react`:

```python
def _format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if days or hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)
```

Insert a new branch in `on_message`, immediately after the `elif intent == "help":` branch and before `elif intent == "send_to_person":`:

```python
        elif intent == "status":
            info = brain.status()
            provider = info["provider"]
            tokens = info["tokens"]
            uptime = _format_uptime(time.monotonic() - _START_TIME)
            lines = [
                f"Backend: {provider['name']} ({provider['model']})",
                f"Endpoint: {provider['base_url']}",
                f"Uptime: {uptime}",
                f"Tokens this session: {tokens['total']:,} ({tokens['prompt']:,} prompt / {tokens['completion']:,} completion)",
            ]
            await message.channel.send("\n".join(lines))
```

- [ ] **Step 2: Run the full test suite**

Run: `pytest -q`
Expected: all tests pass (this task adds no new automated tests)

- [ ] **Step 3: Manual read-back verification**

Re-read the modified `bot.py` in full and confirm:
- `import time` is present.
- `_START_TIME` is set once at module level, before any event handler runs.
- `_format_uptime`'s logic, hand-traced against 3 known inputs:
  - `90` seconds → `days=0, hours=0, minutes=1` → `"1m"` (the `if days or hours` check is false, so only minutes is appended)
  - `3800` seconds → `days=0, hours=1, minutes=3` → `"1h 3m"`
  - `90000` seconds → `days=1, hours=1, minutes=0` → `"1d 1h 0m"`
  
  Confirm these three by hand against the actual code in the file (not just this plan) before considering the step done.
- The new `status` branch is correctly placed between `help` and `send_to_person`, at the same indentation level as the other `elif` branches, and doesn't disturb any existing branch.

- [ ] **Step 4: Commit**

```bash
git add wren/bot.py
git commit -m "feat: add status intent reporting LLM backend, uptime, and token usage"
```

---

## Post-plan verification

- [ ] Run `pytest -q` — expect all tests (existing + new) passing.
- [ ] Manually confirm `README.md`'s "What you can say" section and `HELP_TEXT` in `bot.py` both mention the new status command (e.g. "show status" / "what backend are you using") — not part of any task above, add as a small doc update if missed, matching the precedent set by the reminders feature's own post-plan verification step.
