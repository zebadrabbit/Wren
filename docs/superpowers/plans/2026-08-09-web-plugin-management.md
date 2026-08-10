# Web Plugin Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an owner-only plugins panel to Wren's web chat that shows every skill and channel with the reason any is inactive, toggles skills on and off, and edits the non-secret settings.

**Architecture:** A new SQLite key/value store (`wren/settings.py`) holds only deviations from `.env`. `wren/config.py` gains a `SETTABLE` allowlist, validation, and an `apply_overrides()` that `setattr`s onto the config module — which is the entire live-apply mechanism, because every consumer reads `config.X` at call time. `wren/registry.py` learns enabled-ness and re-registers the intent list with `brain`. `webchat.py` exposes three thin HTTP routes over all of it and contains no domain logic.

**Tech Stack:** Python 3.12, stdlib `sqlite3`, `aiohttp`, `pytest`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-09-web-plugin-management-design.md`

## Global Constraints

- **No new dependencies.** stdlib and what is already in `requirements.txt`.
- **Hard rule 1:** a skill never imports a transport library; it only touches `ctx.channel`.
- **Hard rule 2:** a communication plugin contains no domain logic. `webchat.py` authenticates, checks ownership, calls into `config`/`registry`, and returns JSON. Nothing else.
- **`SETTABLE` is a positive allowlist.** No credential may ever be added to it. `GET /api/plugins` returns setting values and is safe only because of this.
- **Never run an ad-hoc script against the real `wren.db`.** Set `WREN_DB=$(mktemp -d)/scratch.db` before importing anything from `wren`. `pytest` is safe by construction — `tests/conftest.py` sets `WREN_DB` per test.
- **Comments explain *why*,** especially non-obvious ordering. Deliberate simplifications get a `# ponytail:` comment naming the ceiling and the upgrade path.
- **Test style:** build a fake, call the thing, assert on plain values. No Discord mocks. Use `CollectingChannel` and `Ctx` for skill-level tests.
- **Owner id is `config.WHITELIST["owner"]`** (an `int`). There is no `config.OWNER_ID` attribute.
- Run the full suite with `pytest -q` before each commit. It is green at **557 passed** before this plan starts.

---

## File Structure

| File | Responsibility |
|---|---|
| `wren/settings.py` | **new** — SQLite key/value store. Persistence only, no policy. |
| `wren/config.py` | `SETTABLE` allowlist, coercers, `_DEFAULTS` snapshot, `apply_overrides()`, `set_override()`, `clear_override()`. |
| `wren/router.py` | one accessor, `surface(name)`, so `NOTIFY_VIA` validation need not reach into `_surfaces`. |
| `wren/registry.py` | `is_enabled()` / `set_enabled()`; enabled-aware `all_intents()` / `all_guidelines()`; re-registers with `brain`. |
| `wren/core.py` | one dispatch guard so a disabled skill's intent falls through to chat. |
| `wren/skills/web_skill.py`, `wren/communication/{gmail,github,telegram}_plugin.py` | optional `inactive_reason()`. |
| `wren/communication/webchat.py` | three routes + channel discovery helper. |
| `wren/communication/chat.html` | the slide-over panel. |
| `wren/run.py` | `settings` in `_STORAGE`; `config.apply_overrides()` between `init_dbs()` and plugin start. |
| `tests/test_settings.py`, `tests/test_config_overrides.py`, `tests/test_registry_enabled.py`, `tests/test_webchat_plugins.py` | **new** test modules. |

---

### Task 1: The settings store

**Files:**
- Create: `wren/settings.py`
- Modify: `wren/run.py:9-17` (imports and `_STORAGE`)
- Test: `tests/test_settings.py`

**Interfaces:**
- Consumes: `wren.db.conn()`
- Produces: `settings.init_db()`, `settings.get(key) -> str | None`, `settings.set(key, value) -> None`, `settings.unset(key) -> bool`, `settings.all() -> dict[str, str]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_settings.py`:

```python
from wren import settings


def test_get_returns_none_for_unset_key():
    settings.init_db()
    assert settings.get("TIMEZONE") is None


def test_set_then_get_round_trips():
    settings.init_db()
    settings.set("TIMEZONE", "America/Chicago")
    assert settings.get("TIMEZONE") == "America/Chicago"


def test_set_twice_overwrites_rather_than_erroring():
    settings.init_db()
    settings.set("TIMEZONE", "UTC")
    settings.set("TIMEZONE", "America/Chicago")
    assert settings.get("TIMEZONE") == "America/Chicago"


def test_unset_removes_the_row_and_reports_it():
    settings.init_db()
    settings.set("TIMEZONE", "UTC")
    assert settings.unset("TIMEZONE") is True
    assert settings.get("TIMEZONE") is None


def test_unset_reports_false_when_there_was_no_row():
    settings.init_db()
    assert settings.unset("TIMEZONE") is False


def test_all_returns_only_stored_deviations():
    settings.init_db()
    assert settings.all() == {}
    settings.set("TIMEZONE", "UTC")
    settings.set("SEARXNG_URL", "http://searx.lan")
    assert settings.all() == {"TIMEZONE": "UTC", "SEARXNG_URL": "http://searx.lan"}


def test_init_db_is_idempotent():
    settings.init_db()
    settings.set("TIMEZONE", "UTC")
    settings.init_db()
    assert settings.get("TIMEZONE") == "UTC"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_settings.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'wren.settings'`

- [ ] **Step 3: Write minimal implementation**

Create `wren/settings.py`:

```python
from datetime import datetime, timezone

from . import db

# Runtime-editable settings, as key -> raw string. Deliberately shaped like
# contacts.py, the other runtime-mutable store in this codebase.
#
# Only DEVIATIONS from .env live here. A missing row means "whatever .env
# said", which is what makes unset() mean *revert to the file* rather than
# *set to empty* -- and what makes a fresh install behave exactly as before.


def init_db() -> None:
    with db.conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)


def get(key: str) -> str | None:
    with db.conn() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set(key: str, value: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with db.conn() as con:
        con.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, str(value), ts),
        )


def unset(key: str) -> bool:
    with db.conn() as con:
        cur = con.execute("DELETE FROM settings WHERE key=?", (key,))
        return cur.rowcount > 0


def all() -> dict[str, str]:
    with db.conn() as con:
        rows = con.execute("SELECT key, value FROM settings").fetchall()
    return {key: value for key, value in rows}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_settings.py`
Expected: PASS, 7 passed

- [ ] **Step 5: Register the table at boot**

In `wren/run.py`, add the import alongside the other stores (after line 13's `from .skills import shopping_store as shopping`):

```python
from . import settings
```

and add `settings` to the `_STORAGE` tuple:

```python
_STORAGE = (notes, shopping, reminders, contacts, github_state, pins, conversations, settings)
```

- [ ] **Step 6: Run the full suite**

Run: `pytest -q`
Expected: PASS, 564 passed

- [ ] **Step 7: Commit**

```bash
git add wren/settings.py wren/run.py tests/test_settings.py
git commit -m "feat(settings): SQLite key/value store for runtime settings"
```

---

### Task 2: SETTABLE allowlist, validation, and live apply

**Files:**
- Modify: `wren/config.py` (add `logging` import at top; append the new block after line 135, `FIRECRAWL_API_KEY`)
- Modify: `wren/router.py` (add `surface()` accessor after `registered()`, line 15)
- Modify: `wren/run.py` (call `config.apply_overrides()` in `main()`)
- Test: `tests/test_config_overrides.py`

**Interfaces:**
- Consumes: `settings.all/get/set/unset` from Task 1; `config._validate_timezone`, `config._parse_github_watch`, `config._parse_email_watch`; `router.registered()`
- Produces: `config.SETTABLE: dict[str, Callable[[str], object]]`, `config.apply_overrides() -> None`, `config.set_override(key, raw) -> object`, `config.clear_override(key) -> None`, `router.surface(name) -> object | None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_config_overrides.py`:

```python
import pytest

from wren import config
from wren import router
from wren import settings


def test_secrets_are_not_settable():
    # The security boundary. SETTABLE is a positive allowlist; if a credential
    # ever appears in it, GET /api/plugins leaks it.
    for secret in ("DISCORD_TOKEN", "TELEGRAM_TOKEN", "IMAP_PASSWORD",
                   "GITHUB_TOKEN", "OPENAI_API_KEY", "WREN_TOKENS"):
        assert secret not in config.SETTABLE


def test_set_override_rejects_a_key_outside_the_allowlist():
    settings.init_db()
    with pytest.raises(KeyError):
        config.set_override("DISCORD_TOKEN", "hunter2")
    assert settings.get("DISCORD_TOKEN") is None


def test_set_override_applies_immediately_to_the_module():
    settings.init_db()
    config.set_override("SEARXNG_URL", "http://searx.lan")
    try:
        assert config.SEARXNG_URL == "http://searx.lan"
        assert settings.get("SEARXNG_URL") == "http://searx.lan"
    finally:
        config.clear_override("SEARXNG_URL")


def test_clear_override_restores_the_env_default():
    settings.init_db()
    original = config.SEARXNG_URL
    config.set_override("SEARXNG_URL", "http://searx.lan")
    config.clear_override("SEARXNG_URL")
    assert config.SEARXNG_URL == original
    assert settings.get("SEARXNG_URL") is None


def test_poll_intervals_are_coerced_to_int():
    settings.init_db()
    config.set_override("REMINDER_POLL_SECONDS", "45")
    try:
        assert config.REMINDER_POLL_SECONDS == 45
    finally:
        config.clear_override("REMINDER_POLL_SECONDS")


@pytest.mark.parametrize("bad", ["0", "1", "4", "-10"])
def test_poll_intervals_below_the_floor_are_rejected(bad):
    # A 0 would spin the reminder loop hot and hammer IMAP and GitHub's rate
    # limit. This is the sharpest footgun in the editable set.
    settings.init_db()
    with pytest.raises(ValueError):
        config.set_override("REMINDER_POLL_SECONDS", bad)
    assert settings.get("REMINDER_POLL_SECONDS") is None


def test_a_rejected_value_is_not_persisted_and_does_not_change_config():
    settings.init_db()
    original = config.TIMEZONE
    with pytest.raises((ValueError, RuntimeError)):
        config.set_override("TIMEZONE", "Mars/Olympus_Mons")
    assert config.TIMEZONE == original
    assert settings.get("TIMEZONE") is None


def test_github_watch_is_parsed_into_a_list():
    settings.init_db()
    config.set_override("GITHUB_WATCH", "a/b, c/d")
    try:
        assert config.GITHUB_WATCH == ["a/b", "c/d"]
    finally:
        config.clear_override("GITHUB_WATCH")


def test_notify_via_must_name_a_running_channel():
    settings.init_db()
    router.reset()
    with pytest.raises(ValueError):
        config.set_override("NOTIFY_VIA", "telegram")


def test_notify_via_rejects_a_send_only_channel():
    settings.init_db()
    router.reset()

    class SendOnly:
        CAN_NOTIFY = False

    router.register("http", SendOnly())
    try:
        with pytest.raises(ValueError):
            config.set_override("NOTIFY_VIA", "http")
    finally:
        router.reset()


def test_notify_via_accepts_a_channel_that_can_push():
    settings.init_db()
    router.reset()

    class Pusher:
        CAN_NOTIFY = True

    router.register("discord", Pusher())
    try:
        config.set_override("NOTIFY_VIA", "discord")
        assert config.NOTIFY_VIA == "discord"
    finally:
        config.clear_override("NOTIFY_VIA")
        router.reset()


def test_apply_overrides_reads_stored_rows_onto_the_module():
    settings.init_db()
    settings.set("SEARXNG_URL", "http://from-db")
    try:
        config.apply_overrides()
        assert config.SEARXNG_URL == "http://from-db"
    finally:
        settings.unset("SEARXNG_URL")
        config.apply_overrides()


def test_apply_overrides_ignores_an_unknown_key_instead_of_crashing(caplog):
    # An upgrade may drop a setting. Wren must still boot on a row it wrote.
    settings.init_db()
    settings.set("SETTING_FROM_THE_FUTURE", "x")
    try:
        config.apply_overrides()
        assert "SETTING_FROM_THE_FUTURE" in caplog.text
    finally:
        settings.unset("SETTING_FROM_THE_FUTURE")


def test_apply_overrides_ignores_a_stored_value_that_no_longer_validates(caplog):
    settings.init_db()
    settings.set("REMINDER_POLL_SECONDS", "0")
    original = config.REMINDER_POLL_SECONDS
    try:
        config.apply_overrides()
        assert config.REMINDER_POLL_SECONDS == original
        assert "REMINDER_POLL_SECONDS" in caplog.text
    finally:
        settings.unset("REMINDER_POLL_SECONDS")
        config.apply_overrides()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_config_overrides.py`
Expected: FAIL — `AttributeError: module 'wren.config' has no attribute 'SETTABLE'`

- [ ] **Step 3: Add the router accessor**

In `wren/router.py`, directly after `registered()` (line 13-15):

```python
def surface(name: str):
    """The registered surface for a name, or None. Exists so callers can check
    a channel's CAN_NOTIFY without reaching into _surfaces."""
    return _surfaces.get(name)
```

- [ ] **Step 4: Write the config implementation**

Add `import logging` to the top of `wren/config.py` (after `import os`).

Append to the end of `wren/config.py`:

```python
# ── Runtime-editable settings ────────────────────────────────────────────────
# Everything above is read from .env at import. These may additionally be
# overridden at runtime from the settings table, by the owner, through the web
# chat's plugins panel.
#
# SETTABLE is a POSITIVE ALLOWLIST and that is the whole security model: no
# request can reach a name that is not in here, so DISCORD_TOKEN and friends
# are unreachable by construction. GET /api/plugins returns these VALUES, so
# adding a credential to this dict would leak it. Do not.


def _coerce_poll_seconds(raw: str) -> int:
    value = int(raw)
    if value < 5:
        # A 0 spins the poll loop as fast as the CPU allows, and hammers IMAP
        # and GitHub's rate limit with it.
        raise ValueError("poll interval must be at least 5 seconds")
    return value


def _coerce_notify_via(raw: str) -> str:
    from . import router          # local: router imports config, so not at module level

    name = raw.strip()
    surface = router.surface(name)
    if surface is None:
        known = ", ".join(router.registered()) or "none running"
        raise ValueError(f"'{name}' is not a running communication plugin ({known})")
    if not getattr(surface, "CAN_NOTIFY", True):
        raise ValueError(f"'{name}' is send-only and cannot deliver unprompted messages")
    return name


SETTABLE = {
    "TIMEZONE": _validate_timezone,
    "REMINDER_POLL_SECONDS": _coerce_poll_seconds,
    "EMAIL_POLL_SECONDS": _coerce_poll_seconds,
    "GITHUB_POLL_SECONDS": _coerce_poll_seconds,
    "SEARXNG_URL": str.strip,
    "FIRECRAWL_URL": str.strip,
    "GITHUB_WATCH": _parse_github_watch,
    "EMAIL_WATCH": _parse_email_watch,
    "NOTIFY_VIA": _coerce_notify_via,
}

# Snapshot of what .env produced, taken before any override is applied, so
# clear_override() can restore the file's value exactly rather than trying to
# re-derive it.
_DEFAULTS = {key: globals()[key] for key in SETTABLE}


def apply_overrides() -> None:
    """Read the settings table onto this module.

    This is the entire live-apply mechanism, and it works only because every
    consumer reads `config.X` at call time -- including the
    `asyncio.sleep(config.REMINDER_POLL_SECONDS)` inside the reminder loop.
    Nothing in wren/ captures a config value into a module-level constant at
    import. If that ever changes, live edits silently stop working for whatever
    got captured.
    """
    from . import settings

    for key, raw in settings.all().items():
        coerce = SETTABLE.get(key)
        if coerce is None:
            # A version that no longer knows this key must still boot -- the
            # row was written by an older Wren, and crashing on it would make
            # the upgrade unrecoverable without hand-editing the database.
            logging.warning(f"ignoring unknown stored setting '{key}'")
            continue
        try:
            globals()[key] = coerce(raw)
        except (ValueError, RuntimeError) as e:
            logging.warning(f"ignoring invalid stored setting '{key}'={raw!r}: {e}")


def set_override(key: str, raw: str):
    """Validate, persist, then apply -- in that order.

    A rejected value never reaches the database, and a failed write never
    leaves memory ahead of disk. Coercion happens during validation, so the
    value is already known-good by the time it is applied.
    """
    from . import settings

    if key not in SETTABLE:
        raise KeyError(key)
    value = SETTABLE[key](raw)       # raises ValueError/RuntimeError if bad
    settings.set(key, raw)
    globals()[key] = value
    return value


def clear_override(key: str) -> None:
    """Drop the stored row and restore what .env produced at import."""
    from . import settings

    if key not in SETTABLE:
        raise KeyError(key)
    settings.unset(key)
    globals()[key] = _DEFAULTS[key]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest -q tests/test_config_overrides.py`
Expected: PASS, 15 passed

- [ ] **Step 6: Wire apply_overrides into boot**

In `wren/run.py`'s `main()`, immediately after `init_dbs()`:

```python
async def main() -> None:
    init_dbs()
    # After init_dbs (the settings table must exist) and before any plugin
    # starts (or a plugin reads a stale value during startup). The ordering
    # looks arbitrary and is not.
    config.apply_overrides()
```

- [ ] **Step 7: Run the full suite**

Run: `pytest -q`
Expected: PASS, 579 passed

- [ ] **Step 8: Commit**

```bash
git add wren/config.py wren/router.py wren/run.py tests/test_config_overrides.py
git commit -m "feat(config): SETTABLE allowlist with live-applied overrides"
```

---

### Task 3: Enabled-ness in the registry

**Files:**
- Modify: `wren/registry.py:11-43`
- Test: `tests/test_registry_enabled.py`

**Interfaces:**
- Consumes: `settings.get/set/unset` from Task 1
- Produces: `registry.is_enabled(plugin) -> bool`, `registry.set_enabled(plugin, on: bool) -> None`, `registry.enabled_plugins() -> list`, `registry.skill_key(plugin) -> str`

- [ ] **Step 1: Write the failing test**

Create `tests/test_registry_enabled.py`:

```python
from wren import registry
from wren import settings
from wren.skills import notes_skill
from wren.skills import web_skill


def test_a_skill_with_no_stored_row_is_enabled():
    settings.init_db()
    assert registry.is_enabled(notes_skill) is True


def test_skill_key_is_the_module_basename():
    assert registry.skill_key(notes_skill) == "notes_skill"


def test_set_enabled_false_is_readable_back():
    settings.init_db()
    registry.set_enabled(notes_skill, False)
    try:
        assert registry.is_enabled(notes_skill) is False
    finally:
        registry.set_enabled(notes_skill, True)


def test_disabled_skill_drops_out_of_all_intents():
    settings.init_db()
    assert "save_note" in registry.all_intents()
    registry.set_enabled(notes_skill, False)
    try:
        assert "save_note" not in registry.all_intents()
    finally:
        registry.set_enabled(notes_skill, True)
    assert "save_note" in registry.all_intents()


def test_disabled_skill_drops_out_of_all_guidelines():
    settings.init_db()
    registry.set_enabled(web_skill, False)
    try:
        assert "web_search" not in registry.all_guidelines()
    finally:
        registry.set_enabled(web_skill, True)


def test_intent_handlers_is_left_intact_when_a_skill_is_disabled():
    # INTENT_HANDLERS stays a plain dict that core indexes and tests assert
    # against; enforcement happens at dispatch, not by mutating it.
    settings.init_db()
    registry.set_enabled(notes_skill, False)
    try:
        assert registry.INTENT_HANDLERS["save_note"] is notes_skill
    finally:
        registry.set_enabled(notes_skill, True)


def test_set_enabled_reregisters_the_intent_list_with_brain():
    # The trap: core.py calls brain.register_plugins() once at import, so the
    # intent list is baked into brain's globals at startup. Without this
    # re-registration a toggle never reaches the LLM at all.
    from wren import brain

    settings.init_db()
    registry.set_enabled(notes_skill, False)
    try:
        assert "save_note" not in brain._plugin_intents
    finally:
        registry.set_enabled(notes_skill, True)
    assert "save_note" in brain._plugin_intents


def test_enabled_plugins_excludes_the_disabled_ones():
    settings.init_db()
    registry.set_enabled(web_skill, False)
    try:
        assert web_skill not in registry.enabled_plugins()
        assert notes_skill in registry.enabled_plugins()
    finally:
        registry.set_enabled(web_skill, True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_registry_enabled.py`
Expected: FAIL — `AttributeError: module 'wren.registry' has no attribute 'is_enabled'`

- [ ] **Step 3: Write the implementation**

In `wren/registry.py`, add `from . import settings` to the imports, then add after the `WATCHERS` block:

```python
def skill_key(plugin) -> str:
    """Settings key for a skill's on/off row.

    The module basename, not PLUGIN_NAME: a display name can be reworded, and
    a stored row must not lose its meaning when it is.
    """
    return plugin.__name__.rsplit(".", 1)[-1]


def is_enabled(plugin) -> bool:
    # Default on: a skill with no row is enabled, so the table records only
    # what the owner changed and a fresh install behaves exactly as before.
    return settings.get(f"skill.{skill_key(plugin)}.enabled") != "0"


def set_enabled(plugin, on: bool) -> None:
    settings.set(f"skill.{skill_key(plugin)}.enabled", "1" if on else "0")
    # core.py registers the intent list with brain ONCE, at import. Without
    # re-registering here the LLM would keep being offered a skill the owner
    # just switched off. Local import: this is the single choke point every
    # writer goes through, so doing it here makes it impossible to forget --
    # and keeps brain out of registry's module-level imports.
    from . import brain

    brain.register_plugins(all_intents(), all_guidelines())


def enabled_plugins() -> list:
    return [p for p in PLUGINS if is_enabled(p)]
```

Then change `all_intents()` and `all_guidelines()` to filter:

```python
def all_intents() -> list[str]:
    return [intent for plugin in enabled_plugins() for intent in getattr(plugin, "INTENTS", [])]

def all_guidelines() -> str:
    return "\n".join(
        text for plugin in enabled_plugins()
        if (text := getattr(plugin, "PROMPT_GUIDELINES", ""))
    )
```

`INTENT_HANDLERS` and `plugin_status()` are deliberately unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_registry_enabled.py`
Expected: PASS, 8 passed

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: PASS, 587 passed

- [ ] **Step 6: Commit**

```bash
git add wren/registry.py tests/test_registry_enabled.py
git commit -m "feat(registry): per-skill enable/disable, re-registered with brain"
```

---

### Task 4: Dispatch guard in core

**Files:**
- Modify: `wren/core.py:126-127`
- Test: `tests/test_core.py` (append)

**Interfaces:**
- Consumes: `registry.is_enabled` from Task 3
- Produces: nothing new; changes behaviour of `core.handle_message`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_core.py`:

```python
def test_disabled_skill_intent_falls_through_to_chat(monkeypatch):
    # A model can still emit an intent it was never offered -- a stale prompt
    # cache, or plain hallucination. Dispatch must refuse it rather than run a
    # skill the owner switched off.
    from wren import registry, settings
    from wren.skills import notes_skill

    settings.init_db()
    monkeypatch.setattr(brain, "detect_intent", lambda *a, **k: {"intent": "save_note", "content": "milk"})
    monkeypatch.setattr(brain, "chat", lambda *a, **k: "chatty reply")
    registry.set_enabled(notes_skill, False)

    channel = CollectingChannel()
    try:
        asyncio.run(core.handle_message(1, "save a note", channel))
    finally:
        registry.set_enabled(notes_skill, True)

    assert channel.sent == ["chatty reply"]
```

Note: `tests/test_core.py` already imports `asyncio`, `brain`, `core`, and `CollectingChannel`, and whitelists user id 1 — follow the existing fixtures in that file rather than adding new ones.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_core.py::test_disabled_skill_intent_falls_through_to_chat -v`
Expected: FAIL — the note is saved and `channel.sent` holds the skill's confirmation, not `"chatty reply"`

- [ ] **Step 3: Write the implementation**

In `wren/core.py`, change line 126:

```python
        # is_enabled as well as membership: all_intents() already stops
        # offering a disabled skill, but a model can emit an intent it was
        # never offered. Treat that as unknown so it falls through to chat.
        if intent in registry.INTENT_HANDLERS and registry.is_enabled(registry.INTENT_HANDLERS[intent]):
            await registry.INTENT_HANDLERS[intent].handle(intent, ctx)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_core.py -v`
Expected: PASS

- [ ] **Step 5: Pin the Reminders decision with a test**

This is the other half of the same decision and the one most likely to be
"fixed" by a later reader who thinks it is a bug. Append to
`tests/test_reminder_plugin.py`, following the existing `run_one_iteration`
pattern already in that file (it patches
`wren.skills.reminder_skill.asyncio.sleep` to raise `CancelledError` after one
pass — reuse it rather than writing a new driver):

```python
def test_disabling_the_skill_does_not_stop_reminders_already_set():
    # Deliberate, and it looks like a bug until you know why: switching
    # Reminders off stops Wren OFFERING it, but a reminder already set for 6pm
    # still arrives. Only the poll loop marks a reminder fired, so a stopped
    # poller would pile them up unfired and then deliver the lot on re-enable --
    # and suppressing the poller while marking them fired would silently
    # destroy something the user explicitly asked for.
    from wren import registry, settings
    from wren.skills import reminder_skill

    settings.init_db()
    reminders.save(1, "check the oven", _past_iso(60))
    registry.set_enabled(reminder_skill, False)

    async def run_one_iteration():
        with patch.object(router, "notify", new=AsyncMock(return_value=True)) as mock_notify, \
             patch("wren.skills.reminder_skill.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
            try:
                await reminder_skill.start()
            except asyncio.CancelledError:
                pass
        return mock_notify

    try:
        mock_notify = asyncio.run(run_one_iteration())
    finally:
        registry.set_enabled(reminder_skill, True)

    assert mock_notify.await_count == 1
```

Use whatever helper that file already has for a past timestamp — if it only
has `_future_iso`, add the mirrored `_past_iso` beside it.

- [ ] **Step 6: Run both tests**

Run: `pytest -q tests/test_core.py tests/test_reminder_plugin.py`
Expected: PASS

- [ ] **Step 7: Run the full suite**

Run: `pytest -q`
Expected: PASS, 589 passed

- [ ] **Step 8: Commit**

```bash
git add wren/core.py tests/test_core.py tests/test_reminder_plugin.py
git commit -m "fix(core): do not dispatch an intent whose skill is disabled"
```

---

### Task 5: Report why a plugin is inactive

**Files:**
- Modify: `wren/skills/web_skill.py` (beside `is_active`, line 15)
- Modify: `wren/communication/gmail_plugin.py` (beside `is_active`, line 12)
- Modify: `wren/communication/github_plugin.py` (beside its `is_active`)
- Modify: `wren/communication/telegram_plugin.py` (add both)
- Test: `tests/test_registry_enabled.py` (append)

**Interfaces:**
- Produces: optional module-level `inactive_reason() -> str` on those four modules

- [ ] **Step 1: Write the failing test**

Append to `tests/test_registry_enabled.py`:

```python
def test_web_skill_explains_why_it_is_inactive(monkeypatch):
    from wren import config

    monkeypatch.setattr(config, "SEARXNG_URL", "")
    assert web_skill.is_active() is False
    assert "SEARXNG_URL" in web_skill.inactive_reason()


def test_gmail_explains_why_it_is_inactive(monkeypatch):
    from wren import config
    from wren.communication import gmail_plugin

    monkeypatch.setattr(config, "EMAIL_WATCH", {})
    assert gmail_plugin.is_active() is False
    assert "EMAIL_WATCH" in gmail_plugin.inactive_reason()


def test_telegram_explains_that_it_needs_a_token(monkeypatch):
    from wren import config
    from wren.communication import telegram_plugin

    monkeypatch.setattr(config, "TELEGRAM_TOKEN", "")
    assert telegram_plugin.is_active() is False
    assert "TELEGRAM_TOKEN" in telegram_plugin.inactive_reason()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_registry_enabled.py -k inactive`
Expected: FAIL — `AttributeError: module 'wren.skills.web_skill' has no attribute 'inactive_reason'`

- [ ] **Step 3: Write the implementation**

`wren/skills/web_skill.py`, beside `is_active()`:

```python
def inactive_reason() -> str:
    return "SEARXNG_URL is not set — web search and page reading are disabled."
```

`wren/communication/gmail_plugin.py`:

```python
def inactive_reason() -> str:
    return "EMAIL_WATCH is empty — no senders are being watched."
```

`wren/communication/github_plugin.py`:

```python
def inactive_reason() -> str:
    return "GITHUB_WATCH is empty — no repositories are being watched."
```

`wren/communication/telegram_plugin.py` (it has neither yet):

```python
def is_active() -> bool:
    return bool(config.TELEGRAM_TOKEN)


def inactive_reason() -> str:
    return ("TELEGRAM_TOKEN is not set. Add it and put 'telegram' in "
            "COMMUNICATION_PLUGINS, then restart.")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_registry_enabled.py`
Expected: PASS, 11 passed

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: PASS, 591 passed

- [ ] **Step 6: Commit**

```bash
git add wren/skills/web_skill.py wren/communication/gmail_plugin.py wren/communication/github_plugin.py wren/communication/telegram_plugin.py tests/test_registry_enabled.py
git commit -m "feat: plugins explain why they are inactive, not just that they are"
```

---

### Task 6: The HTTP endpoints

**Files:**
- Modify: `wren/communication/webchat.py` (add helper + three routes; register them in `register_routes`, line 189-195)
- Test: `tests/test_webchat_plugins.py`

**Interfaces:**
- Consumes: everything from Tasks 1-5
- Produces: `GET /api/plugins`, `PATCH /api/plugins/{module}`, `PATCH /api/settings`; helper `_channel_rows() -> list[dict]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_webchat_plugins.py`. This mirrors the harness in `tests/test_webchat.py` exactly — the same env preamble, the same `call()` wrapper around `TestClient(TestServer(http_surface.build_app()))`, and the same `tokens` autouse fixture. `OWNER_ID` is `"1"` there, so **`TOKEN_A` is the owner and `TOKEN_B` is the non-owner.**

```python
import os, asyncio, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from aiohttp.test_utils import TestClient, TestServer

from wren import config
from wren import registry
from wren import settings
from wren.communication import http_plugin as http_surface
from wren.communication import webchat

TOKEN_OWNER, USER_OWNER = "tok-a", 1          # matches OWNER_ID above
TOKEN_OTHER, USER_OTHER = "tok-b", 2


@pytest.fixture(autouse=True)
def tokens(monkeypatch, isolated_db):
    # isolated_db (conftest) must run first so init_db lands on the tmp file
    monkeypatch.setattr(config, "WREN_TOKENS", {TOKEN_OWNER: USER_OWNER, TOKEN_OTHER: USER_OTHER})
    monkeypatch.setattr(config, "id_to_name", lambda: {USER_OWNER: "ann", USER_OTHER: "bob"})
    settings.init_db()


async def _request(method, path, *, token=None, **kw):
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    async with TestClient(TestServer(http_surface.build_app())) as client:
        resp = await getattr(client, method)(path, headers=headers, **kw)
        return resp.status, await resp.json()


def call(method, path, **kw):
    return asyncio.run(_request(method, path, **kw))


def test_owner_can_read_the_plugin_list():
    status, body = call("get", "/api/plugins", token=TOKEN_OWNER)
    assert status == 200
    assert {"skills", "channels", "settings"} <= set(body)
    assert any(s["module"] == "notes_skill" for s in body["skills"])


def test_a_non_owner_is_refused():
    status, _ = call("get", "/api/plugins", token=TOKEN_OTHER)
    assert status == 403


def test_no_token_is_unauthorized():
    status, _ = call("get", "/api/plugins")
    assert status == 401


def test_settings_payload_contains_no_credential():
    # Pairs with the SETTABLE allowlist test: this endpoint returns values, so
    # the allowlist is the only thing keeping secrets out of the response.
    _, body = call("get", "/api/plugins", token=TOKEN_OWNER)
    assert "DISCORD_TOKEN" not in body["settings"]


def test_channels_include_one_that_is_not_enabled():
    # The case the panel exists to show: Telegram is present in the package but
    # absent from COMMUNICATION_PLUGINS.
    rows = webchat._channel_rows()
    telegram = next(r for r in rows if r["module"] == "telegram_plugin")
    assert telegram["running"] is False
    assert "TELEGRAM_TOKEN" in telegram["reason"]


def test_owner_can_disable_a_skill():
    try:
        status, _ = call("patch", "/api/plugins/notes_skill",
                         token=TOKEN_OWNER, json={"enabled": False})
        assert status == 200
        assert "save_note" not in registry.all_intents()
    finally:
        settings.unset("skill.notes_skill.enabled")
        from wren.skills import notes_skill
        registry.set_enabled(notes_skill, True)


def test_a_non_owner_cannot_disable_a_skill():
    status, _ = call("patch", "/api/plugins/notes_skill",
                     token=TOKEN_OTHER, json={"enabled": False})
    assert status == 403
    assert settings.get("skill.notes_skill.enabled") is None


def test_toggling_a_channel_is_refused():
    # Channels need a restart. A toggle that silently does nothing is worse
    # than no toggle.
    status, _ = call("patch", "/api/plugins/telegram_plugin",
                     token=TOKEN_OWNER, json={"enabled": True})
    assert status == 400


def test_an_unknown_module_is_a_404():
    status, _ = call("patch", "/api/plugins/nope_skill",
                     token=TOKEN_OWNER, json={"enabled": False})
    assert status == 404


def test_owner_can_change_a_setting():
    try:
        status, _ = call("patch", "/api/settings",
                         token=TOKEN_OWNER, json={"SEARXNG_URL": "http://searx.lan"})
        assert status == 200
        assert config.SEARXNG_URL == "http://searx.lan"
    finally:
        config.clear_override("SEARXNG_URL")


def test_an_invalid_setting_is_400_and_persists_nothing():
    original = config.REMINDER_POLL_SECONDS
    status, _ = call("patch", "/api/settings",
                     token=TOKEN_OWNER, json={"REMINDER_POLL_SECONDS": "0"})
    assert status == 400
    assert config.REMINDER_POLL_SECONDS == original
    assert settings.get("REMINDER_POLL_SECONDS") is None


def test_a_secret_cannot_be_set_through_the_api():
    status, _ = call("patch", "/api/settings",
                     token=TOKEN_OWNER, json={"DISCORD_TOKEN": "hunter2"})
    assert status == 400
    assert settings.get("DISCORD_TOKEN") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_webchat_plugins.py`
Expected: FAIL — 404 on `/api/plugins`, and `AttributeError` on `webchat._channel_rows`

- [ ] **Step 3: Write the implementation**

In `wren/communication/webchat.py`, add at module level:

```python
import importlib
import pkgutil


def _plugin_row(module, running: bool) -> dict:
    active = getattr(module, "is_active", lambda: True)()
    row = {
        "module": module.__name__.rsplit(".", 1)[-1],
        "name": getattr(module, "PLUGIN_NAME", module.__name__),
        "active": active,
        "running": running,
    }
    if not active:
        reason = getattr(module, "inactive_reason", lambda: "Not configured.")()
        row["reason"] = reason
    return row


def _channel_rows() -> list[dict]:
    """Every communication plugin in the package, enabled or not.

    Discovered rather than read from COMMUNICATION_PLUGINS, because the panel
    has to show the channels you have NOT enabled -- that is the whole point of
    "Telegram: no TELEGRAM_TOKEN". Importing an unenabled module is safe: these
    files define constants and functions at module level and start nothing
    until start() is awaited.
    """
    from .. import communication

    rows = []
    for info in sorted(pkgutil.iter_modules(communication.__path__), key=lambda i: i.name):
        if not info.name.endswith("_plugin"):
            continue
        module = importlib.import_module(f"..communication.{info.name}", __package__)
        name = info.name[: -len("_plugin")]
        row = _plugin_row(module, running=name in config.COMMUNICATION_PLUGINS)
        row["role"] = getattr(module, "ROLE", "chat")
        rows.append(row)
    return rows
```

Then inside `register_routes`, add the handlers and register them:

```python
    def _owner(request):
        user_id = _auth(request)
        if user_id != config.WHITELIST["owner"]:
            raise web.HTTPForbidden(text='{"error": "owner only"}',
                                    content_type="application/json")
        return user_id

    async def get_plugins(request):
        _owner(request)
        from .. import registry
        return web.json_response({
            "skills": [
                dict(_plugin_row(p, running=True), enabled=registry.is_enabled(p))
                for p in registry.PLUGINS
            ],
            "channels": _channel_rows(),
            "settings": {key: str(getattr(config, key)) for key in config.SETTABLE},
        })

    async def patch_plugin(request):
        _owner(request)
        from .. import registry
        module_name = request.match_info["module"]
        body = await request.json()
        target = next((p for p in registry.PLUGINS if registry.skill_key(p) == module_name), None)
        if target is None:
            if any(r["module"] == module_name for r in _channel_rows()):
                return web.json_response(
                    {"error": "channels cannot be toggled here; they are read once at "
                              "startup from COMMUNICATION_PLUGINS and need a restart"},
                    status=400)
            return web.json_response({"error": "unknown plugin"}, status=404)
        registry.set_enabled(target, bool(body.get("enabled")))
        return web.json_response(dict(_plugin_row(target, running=True),
                                      enabled=registry.is_enabled(target)))

    async def patch_settings(request):
        _owner(request)
        body = await request.json()
        for key, value in body.items():
            try:
                config.set_override(key, str(value))
            except KeyError:
                return web.json_response({"error": f"'{key}' is not a settable option"}, status=400)
            except (ValueError, RuntimeError) as e:
                return web.json_response({"error": f"{key}: {e}"}, status=400)
        return web.json_response({key: str(getattr(config, key)) for key in config.SETTABLE})

    app.router.add_get("/api/plugins", get_plugins)
    app.router.add_patch("/api/plugins/{module}", patch_plugin)
    app.router.add_patch("/api/settings", patch_settings)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_webchat_plugins.py`
Expected: PASS, 12 passed

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: PASS, 603 passed

- [ ] **Step 6: Commit**

```bash
git add wren/communication/webchat.py tests/test_webchat_plugins.py
git commit -m "feat(webchat): owner-only plugins and settings endpoints"
```

---

### Task 7: The panel

**Files:**
- Modify: `wren/communication/chat.html` (gear button in the sidebar header at line 134; panel markup after the `#app` div; CSS beside the existing `aside` rules; JS beside the existing `$` helpers)
- Test: `tests/test_chat_renderer.py` (append)

**Interfaces:**
- Consumes: the three endpoints from Task 6

**WARNING — editing `chat.html`:** lines around the markdown renderer contain the private-use escape `` written as a six-character JS escape. The Edit tool can turn a typed `` into a raw U+E000 byte and silently corrupt the placeholder logic. Do not edit those lines. Add the panel's markup, CSS and JS as new blocks, and if you must repair that region, restore it with `git show HEAD:wren/communication/chat.html`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_chat_renderer.py`:

```python
def test_page_has_the_plugins_panel_and_its_gear():
    src = PAGE.read_text(encoding="utf-8")
    assert 'id="gear"' in src
    assert 'id="plugins"' in src
    assert "/api/plugins" in src


def test_panel_wires_every_endpoint_it_needs():
    src = PAGE.read_text(encoding="utf-8")
    for fragment in ('"/api/settings"', '"/api/plugins"', "/api/plugins/${s.module}"):
        assert fragment in src, f"panel never calls {fragment}"


def test_panel_renders_server_text_without_building_html():
    # Plugin names and inactive reasons come from the server. The page's
    # standing rule is escape-first, never build HTML from a value -- so these
    # go in via textContent, not interpolation.
    src = PAGE.read_text(encoding="utf-8")
    assert "textContent = s.name" in src
    assert "textContent = c.name" not in src or "innerHTML = `${c.name}" not in src
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_chat_renderer.py -k plugins`
Expected: FAIL — `assert 'id="gear"' in src`

- [ ] **Step 3: Add the gear to the sidebar header**

Change line 134 of `chat.html` from:

```html
    <header><b>Wren</b><button id="new">New</button></header>
```

to:

```html
    <header><b>Wren</b><button id="gear" title="Plugins">⚙</button><button id="new">New</button></header>
```

- [ ] **Step 4: Add the panel markup**

Immediately after the closing `</div>` of `<div id="app">`:

```html
<div id="plugins" hidden>
  <div class="sheet">
    <header><b>Plugins</b><button id="pclose">✕</button></header>
    <div class="pbody">
      <h4>Skills</h4><div id="pskills"></div>
      <h4>Channels <small>restart required</small></h4><div id="pchannels"></div>
      <h4>Settings</h4><div id="psettings"></div>
    </div>
    <div class="perr" id="perr"></div>
  </div>
</div>
```

- [ ] **Step 5: Add the CSS**

Beside the existing `aside` rules:

```css
#plugins { position: fixed; inset: 0; background: rgba(0,0,0,.35); display: flex; justify-content: flex-end; }
#plugins[hidden] { display: none; }
#plugins .sheet {
  width: 420px; max-width: 100%; background: var(--bg); height: 100%;
  display: flex; flex-direction: column; border-left: 1px solid var(--line);
}
#plugins header {
  display: flex; align-items: center; padding: 12px 16px;
  border-bottom: 1px solid var(--line);
}
#plugins header b { flex: 1; }
#plugins .pbody { flex: 1; overflow-y: auto; padding: 16px; }
#plugins h4 { margin: 18px 0 8px; font-size: 12px; letter-spacing: .08em;
              text-transform: uppercase; color: var(--muted); }
#plugins h4 small { text-transform: none; letter-spacing: 0; font-weight: 400; }
.prow { display: flex; align-items: center; gap: 8px; padding: 8px 0;
        border-bottom: 1px solid var(--line); }
.prow .pname { flex: 1; }
.prow .why { flex-basis: 100%; color: var(--muted); font-size: 13px; }
.pset { display: flex; align-items: center; gap: 8px; padding: 6px 0; }
.pset label { flex: 1; font-size: 14px; }
.pset input { width: 190px; padding: 6px 8px; border: 1px solid var(--line);
              border-radius: 6px; background: var(--panel); color: var(--text); font: inherit; }
.perr { color: #c0503f; font-size: 13px; padding: 0 16px 12px; min-height: 18px; }
```

- [ ] **Step 6: Add the JS**

Append inside the existing `<script>`, using the same `api()` helper the page already uses for authenticated requests (read it first and match its signature):

```js
async function loadPlugins() {
  const d = await api("GET", "/api/plugins");
  $("#pskills").innerHTML = "";
  d.skills.forEach(s => {
    const row = document.createElement("div");
    row.className = "prow";
    row.innerHTML = `<span class="pname"></span>
                     <button class="ptoggle">${s.enabled ? "on" : "off"}</button>`;
    row.querySelector(".pname").textContent = s.name;
    row.querySelector(".ptoggle").onclick = async () => {
      await api("PATCH", `/api/plugins/${s.module}`, {enabled: !s.enabled});
      loadPlugins();
    };
    if (s.reason) {
      const why = document.createElement("div");
      why.className = "why";
      why.textContent = s.reason;
      row.appendChild(why);
    }
    $("#pskills").appendChild(row);
  });

  $("#pchannels").innerHTML = "";
  d.channels.forEach(c => {
    const row = document.createElement("div");
    row.className = "prow";
    row.innerHTML = `<span class="pname"></span><span>${c.running ? "● live" : "○ off"}</span>`;
    row.querySelector(".pname").textContent = `${c.name} (${c.role})`;
    if (c.reason) {
      const why = document.createElement("div");
      why.className = "why";
      why.textContent = c.reason;
      row.appendChild(why);
    }
    $("#pchannels").appendChild(row);
  });

  $("#psettings").innerHTML = "";
  Object.entries(d.settings).forEach(([key, value]) => {
    const row = document.createElement("div");
    row.className = "pset";
    row.innerHTML = `<label></label><input>`;
    row.querySelector("label").textContent = key;
    const input = row.querySelector("input");
    input.value = value;
    input.onchange = async () => {
      $("#perr").textContent = "";
      try {
        await api("PATCH", "/api/settings", {[key]: input.value});
      } catch (e) {
        $("#perr").textContent = String(e.message || e);
      }
      loadPlugins();
    };
    $("#psettings").appendChild(row);
  });
}

$("#gear").onclick = () => { $("#plugins").hidden = false; loadPlugins(); };
$("#pclose").onclick = () => { $("#plugins").hidden = true; };
$("#plugins").onclick = e => { if (e.target.id === "plugins") $("#plugins").hidden = true; };
```

Note: `textContent` rather than string interpolation for every server-supplied value — plugin names and reasons are trusted here, but the page's existing rule is to escape first and never build HTML from a value.

- [ ] **Step 7: Hide the gear from non-owners**

`GET /api/plugins` already 403s for non-owners. In `loadPlugins`'s failure path, hide the gear rather than showing an error, and call it once on sign-in so a non-owner never sees the button:

```js
$("#gear").hidden = true;
api("GET", "/api/plugins").then(() => { $("#gear").hidden = false; }).catch(() => {});
```

Place this alongside the existing post-sign-in initialisation.

- [ ] **Step 8: Run the tests**

Run: `pytest -q tests/test_chat_renderer.py tests/test_webchat.py`
Expected: PASS

- [ ] **Step 9: Run the full suite**

Run: `pytest -q`
Expected: PASS, 605 passed

- [ ] **Step 10: Manual smoke test**

Never against the real database or the live port:

```bash
source venv/bin/activate
WREN_DB=$(mktemp -d)/panel.db COMMUNICATION_PLUGINS=http NOTIFY_VIA=http \
WREN_TOKENS=paneltest:1 OWNER_ID=1 WREN_HTTP_PORT=8799 WREN_HTTP_HOST=127.0.0.1 \
python -m wren.run
```

Open `http://127.0.0.1:8799/`, paste `paneltest`, click the gear. Confirm: skills list with toggles; Telegram shown as off with its reason; switching Web Lookup off then asking "search the web for badgers" falls through to chat. Stop it by recorded PID — **not** `pkill -f`, which also matches the shell that launched it.

- [ ] **Step 11: Commit**

```bash
git add wren/communication/chat.html tests/test_chat_renderer.py
git commit -m "feat(webchat): plugins panel"
```

---

### Task 8: Documentation

**Files:**
- Modify: `README.md` (Web chat section — add the panel and its route table rows)
- Modify: `CLAUDE.md` (note the settings store as the second runtime-mutable state)

**Interfaces:** none

- [ ] **Step 1: Update the README**

In the Web chat route table, add:

```markdown
| `GET /api/plugins` | — | `{skills, channels, settings}` — owner only |
| `PATCH /api/plugins/{module}` | `{enabled}` | toggle a skill — owner only |
| `PATCH /api/settings` | `{KEY: value}` | change a non-secret setting — owner only |
```

And a short subsection under Web chat:

```markdown
### The plugins panel

The gear beside "New" opens an owner-only panel: every skill and channel with
the reason any of them is inactive, switches for the skills, and the settings
that are not credentials.

Credentials are deliberately absent. The bearer token buys a chat window; it
must not also buy `DISCORD_TOKEN`. Channels are read-only status because
`COMMUNICATION_PLUGINS` is read once at startup — enabling Telegram is still
two lines in `.env` and a restart.

Changes apply immediately and persist in a `settings` table that overrides
`.env`. Wren never writes to `.env`.
```

- [ ] **Step 2: Update CLAUDE.md**

Under the hard rules, extend rule 6's neighbourhood with:

```markdown
7. **Runtime-mutable state is `contacts` and `settings`, nothing else.**
   Everything else comes from `.env` and is read at import. `settings` stores
   only deviations, and `config.SETTABLE` is a positive allowlist — never add
   a credential to it, because `GET /api/plugins` returns its values.
```

- [ ] **Step 3: Run the full suite**

Run: `pytest -q`
Expected: PASS, 605 passed

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: the plugins panel and the settings allowlist rule"
```

---

### Task 9: Brand the chat window

Added after the plan was written, at the owner's request: the web chat is unbranded — a plain
`<h1>Wren</h1>` on the login screen, a plain `<b>Wren</b>` in the sidebar, and the browser's
default document icon. `Brand/` now ships the marks, so use them.

**Files:**
- Modify: `wren/communication/chat.html` (head, login screen, sidebar header, plus CSS)
- Test: `tests/test_chat_renderer.py` (append)

**Interfaces:** none — this is presentation only. No Python changes, no new routes.

**Source assets** (read them, do not retype the path data by hand):
- `Brand/favicon/favicon.svg` — 64×64, clay rounded square with a cream bird. The favicon.
- `Brand/brand/mark-simple.svg` — 128×128, one clay path, silhouette only. The sidebar.
- `Brand/brand/lockup.svg` — 338×153, mark + wordmark as outlines. The login screen.

**Two rules from the brand system that decide the sizing** (`Brand/README.md`):
- The full mark's wing, eye and supercilium stop resolving below roughly 24 px and read as dirt.
  Anything smaller uses the **simple mark**. The sidebar icon is ~20 px, so it MUST be
  `mark-simple.svg`, not `mark.svg`.
- Clear space is **0.35 × the mark's height** on every side. Nothing intrudes.

**Why inline rather than serve the files:** `chat.html` is one self-contained file with no
external requests — no webfonts, no CDN, nothing to 404. Serving `Brand/` would add a static
route and make the running service depend on a directory that is really a design source. The
SVGs are 0.9–2.2 KB, so inlining costs nothing. The tradeoff is real and must be commented: the
geometry is now duplicated, and if the mark is ever regenerated, `chat.html` goes stale. Name
the source file and the regeneration command in the comment so the next person knows.

**Do NOT re-theme the page.** The chat UI's accent is green (`--accent: #5b6f4e`); the brand is
clay. Making those agree is a separate decision the owner has not asked for. Add the marks, leave
the palette alone.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_chat_renderer.py`:

```python
def test_page_carries_the_wren_favicon_inline():
    src = PAGE.read_text(encoding="utf-8")
    assert 'rel="icon"' in src
    assert "data:image/svg+xml" in src, "favicon must be inline, not a separate request"
    assert 'name="theme-color"' in src


def test_login_screen_shows_the_lockup():
    src = PAGE.read_text(encoding="utf-8")
    assert 'id="brandmark"' in src


def test_sidebar_uses_the_simple_mark_not_the_full_one():
    # Below ~24px the full mark's wing and eye stop resolving and read as dirt.
    # The sidebar icon is ~20px, so it must be the one-colour silhouette.
    src = PAGE.read_text(encoding="utf-8")
    assert 'class="sidemark"' in src


def test_page_makes_no_external_requests():
    # The whole point of inlining: one file, no network. Guard it.
    src = PAGE.read_text(encoding="utf-8")
    for scheme in ("http://", "https://"):
        for tag in ("src=", "href="):
            assert f'{tag}"{scheme}' not in src, f"external {tag} reference found"
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `pytest -q tests/test_chat_renderer.py -k "favicon or lockup or sidemark or external"`
Expected: FAIL on the first three; the fourth should already pass and must KEEP passing.

- [ ] **Step 3: Inline the favicon**

Generate the data URI from the real file rather than hand-encoding. URL-encode it (percent-encode
`#`, `<`, `>`, `"`, and newlines); do NOT base64 — a plain-text SVG data URI stays readable and
diffable:

```python
# one-off, to produce the string you paste:
import urllib.parse, pathlib
svg = pathlib.Path("Brand/favicon/favicon.svg").read_text().strip()
print("data:image/svg+xml," + urllib.parse.quote(svg, safe="/:=\"' "))
```

Add to `<head>`, after the existing `<title>`:

```html
<!-- Inlined from Brand/favicon/favicon.svg so the page stays one self-contained
     file with no external requests. Regenerate with:
     cd Brand && python3 build.py && python3 render_pngs.py  -- then re-inline. -->
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,...">
<meta name="theme-color" content="#C4694A">
```

- [ ] **Step 4: Put the lockup on the login screen**

Replace `<h1>Wren</h1>` in the `#login` block with the inlined contents of
`Brand/brand/lockup.svg`, given `id="brandmark"`, and sized in CSS rather than by the SVG's own
width/height attributes (drop those, keep `viewBox`). Keep the SVG's `role="img"` and
`aria-label="Wren"` — they replace the heading's accessible text, so do not also leave a visually
hidden "Wren" heading or the name is announced twice.

- [ ] **Step 5: Put the simple mark in the sidebar**

Replace `<b>Wren</b>` in the sidebar header with the inlined contents of
`Brand/brand/mark-simple.svg` carrying `class="sidemark"`, followed by the text `Wren`. The mark
is decorative here because the word is right beside it, so give the SVG `aria-hidden="true"` and
drop its `role`/`aria-label`.

- [ ] **Step 6: Add the CSS**

Beside the existing `aside header` rules, respecting the file's custom properties:

```css
#brandmark { width: 190px; height: auto; display: block; margin: 0 auto 18px; }
aside header b { display: flex; align-items: center; gap: 8px; }
.sidemark { width: 20px; height: 20px; flex: none; }
```

Clear space is 0.35 × height: at 20 px that is a 7 px minimum gap, so the 8 px `gap` satisfies it.
At 190 px the lockup needs ~24 px; the 18 px bottom margin plus the surrounding padding covers it.

- [ ] **Step 7: Run the tests**

Run: `pytest -q tests/test_chat_renderer.py tests/test_webchat.py`
Expected: PASS. The renderer tests are also your canary — if they fail, you have damaged the
U+E000 placeholder region; restore with `git show HEAD:wren/communication/chat.html`.

- [ ] **Step 8: Run the full suite**

Run: `pytest -q`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add wren/communication/chat.html tests/test_chat_renderer.py
git commit -m "feat(webchat): brand the chat window with the Wren marks"
```

---

## Notes for the implementer

- **Test counts are cumulative and approximate.** If yours differ by a few, that is fine; if the suite goes *red*, stop and fix before moving on.
- **`config` module state leaks between tests.** `set_override` mutates module globals, so every test that sets one must clear it in a `finally`. The tests above do; keep that habit for any you add.
- **`router.reset()`** exists and the `NOTIFY_VIA` tests use it. Always restore router state in a `finally`, or later tests in the same session will see your fake channel.
- **Do not add a credential to `SETTABLE`.** If a future task seems to need one, that is a design change, not an implementation detail — stop and raise it.
