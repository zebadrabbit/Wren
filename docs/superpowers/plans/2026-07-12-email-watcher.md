# Email-Arrival Watcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wren polls an IMAP inbox, and when unread mail arrives from a configured sender, DMs the mapped contact on Discord. This is the first real consumer of the plugin system's `start()` hook and `discord_utils.notify()`, and fixes two forward-looking issues flagged during the plugin-system review (a blocking `start_all()`, and a plugin contract that forced event-only plugins to declare vestigial `INTENTS`/`PROMPT_GUIDELINES`/`handle`).

**Architecture:** Four tasks. Task 1 fixes `plugins.py` (relaxed contract + non-blocking `start_all()`) — standalone, needed before a real event-only plugin can register cleanly. Task 2 adds IMAP/watch-list config to `config.py` — standalone. Task 3 builds `email_plugin.py` on top of Tasks 1+2 plus the existing `discord_utils.py`. Task 4 registers `email_plugin` in `plugins.py`'s `PLUGINS` list.

**Tech Stack:** Python 3.11+, stdlib `imaplib`/`email` (no new dependency), `pytest`, `asyncio.run(...)` from plain sync test functions (established pattern, no `pytest-asyncio`).

## Global Constraints

- No new dependencies — `imaplib`/`email` are stdlib.
- The email-watcher feature is opt-in: an empty `config.EMAIL_WATCH` means `email_plugin.start()` returns immediately without ever connecting to IMAP. No fail-fast validation of IMAP credentials at `config.py` import time.
- A poll failure (bad connection, bad creds, any exception) is logged as a warning and the loop continues on the next interval — it must never crash the bot.
- Dedup is via IMAP's own `\Seen` flag: every `UNSEEN` message is marked `\Seen` after processing, matched or not. No new persistence layer.
- Sender matching is exact, case-insensitive match on the bare email address (via `email.utils.parseaddr`) — no fuzzy/display-name matching.
- `plugins.py`'s plugin contract becomes: `INTENTS`/`PROMPT_GUIDELINES`/`handle` are only required for a plugin that handles user-invoked intents (use `getattr(plugin, ..., default)` when reading them); `start()` is optional and used for background work.
- `plugins.start_all()` must not block on a plugin whose `start()` never returns — wrap each call in `asyncio.create_task(...)`.

---

### Task 1: `plugins.py` — relaxed contract + non-blocking `start_all()`

**Files:**
- Modify: `plugins.py`
- Test: `tests/test_plugins.py` (add cases)

**Interfaces:**
- Produces (signatures unchanged, behavior changed): `plugins.all_intents() -> list[str]`, `plugins.all_guidelines() -> str`, `plugins.INTENT_HANDLERS: dict[str, module]`, `async def plugins.start_all(client) -> None`. All four now tolerate a plugin module that defines none of `INTENTS`/`PROMPT_GUIDELINES`/`handle`. `start_all()` no longer blocks on a plugin whose `start()` runs forever.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_plugins.py` (needs a new `import asyncio` and `import types` at the top, alongside the existing imports):

```python
import types

def test_event_only_plugin_contributes_nothing_to_intents_or_guidelines(monkeypatch):
    intents_before = plugins.all_intents()
    guidelines_before = plugins.all_guidelines()
    fake = types.SimpleNamespace()  # no INTENTS, no PROMPT_GUIDELINES, no handle
    monkeypatch.setattr(plugins, "PLUGINS", plugins.PLUGINS + [fake])
    assert plugins.all_intents() == intents_before
    assert plugins.all_guidelines() == guidelines_before

def test_start_all_does_not_block_on_long_running_plugin(monkeypatch):
    class SlowPlugin:
        async def start(self, client):
            await asyncio.sleep(3600)

    monkeypatch.setattr(plugins, "PLUGINS", [SlowPlugin()])

    async def run():
        await asyncio.wait_for(plugins.start_all(None), timeout=1)
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()

    asyncio.run(run())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_plugins.py -v`
Expected: `test_event_only_plugin_contributes_nothing_to_intents_or_guidelines` FAILS with `AttributeError: 'types.SimpleNamespace' object has no attribute 'INTENTS'` (current code does direct attribute access, not `getattr` with a default). `test_start_all_does_not_block_on_long_running_plugin` FAILS with a `TimeoutError` from `asyncio.wait_for` (current `start_all` awaits each `start()` directly, so it never returns within 1 second).

- [ ] **Step 3: Update `plugins.py`**

Replace the whole file:

```python
import asyncio
import notes_plugin
import shopping_plugin

PLUGINS = [notes_plugin, shopping_plugin]

INTENT_HANDLERS = {
    intent: plugin
    for plugin in PLUGINS
    for intent in getattr(plugin, "INTENTS", [])
}

def all_intents() -> list[str]:
    return [intent for plugin in PLUGINS for intent in getattr(plugin, "INTENTS", [])]

def all_guidelines() -> str:
    return "\n".join(
        text for plugin in PLUGINS
        if (text := getattr(plugin, "PROMPT_GUIDELINES", ""))
    )

async def start_all(client) -> None:
    for plugin in PLUGINS:
        if hasattr(plugin, "start"):
            asyncio.create_task(plugin.start(client))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_plugins.py -v`
Expected: PASS (all tests, including the 2 new ones)

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add plugins.py tests/test_plugins.py
git commit -m "fix: relax plugin contract and make plugins.start_all() non-blocking"
```

---

### Task 2: `config.py` — IMAP and email-watch-list settings

**Files:**
- Modify: `config.py` (append new settings)
- Modify: `.env.example`
- Test: `tests/test_config.py` (new — `config.py` has no dedicated test file yet)

**Interfaces:**
- Produces: `config.IMAP_HOST: str`, `config.IMAP_USER: str`, `config.IMAP_PASSWORD: str` (all default to `""` if unset — no fail-fast validation, this feature is opt-in), `config.EMAIL_POLL_SECONDS: int` (default `60`), `config.EMAIL_WATCH: dict[str, str]` (lowercased sender address → lowercased contact name), and the pure helper `config._parse_email_watch(raw: str) -> dict[str, str]` used to build it (extracted specifically so it's testable without reimporting the module under different env vars, same reasoning as `providers.resolve()` in prior work).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_config.py`:

```python
import os
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("HUSBAND_ID", "2")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

import config

def test_parse_email_watch_empty():
    assert config._parse_email_watch("") == {}

def test_parse_email_watch_single_pair():
    assert config._parse_email_watch("alice@example.com:owner") == {"alice@example.com": "owner"}

def test_parse_email_watch_multiple_pairs():
    result = config._parse_email_watch("alice@example.com:owner,bob@example.com:husband")
    assert result == {"alice@example.com": "owner", "bob@example.com": "husband"}

def test_parse_email_watch_whitespace_and_case_tolerant():
    result = config._parse_email_watch(" Alice@Example.com : Owner , bob@example.com:husband ")
    assert result == {"alice@example.com": "owner", "bob@example.com": "husband"}

def test_email_poll_seconds_default():
    assert config.EMAIL_POLL_SECONDS == 60
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config.py -v`
Expected: FAIL with `AttributeError: module 'config' has no attribute '_parse_email_watch'`

- [ ] **Step 3: Append to `config.py`**

Add at the end of the file, after the existing `ID_TO_NAME` line:

```python
def _parse_email_watch(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        addr, name = pair.split(":", 1)
        result[addr.strip().lower()] = name.strip().lower()
    return result

IMAP_HOST = os.environ.get("IMAP_HOST", "")
IMAP_USER = os.environ.get("IMAP_USER", "")
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "")
EMAIL_POLL_SECONDS = int(os.environ.get("EMAIL_POLL_SECONDS", "60"))
EMAIL_WATCH: dict[str, str] = _parse_email_watch(os.environ.get("EMAIL_WATCH", ""))
```

- [ ] **Step 4: Update `.env.example`**

Append:

```
# Email-arrival watcher (optional — leave EMAIL_WATCH unset to disable)
# IMAP_HOST=imap.example.com
# IMAP_USER=you@example.com
# IMAP_PASSWORD=app-password
# EMAIL_POLL_SECONDS=60
# EMAIL_WATCH=alice@example.com:owner,bob@example.com:husband
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_config.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Run the full suite**

Run: `pytest -q`
Expected: all tests pass

- [ ] **Step 7: Commit**

```bash
git add config.py .env.example tests/test_config.py
git commit -m "feat: add IMAP/email-watch-list config for the email watcher"
```

---

### Task 3: `email_plugin.py` — IMAP poll loop

**Files:**
- Create: `email_plugin.py`
- Test: `tests/test_email_plugin.py`

**Interfaces:**
- Consumes: `config.IMAP_HOST`/`IMAP_USER`/`IMAP_PASSWORD`/`EMAIL_POLL_SECONDS`/`EMAIL_WATCH` (Task 2), `discord_utils.notify(client, contact_name, text) -> bool` (existing, from the plugin-system work)
- Produces: `email_plugin._connect() -> imaplib.IMAP4_SSL`, `email_plugin._sender_address(raw_from: str) -> str`, `async def email_plugin._poll_once(client, imap_conn) -> None`, `async def email_plugin.start(client) -> None`. This is an event-only plugin — it deliberately does NOT define `INTENTS`/`PROMPT_GUIDELINES`/`handle` (Task 1 made those optional).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_email_plugin.py`:

```python
import os, asyncio
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("HUSBAND_ID", "2")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import MagicMock, AsyncMock, patch
import config
import discord_utils
import email_plugin

def _raw_email(from_addr: str, subject: str) -> bytes:
    return f"From: {from_addr}\r\nSubject: {subject}\r\n\r\nBody text.".encode()

def test_sender_address_plain():
    assert email_plugin._sender_address("alice@example.com") == "alice@example.com"

def test_sender_address_display_name_form():
    assert email_plugin._sender_address("Alice <Alice@Example.com>") == "alice@example.com"

def test_poll_once_notifies_on_match(monkeypatch):
    monkeypatch.setattr(config, "EMAIL_WATCH", {"alice@example.com": "owner"})
    imap_conn = MagicMock()
    imap_conn.search.return_value = ("OK", [b"1"])
    imap_conn.fetch.return_value = ("OK", [(b"1 (RFC822 {size})", _raw_email("alice@example.com", "Hello"))])

    with patch.object(discord_utils, "notify", new=AsyncMock(return_value=True)) as mock_notify:
        asyncio.run(email_plugin._poll_once(None, imap_conn))

    mock_notify.assert_awaited_once_with(None, "owner", "Email from alice@example.com: Hello")
    imap_conn.store.assert_called_once_with(b"1", "+FLAGS", "\\Seen")

def test_poll_once_no_match_still_marks_seen(monkeypatch):
    monkeypatch.setattr(config, "EMAIL_WATCH", {"alice@example.com": "owner"})
    imap_conn = MagicMock()
    imap_conn.search.return_value = ("OK", [b"1"])
    imap_conn.fetch.return_value = ("OK", [(b"1 (RFC822 {size})", _raw_email("stranger@example.com", "Hi"))])

    with patch.object(discord_utils, "notify", new=AsyncMock()) as mock_notify:
        asyncio.run(email_plugin._poll_once(None, imap_conn))

    mock_notify.assert_not_called()
    imap_conn.store.assert_called_once_with(b"1", "+FLAGS", "\\Seen")

def test_poll_once_processes_multiple_messages(monkeypatch):
    monkeypatch.setattr(config, "EMAIL_WATCH", {"alice@example.com": "owner", "bob@example.com": "husband"})
    imap_conn = MagicMock()
    imap_conn.search.return_value = ("OK", [b"1 2"])
    imap_conn.fetch.side_effect = [
        ("OK", [(b"1 (RFC822 {size})", _raw_email("alice@example.com", "One"))]),
        ("OK", [(b"2 (RFC822 {size})", _raw_email("bob@example.com", "Two"))]),
    ]

    with patch.object(discord_utils, "notify", new=AsyncMock(return_value=True)) as mock_notify:
        asyncio.run(email_plugin._poll_once(None, imap_conn))

    assert mock_notify.await_count == 2

def test_start_returns_immediately_when_email_watch_empty(monkeypatch):
    monkeypatch.setattr(config, "EMAIL_WATCH", {})

    def _fail_connect():
        raise AssertionError("_connect should not be called when EMAIL_WATCH is empty")

    monkeypatch.setattr(email_plugin, "_connect", _fail_connect)

    asyncio.run(asyncio.wait_for(email_plugin.start(None), timeout=1))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_email_plugin.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'email_plugin'`

- [ ] **Step 3: Write `email_plugin.py`**

```python
import asyncio
import email
import email.utils
import imaplib
import logging
import config
import discord_utils

def _connect() -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(config.IMAP_HOST)
    conn.login(config.IMAP_USER, config.IMAP_PASSWORD)
    conn.select("INBOX")
    return conn

def _sender_address(raw_from: str) -> str:
    _, addr = email.utils.parseaddr(raw_from)
    return addr.lower()

async def _poll_once(client, imap_conn) -> None:
    status, data = imap_conn.search(None, "UNSEEN")
    if status != "OK":
        return
    for num in data[0].split():
        status, msg_data = imap_conn.fetch(num, "(RFC822)")
        if status != "OK":
            continue
        msg = email.message_from_bytes(msg_data[0][1])
        sender = _sender_address(msg.get("From", ""))
        contact = config.EMAIL_WATCH.get(sender)
        if contact:
            subject = msg.get("Subject", "(no subject)")
            await discord_utils.notify(client, contact, f"Email from {sender}: {subject}")
        imap_conn.store(num, "+FLAGS", "\\Seen")

async def start(client) -> None:
    if not config.EMAIL_WATCH:
        return
    while True:
        try:
            conn = _connect()
            try:
                await _poll_once(client, conn)
            finally:
                conn.logout()
        except Exception as e:
            logging.warning(f"email watcher poll failed: {e}")
        await asyncio.sleep(config.EMAIL_POLL_SECONDS)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_email_plugin.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add email_plugin.py tests/test_email_plugin.py
git commit -m "feat: add email_plugin.py IMAP poll loop for the email watcher"
```

---

### Task 4: Register `email_plugin` in `plugins.py`

**Files:**
- Modify: `plugins.py:2-4` (imports and `PLUGINS` list)
- Test: `tests/test_plugins.py` (add cases)

**Interfaces:**
- Consumes: `email_plugin` (Task 3), the relaxed contract from Task 1
- Produces: no new functions — `plugins.PLUGINS` now includes `email_plugin`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_plugins.py` (needs `import email_plugin` added to the top imports alongside the existing `notes_plugin`/`shopping_plugin` imports):

```python
def test_email_plugin_registered():
    assert email_plugin in plugins.PLUGINS

def test_all_intents_unaffected_by_event_only_email_plugin():
    intents = plugins.all_intents()
    assert "save_note" in intents
    assert "add_shopping_item" in intents
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_plugins.py -v`
Expected: FAIL — `test_email_plugin_registered` fails with `AssertionError` (email_plugin not yet in `PLUGINS`)

- [ ] **Step 3: Update `plugins.py`**

Change:

```python
import asyncio
import notes_plugin
import shopping_plugin

PLUGINS = [notes_plugin, shopping_plugin]
```

to:

```python
import asyncio
import notes_plugin
import shopping_plugin
import email_plugin

PLUGINS = [notes_plugin, shopping_plugin, email_plugin]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_plugins.py -v`
Expected: PASS (all tests, including the 2 new ones)

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add plugins.py tests/test_plugins.py
git commit -m "feat: register email_plugin in the plugin registry"
```

---

## Post-plan verification

- [ ] Run `pytest -q` — expect all tests (existing + new) passing.
- [ ] Manually confirm `.env.example` documents `IMAP_HOST`/`IMAP_USER`/`IMAP_PASSWORD`/`EMAIL_POLL_SECONDS`/`EMAIL_WATCH`, all commented out (opt-in feature, no impact on anyone who doesn't set `EMAIL_WATCH`).
