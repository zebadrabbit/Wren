# List Active Plugins Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** "what plugins do you have" / "what's active" reports which of Wren's plugins are currently active, via a new `list_plugins` core intent — distinct from `HELP_TEXT` (example phrases) and `status` (LLM backend/uptime/tokens).

**Architecture:** Each of the 7 existing plugin files gains a `PLUGIN_NAME` string constant; the two plugins that are ever conditionally inactive (`web_plugin`, `email_plugin`) also gain an `is_active() -> bool`. `wren/plugins.py` gains `plugin_status()`, aggregating name+active state across `PLUGINS`. `bot.py` gains a new core-intent branch (alongside `help`/`status`) that formats and sends the result.

**Tech Stack:** Python 3.11+, `pytest`. No new dependencies.

## Global Constraints

- A plugin with no `is_active` defined reports `True` (always-active by omission) — `plugin_status()` must never require every plugin to define `is_active`.
- A plugin with no `PLUGIN_NAME` falls back to its module `__name__` — `plugin_status()` must never raise on a plugin missing `PLUGIN_NAME`.
- `list_plugins` is a core intent (lives in `bot.py`, like `help`/`status`), not owned by any single plugin's `INTENT_HANDLERS` entry.
- No new dependencies.

---

### Task 1: `plugins.plugin_status()` and `PLUGIN_NAME`/`is_active()` on each plugin

**Files:**
- Modify: `wren/notes_plugin.py`
- Modify: `wren/shopping_plugin.py`
- Modify: `wren/reminder_plugin.py`
- Modify: `wren/contacts_plugin.py`
- Modify: `wren/pins_plugin.py`
- Modify: `wren/web_plugin.py`
- Modify: `wren/email_plugin.py`
- Modify: `wren/plugins.py`
- Modify: `tests/test_plugins.py`

**Interfaces:**
- Produces: `plugins.plugin_status() -> list[tuple[str, bool]]` — `[(name, is_active), ...]`, one entry per plugin in `PLUGINS`, in `PLUGINS` order.

- [ ] **Step 1: Write the failing tests**

In `tests/test_plugins.py`, add `import types` is already imported at the top (`import os, asyncio, types`) — no import change needed. Add these tests at the end of the file:

```python
def test_plugin_status_defaults_to_active_true_when_is_active_absent(monkeypatch):
    fake = types.SimpleNamespace(PLUGIN_NAME="Fake Plugin")
    monkeypatch.setattr(plugins, "PLUGINS", [fake])
    assert plugins.plugin_status() == [("Fake Plugin", True)]

def test_plugin_status_calls_is_active_when_present(monkeypatch):
    fake = types.SimpleNamespace(PLUGIN_NAME="Fake Plugin", is_active=lambda: False)
    monkeypatch.setattr(plugins, "PLUGINS", [fake])
    assert plugins.plugin_status() == [("Fake Plugin", False)]

def test_plugin_status_falls_back_to_module_name_when_plugin_name_absent(monkeypatch):
    fake = types.SimpleNamespace()
    fake.__name__ = "wren.fake_plugin"
    monkeypatch.setattr(plugins, "PLUGINS", [fake])
    assert plugins.plugin_status() == [("wren.fake_plugin", True)]

def test_plugin_status_reflects_real_plugins_order_and_names():
    names = [name for name, _ in plugins.plugin_status()]
    assert names == [
        "Notes & Ideas", "Shopping List", "Email Watcher",
        "Reminders", "Web Lookup", "Contacts", "Pins",
    ]

def test_plugin_status_web_plugin_inactive_without_searxng_url(monkeypatch):
    from wren import config
    monkeypatch.setattr(config, "SEARXNG_URL", "")
    status = dict(plugins.plugin_status())
    assert status["Web Lookup"] is False

def test_plugin_status_web_plugin_active_with_searxng_url(monkeypatch):
    from wren import config
    monkeypatch.setattr(config, "SEARXNG_URL", "http://searxng.local")
    status = dict(plugins.plugin_status())
    assert status["Web Lookup"] is True

def test_plugin_status_email_plugin_inactive_without_email_watch(monkeypatch):
    from wren import config
    monkeypatch.setattr(config, "EMAIL_WATCH", {})
    status = dict(plugins.plugin_status())
    assert status["Email Watcher"] is False

def test_plugin_status_email_plugin_active_with_email_watch(monkeypatch):
    from wren import config
    monkeypatch.setattr(config, "EMAIL_WATCH", {"alice@example.com": "owner"})
    status = dict(plugins.plugin_status())
    assert status["Email Watcher"] is True

def test_plugin_status_always_active_plugin_reports_true():
    status = dict(plugins.plugin_status())
    assert status["Notes & Ideas"] is True
    assert status["Shopping List"] is True
    assert status["Reminders"] is True
    assert status["Contacts"] is True
    assert status["Pins"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_plugins.py -k plugin_status -v`
Expected: FAIL (`AttributeError: module 'wren.plugins' has no attribute 'plugin_status'`)

- [ ] **Step 3: Add `PLUGIN_NAME` to each plugin**

In `wren/notes_plugin.py`, after the existing `INTENTS = [...]` line, add:

```python
PLUGIN_NAME = "Notes & Ideas"
```

In `wren/shopping_plugin.py`, after the existing `INTENTS = [...]` line, add:

```python
PLUGIN_NAME = "Shopping List"
```

In `wren/reminder_plugin.py`, after the existing `INTENTS = [...]` line, add:

```python
PLUGIN_NAME = "Reminders"
```

In `wren/contacts_plugin.py`, after the existing `INTENTS = [...]` line, add:

```python
PLUGIN_NAME = "Contacts"
```

In `wren/pins_plugin.py`, after the existing `INTENTS = [...]` line, add:

```python
PLUGIN_NAME = "Pins"
```

In `wren/web_plugin.py`, after the existing `INTENTS = [...]` line, add:

```python
PLUGIN_NAME = "Web Lookup"
```

`wren/email_plugin.py` has no `INTENTS` constant (it's an event-only plugin using `start()`). Add near the top of the file, right after the existing imports:

```python
PLUGIN_NAME = "Email Watcher"
```

- [ ] **Step 4: Add `is_active()` to `web_plugin.py` and `email_plugin.py`**

In `wren/web_plugin.py`, add this function anywhere after the imports (e.g. right after `PROMPT_GUIDELINES`):

```python
def is_active() -> bool:
    return bool(config.SEARXNG_URL)
```

In `wren/email_plugin.py`, add this function anywhere after the imports (e.g. right after `PLUGIN_NAME`):

```python
def is_active() -> bool:
    return bool(config.EMAIL_WATCH)
```

- [ ] **Step 5: Add `plugin_status()` to `wren/plugins.py`**

In `wren/plugins.py`, add this function after the existing `all_guidelines()` function:

```python
def plugin_status() -> list[tuple[str, bool]]:
    return [
        (getattr(p, "PLUGIN_NAME", p.__name__), getattr(p, "is_active", lambda: True)())
        for p in PLUGINS
    ]
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_plugins.py -v`
Expected: PASS (all tests in the file, including the 9 new ones)

- [ ] **Step 7: Run the full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: PASS (no regressions)

- [ ] **Step 8: Commit**

```bash
git add wren/notes_plugin.py wren/shopping_plugin.py wren/reminder_plugin.py wren/contacts_plugin.py wren/pins_plugin.py wren/web_plugin.py wren/email_plugin.py wren/plugins.py tests/test_plugins.py
git commit -m "feat: add PLUGIN_NAME/is_active and plugins.plugin_status()"
```

---

### Task 2: `list_plugins` core intent in `bot.py`, prompt update

**Files:**
- Modify: `wren/bot.py`
- Modify: `wren/brain.py`

**Interfaces:**
- Consumes: `plugins.plugin_status() -> list[tuple[str, bool]]` (Task 1)

- [ ] **Step 1: Update `wren/brain.py`'s intent enum and guidelines**

In `wren/brain.py`, find:

```python
  "intent": "send_to_person" | "help" | "status" | {plugin_intents} | "chat",
```

Change to:

```python
  "intent": "send_to_person" | "help" | "status" | "list_plugins" | {plugin_intents} | "chat",
```

Find the `Guidelines:` section's bullets:

```python
Guidelines:
- send_to_person: user wants to send a message or note to someone
- help: user wants to know what Wren can do, asks for help, or asks to see available commands
- status: user wants to know Wren's operational status — active LLM backend/model/endpoint, uptime, token usage
{plugin_guidelines}
```

Change to:

```python
Guidelines:
- send_to_person: user wants to send a message or note to someone
- help: user wants to know what Wren can do, asks for help, or asks to see available commands
- status: user wants to know Wren's operational status — active LLM backend/model/endpoint, uptime, token usage
- list_plugins: user wants to know what plugins/capabilities Wren currently has active (e.g. "what plugins do you have", "what's active")
{plugin_guidelines}
```

- [ ] **Step 2: Add the `list_plugins` branch to `wren/bot.py`**

In `wren/bot.py`, find the `status` branch:

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

Add a new branch immediately after it (still inside the `if intent in plugins.INTENT_HANDLERS: ... elif intent == "help": ... elif intent == "status": ...` chain):

```python
        elif intent == "list_plugins":
            lines = [f"{'✅' if active else '⏸️'} {name}" for name, active in plugins.plugin_status()]
            await message.channel.send("\n".join(lines))
```

- [ ] **Step 3: Run the full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: PASS (no regressions — `bot.py` has no automated test coverage in this repo, module-level `client.run()` makes it unimportable without a real token, pre-existing condition unrelated to this change; this step confirms Task 1's tests still pass)

- [ ] **Step 4: Commit**

```bash
git add wren/bot.py wren/brain.py
git commit -m "feat: add list_plugins core intent"
```

## Post-plan manual check

`bot.py`'s dispatch branch has no automated test coverage. After both tasks land, manually verify against a running bot instance:
1. DM: "what plugins do you have" → confirms a reply listing all 7 plugins with ✅/⏸️ markers.
2. With `SEARXNG_URL`/`EMAIL_WATCH` unset, confirm "Web Lookup" and "Email Watcher" show ⏸️ while the rest show ✅.
