# LLM Provider Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wren tries a configured, ordered list of LLM providers (lmstudio, ollama, openai, claude, openrouter) for every LLM call, falling back to the next provider in the list on any exception, so the bot keeps operating if its primary endpoint goes down.

**Architecture:** A new `providers.py` module resolves provider configs (base_url/api_key/model) from env vars, skipping any provider missing required config. `config.py` reads `LLM_PROVIDERS` (comma-ordered list) and builds `config.LLM_CHAIN` from resolved providers, raising at startup if the chain ends up empty. `brain.py` replaces its single module-level OpenAI client with a per-provider client cache and a `_complete()` helper that walks `config.LLM_CHAIN`, trying each provider's client until one succeeds or the chain is exhausted.

**Tech Stack:** Python 3.11+, `openai` SDK (already a dependency — all 5 providers speak the OpenAI chat-completions wire format, including Claude via Anthropic's OpenAI-compatible endpoint), `pytest` + `unittest.mock`.

## Global Constraints

- No new dependencies — reuse the existing `openai` package for every provider.
- Fallback triggers on **any** exception from a provider's `chat.completions.create` call (spec: "any exception" is simpler and matches the bot's existing broad try/except style).
- A provider missing required env config is **silently skipped** when building the chain, not an error — only an empty resulting chain raises at startup.
- Existing public functions (`brain.detect_intent`, `brain.recall`, `brain.chat`) keep their current signatures — `bot.py` does not change.

---

### Task 1: `providers.py` — provider config resolution

**Files:**
- Create: `providers.py`
- Test: `tests/test_providers.py`

**Interfaces:**
- Produces: `providers.PROVIDER_DEFAULTS: dict[str, dict]` and `providers.resolve(name: str) -> dict | None`. `resolve()` returns `{"name": str, "base_url": str, "api_key": str, "model": str}` on success, or `None` if the provider name is unknown or required env vars are missing.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_providers.py`:

```python
import os
from unittest.mock import patch
import providers

def test_resolve_unknown_provider_returns_none():
    assert providers.resolve("not-a-real-provider") is None

def test_resolve_lmstudio_requires_base_url_and_model():
    with patch.dict(os.environ, {}, clear=True):
        assert providers.resolve("lmstudio") is None

def test_resolve_lmstudio_success():
    env = {
        "LMSTUDIO_BASE_URL": "http://192.168.1.70:30068/v1",
        "LMSTUDIO_MODEL": "gemma4-e4b-131k:latest",
    }
    with patch.dict(os.environ, env, clear=True):
        result = providers.resolve("lmstudio")
    assert result == {
        "name": "lmstudio",
        "base_url": "http://192.168.1.70:30068/v1",
        "api_key": "not-needed",
        "model": "gemma4-e4b-131k:latest",
    }

def test_resolve_ollama_uses_default_base_url():
    with patch.dict(os.environ, {"OLLAMA_MODEL": "llama3"}, clear=True):
        result = providers.resolve("ollama")
    assert result["base_url"] == "http://localhost:11434/v1"
    assert result["model"] == "llama3"
    assert result["api_key"] == "not-needed"

def test_resolve_openai_requires_api_key():
    with patch.dict(os.environ, {"OPENAI_MODEL": "gpt-4o-mini"}, clear=True):
        assert providers.resolve("openai") is None

def test_resolve_openai_success():
    env = {"OPENAI_API_KEY": "sk-test", "OPENAI_MODEL": "gpt-4o-mini"}
    with patch.dict(os.environ, env, clear=True):
        result = providers.resolve("openai")
    assert result == {
        "name": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key": "sk-test",
        "model": "gpt-4o-mini",
    }

def test_resolve_claude_success():
    env = {"ANTHROPIC_API_KEY": "sk-ant-test", "CLAUDE_MODEL": "claude-sonnet-5"}
    with patch.dict(os.environ, env, clear=True):
        result = providers.resolve("claude")
    assert result["base_url"] == "https://api.anthropic.com/v1"
    assert result["api_key"] == "sk-ant-test"
    assert result["model"] == "claude-sonnet-5"

def test_resolve_openrouter_success():
    env = {"OPENROUTER_API_KEY": "sk-or-test", "OPENROUTER_MODEL": "meta-llama/llama-3"}
    with patch.dict(os.environ, env, clear=True):
        result = providers.resolve("openrouter")
    assert result["base_url"] == "https://openrouter.ai/api/v1"
    assert result["api_key"] == "sk-or-test"
    assert result["model"] == "meta-llama/llama-3"

def test_resolve_missing_model_returns_none():
    env = {"OPENAI_API_KEY": "sk-test"}
    with patch.dict(os.environ, env, clear=True):
        assert providers.resolve("openai") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_providers.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'providers'`

- [ ] **Step 3: Write `providers.py`**

```python
import os

PROVIDER_DEFAULTS = {
    "lmstudio": {
        "default_base_url": None,
        "base_url_env": "LMSTUDIO_BASE_URL",
        "api_key_env": None,
        "model_env": "LMSTUDIO_MODEL",
    },
    "ollama": {
        "default_base_url": "http://localhost:11434/v1",
        "base_url_env": "OLLAMA_BASE_URL",
        "api_key_env": None,
        "model_env": "OLLAMA_MODEL",
    },
    "openai": {
        "default_base_url": "https://api.openai.com/v1",
        "base_url_env": "OPENAI_BASE_URL",
        "api_key_env": "OPENAI_API_KEY",
        "model_env": "OPENAI_MODEL",
    },
    "claude": {
        "default_base_url": "https://api.anthropic.com/v1",
        "base_url_env": "CLAUDE_BASE_URL",
        "api_key_env": "ANTHROPIC_API_KEY",
        "model_env": "CLAUDE_MODEL",
    },
    "openrouter": {
        "default_base_url": "https://openrouter.ai/api/v1",
        "base_url_env": "OPENROUTER_BASE_URL",
        "api_key_env": "OPENROUTER_API_KEY",
        "model_env": "OPENROUTER_MODEL",
    },
}

def resolve(name: str) -> dict | None:
    spec = PROVIDER_DEFAULTS.get(name)
    if spec is None:
        return None

    base_url = os.environ.get(spec["base_url_env"]) or spec["default_base_url"]
    if not base_url:
        return None

    if spec["api_key_env"]:
        api_key = os.environ.get(spec["api_key_env"])
        if not api_key:
            return None
    else:
        api_key = "not-needed"

    model = os.environ.get(spec["model_env"])
    if not model:
        return None

    return {"name": name, "base_url": base_url, "api_key": api_key, "model": model}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_providers.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add providers.py tests/test_providers.py
git commit -m "feat: add provider config resolution for LLM fallback chain"
```

---

### Task 2: `config.py` — build the ordered provider chain

**Files:**
- Modify: `config.py:12-14` (remove hardcoded `LLM_BASE_URL`/`LLM_MODEL`, add chain building)
- Modify: `.env.example`

**Interfaces:**
- Consumes: `providers.resolve(name: str) -> dict | None` (Task 1)
- Produces: `config.LLM_CHAIN: list[dict]` — ordered list of resolved provider configs, each `{"name", "base_url", "api_key", "model"}`. Raises `RuntimeError` at import time if the chain is empty.

This task has no isolated unit test of its own: `providers.resolve()` (the substantive logic) is already covered by Task 1, and `config.py` only exists as a real module loaded once at process start — reloading it under different env vars per test would require `importlib.reload` gymnastics for a few lines of glue. Task 3's `tests/test_brain.py` exercises the real `config.LLM_CHAIN` end-to-end instead.

- [ ] **Step 1: Update `config.py`**

Replace lines 12-14 (`DISCORD_TOKEN = _require(...)` through `LLM_MODEL = ...`):

```python
DISCORD_TOKEN = _require("DISCORD_TOKEN")

_provider_names = [p.strip() for p in os.environ.get("LLM_PROVIDERS", "").split(",") if p.strip()]
LLM_CHAIN = [c for c in (providers.resolve(name) for name in _provider_names) if c is not None]
if not LLM_CHAIN:
    raise RuntimeError(
        "No usable LLM providers configured. Set LLM_PROVIDERS in .env to a "
        "comma-separated list (e.g. lmstudio,openai) and set that provider's "
        "base_url/api_key/model env vars."
    )
```

Add `import providers` to the top imports (after `import os`).

- [ ] **Step 2: Update `.env.example`**

```
DISCORD_TOKEN=your_bot_token_here
OWNER_ID=111111111111111111
HUSBAND_ID=222222222222222222

# Comma-separated priority order. Wren tries each in order until one succeeds.
# Available: lmstudio, ollama, openai, claude, openrouter
LLM_PROVIDERS=lmstudio

LMSTUDIO_BASE_URL=http://192.168.1.70:30068/v1
LMSTUDIO_MODEL=gemma4-e4b-131k:latest

# OLLAMA_BASE_URL=http://localhost:11434/v1
# OLLAMA_MODEL=llama3

# OPENAI_API_KEY=sk-...
# OPENAI_MODEL=gpt-4o-mini

# ANTHROPIC_API_KEY=sk-ant-...
# CLAUDE_MODEL=claude-sonnet-5

# OPENROUTER_API_KEY=sk-or-...
# OPENROUTER_MODEL=meta-llama/llama-3
```

- [ ] **Step 3: Verify by hand**

Run: `python3 -c "import config; print(config.LLM_CHAIN)"` from the project root with a real `.env` present (or with `LLM_PROVIDERS=lmstudio LMSTUDIO_BASE_URL=http://x LMSTUDIO_MODEL=m DISCORD_TOKEN=t OWNER_ID=1 HUSBAND_ID=2 python3 -c "import config; print(config.LLM_CHAIN)"` if no `.env` is set up).
Expected: prints a one-item list with `name: lmstudio`.

- [ ] **Step 4: Commit**

```bash
git add config.py .env.example
git commit -m "feat: build ordered LLM provider chain from LLM_PROVIDERS env var"
```

---

### Task 3: `brain.py` — fallback across the provider chain

**Files:**
- Modify: `brain.py:1-6` (client setup) and every function that currently calls `_client.chat.completions.create` (`detect_intent`, `recall`, `chat`)
- Test: `tests/test_brain.py` (rewrite)

**Interfaces:**
- Consumes: `config.LLM_CHAIN: list[dict]` (Task 2), each item `{"name", "base_url", "api_key", "model"}`
- Produces: `brain._get_client(provider: dict) -> OpenAI`, `brain._complete(messages: list[dict], temperature: float) -> str` (raises the last provider's exception if every provider in the chain fails). `brain.detect_intent`, `brain.recall`, `brain.chat` keep their existing signatures.

- [ ] **Step 1: Write the failing tests**

Replace `tests/test_brain.py` entirely:

```python
import os, json, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("HUSBAND_ID", "2")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio,ollama")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test-primary")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model-1")
os.environ.setdefault("OLLAMA_MODEL", "test-model-2")

from unittest.mock import patch, MagicMock
import brain

def _mock_completion(content: str):
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp

def _client_returning(content: str) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_completion(content)
    return client

def _client_raising(exc: Exception) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create.side_effect = exc
    return client

def test_detect_intent_save():
    payload = json.dumps({
        "intent": "save_note",
        "content": "buy milk",
        "tags": ["grocery"],
        "person": None
    })
    with patch.object(brain, "_get_client", return_value=_client_returning(payload)):
        result = brain.detect_intent(1, "remind me to buy milk")
    assert result["intent"] == "save_note"
    assert result["tags"] == ["grocery"]

def test_detect_intent_bad_json_falls_back_to_chat():
    with patch.object(brain, "_get_client", return_value=_client_returning("not json")):
        result = brain.detect_intent(1, "hello")
    assert result["intent"] == "chat"

def test_detect_intent_all_providers_fail_falls_back_to_chat():
    with patch.object(brain, "_get_client", return_value=_client_raising(RuntimeError("down"))):
        result = brain.detect_intent(1, "hello")
    assert result["intent"] == "chat"

def test_recall_returns_string():
    sample_notes = [{"content": "buy eggs", "tags": "grocery", "created_at": "2026-06-25T10:00:00+00:00"}]
    with patch.object(brain, "_get_client", return_value=_client_returning("You need eggs.")):
        result = brain.recall(sample_notes, "what groceries do I need?")
    assert isinstance(result, str)
    assert len(result) > 0

def test_chat_returns_string():
    with patch.object(brain, "_get_client", return_value=_client_returning("Hello.")):
        result = brain.chat("hey")
    assert isinstance(result, str)

def test_chat_falls_back_to_second_provider_on_failure():
    primary = _client_raising(RuntimeError("primary down"))
    secondary = _client_returning("fallback reply")

    def fake_get_client(provider):
        return primary if provider["name"] == "lmstudio" else secondary

    with patch.object(brain, "_get_client", side_effect=fake_get_client):
        result = brain.chat("hey")
    assert result == "fallback reply"

def test_chat_raises_when_all_providers_fail():
    with patch.object(brain, "_get_client", return_value=_client_raising(RuntimeError("down"))):
        with pytest.raises(RuntimeError):
            brain.chat("hey")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_brain.py -v`
Expected: FAIL — `AttributeError: module 'brain' has no attribute '_get_client'` (and `brain._client` no longer exists, so collection of the old file would already be broken — that's expected, we're replacing it).

- [ ] **Step 3: Rewrite `brain.py`**

Replace lines 1-6:

```python
import json
import logging
from datetime import datetime, timezone
from openai import OpenAI
import config
```

(drop the old `_client = OpenAI(...)` line entirely)

Add after the `_contacts()` function and before `detect_intent`:

```python
_clients: dict[str, OpenAI] = {}

def _get_client(provider: dict) -> OpenAI:
    if provider["name"] not in _clients:
        _clients[provider["name"]] = OpenAI(base_url=provider["base_url"], api_key=provider["api_key"])
    return _clients[provider["name"]]

def _complete(messages: list[dict], temperature: float) -> str:
    last_exc: Exception | None = None
    for provider in config.LLM_CHAIN:
        try:
            client = _get_client(provider)
            resp = client.chat.completions.create(
                model=provider["model"],
                messages=messages,
                temperature=temperature,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logging.warning(f"LLM provider '{provider['name']}' failed: {e}")
            last_exc = e
    raise last_exc
```

Replace `detect_intent`:

```python
def detect_intent(user_id: int, text: str) -> dict:
    system = _SYSTEM.format(date=_now(), contacts=_contacts())
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

Replace `recall`:

```python
def recall(notes: list[dict], query: str) -> str:
    notes_text = "\n".join(
        f"- [{n['created_at'][:10]}] {n['content']} (tags: {n['tags']})"
        for n in notes
    )
    prompt = f"User's notes:\n{notes_text}\n\nUser asked: {query}\n\nAnswer directly using only what's in the notes."
    return _complete(
        [
            {"role": "system", "content": "You are Wren. Short, structured, ready. No filler."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
    )
```

Replace `chat`:

```python
def chat(text: str) -> str:
    return _complete(
        [
            {"role": "system", "content": f"You are Wren, a personal assistant. Short, structured, ready. No filler. Today is {_now()}."},
            {"role": "user", "content": text},
        ],
        temperature=0.7,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_brain.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: all tests pass (existing `tests/test_notes.py` untouched by this task still passes)

- [ ] **Step 6: Commit**

```bash
git add brain.py tests/test_brain.py
git commit -m "feat: fall back across LLM provider chain on any request failure"
```

---

### Task 4: `notes.py` — fix substring tag-match bug

**Files:**
- Modify: `notes.py:30-45` (`search` function)
- Test: `tests/test_notes.py`

**Interfaces:**
- Consumes: nothing new
- Produces: `notes.search(owner_id: int, tags: list[str] | None = None) -> list[dict]` — same signature, now matches tags as exact comma-delimited tokens instead of substrings.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_notes.py`:

```python
def test_search_tag_is_exact_not_substring():
    notes.save(1, "note about home", ["home"])
    notes.save(1, "note about homework", ["homework"])
    results = notes.search(1, tags=["home"])
    assert len(results) == 1
    assert results[0]["content"] == "note about home"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_notes.py::test_search_tag_is_exact_not_substring -v`
Expected: FAIL — 2 results returned instead of 1 (the old `LIKE '%home%'` matches `"homework"` too)

- [ ] **Step 3: Fix `search()` in `notes.py`**

Replace the whole function (lines 30-45):

```python
def search(owner_id: int, tags: list[str] | None = None) -> list[dict]:
    with _conn() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM notes WHERE owner_id=? ORDER BY created_at DESC",
            (str(owner_id),),
        ).fetchall()
    results = [dict(r) for r in rows]
    if tags:
        wanted = set(tags)
        results = [
            r for r in results
            if wanted & set(filter(None, r["tags"].split(",")))
        ]
    return results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_notes.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add notes.py tests/test_notes.py
git commit -m "fix: match note tags exactly instead of substring (home != homework)"
```

---

## Post-plan verification

- [ ] Run `pytest -q` — expect all tests (existing + new) passing.
- [ ] Manually confirm `.env.example` documents every provider env var used by `providers.py`.
