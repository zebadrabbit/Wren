# "What Plugins Are Active" Report — Design

## Goal

"what plugins do you have" / "what's active" — a new core intent reporting
which of Wren's plugins are currently active, distinct from `HELP_TEXT`
(which lists example phrases per feature) and `status` (which reports the
LLM backend/uptime/tokens, not the plugin system).

## Each plugin gains a `PLUGIN_NAME` string constant

One-line addition to each of the 7 existing plugin files:

```python
PLUGIN_NAME = "Notes & Ideas"      # notes_plugin.py
PLUGIN_NAME = "Shopping List"      # shopping_plugin.py
PLUGIN_NAME = "Reminders"          # reminder_plugin.py
PLUGIN_NAME = "Contacts"           # contacts_plugin.py
PLUGIN_NAME = "Pins"               # pins_plugin.py
PLUGIN_NAME = "Web Lookup"         # web_plugin.py
PLUGIN_NAME = "Email Watcher"      # email_plugin.py
```

## Optional `is_active() -> bool`

Only the two plugins that are ever conditionally inactive define this;
everything else is always-active by omission (a plugin with no `is_active`
reports `True`):

```python
def is_active() -> bool:
    return bool(config.SEARXNG_URL)   # web_plugin.py
```

```python
def is_active() -> bool:
    return bool(config.EMAIL_WATCH)   # email_plugin.py
```

## `wren/plugins.py`: `plugin_status()`

```python
def plugin_status() -> list[tuple[str, bool]]:
    return [
        (getattr(p, "PLUGIN_NAME", p.__name__), getattr(p, "is_active", lambda: True)())
        for p in PLUGINS
    ]
```

Falls back to the module's `__name__` if a plugin somehow lacks
`PLUGIN_NAME` (shouldn't happen once all 7 are updated, but keeps the
function total rather than raising).

## `wren/bot.py`: the `list_plugins` core intent

New branch in `on_message`, alongside `help`/`status`/`send_to_person`:

```python
elif intent == "list_plugins":
    lines = [f"{'✅' if active else '⏸️'} {name}" for name, active in plugins.plugin_status()]
    await message.channel.send("\n".join(lines))
```

## `wren/brain.py`: prompt update

`"list_plugins"` added to the intent enum (alongside `"help"`/`"status"`)
and one new guideline bullet: "user wants to know what capabilities/plugins
Wren currently has active."

## Testing

`tests/test_plugins.py`: `plugin_status()` — a plugin with no `is_active`
reports `True`; a fake plugin (`types.SimpleNamespace` with `PLUGIN_NAME`
and `is_active`) reporting `False`; name falls back to `__name__` when
`PLUGIN_NAME` is absent. `bot.py`'s dispatch branch is untested (pre-
existing condition — module-level `client.run()` makes it unimportable
without a real token — consistent with every other `bot.py` change in
this project).

## Out of scope

- Per-plugin descriptions or example commands in this report (that's
  `HELP_TEXT`'s job).
- A way to actually toggle a plugin on/off at runtime (this report is
  read-only visibility into config-derived state, not a new control
  surface).
