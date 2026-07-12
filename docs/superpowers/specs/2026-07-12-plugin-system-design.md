# Plugin System — Design

## Goal

`bot.py`'s `on_message` has grown into a 12-branch if/elif chain and `brain.py`'s
LLM prompt is one hardcoded string listing every intent, across three
features (notes, ideas, shopping) added in prior work. Before adding more
features — and before the planned email-arrival/Node-RED/Grafana
integrations, which need a way to push a Discord message *without* being
triggered by an incoming DM — Wren needs a plugin architecture: existing
intent-handling refactored into pluggable modules, and an extension point
ready for event-driven (inbound) plugins.

This is sub-project 3 of the personal-assistant expansion (shopping list and
ideas bucket already shipped; external integrations are still a separate,
later sub-project that will be the first real *consumer* of the event-plugin
extension point this adds).

## Plugin contract

A plugin is a plain Python module — no base class or registration
decorator, just a duck-typed contract, since this bot has a handful of
plugins, not hundreds:

```python
INTENTS: list[str]        # intent names this plugin owns
PROMPT_GUIDELINES: str    # guideline bullet(s) for brain.py's prompt, one line per intent

async def handle(
    intent: str, message: discord.Message, client: discord.Client,
    user_id: int, content: str, tags: list[str], person: str | None,
) -> None:
    """Dispatch on `intent`, send the reply via `message.channel.send(...)`."""

# OPTIONAL:
async def start(client: discord.Client) -> None:
    """Run a background task (e.g. poll an inbox). Not used by any plugin
    yet — this is the hook the future email/Node-RED integration sub-project
    will implement. bot.py's on_ready calls this for every plugin that
    defines it."""
```

## New files

- **`notes_plugin.py`** — owns `save_note`, `recall_notes`, `save_idea`,
  `recall_ideas`, `discard_idea`, `expand_idea` (6 intents). Wraps `notes.py`
  (storage, unchanged) and calls `brain.recall()`/`brain.expand()`. The
  branch bodies are moved verbatim from `bot.py`, including the
  empty-content guard added during the ideas-bucket work.
- **`shopping_plugin.py`** — owns `add_shopping_item`, `remove_shopping_item`,
  `recall_shopping`, `send_shopping_list` (4 intents). Wraps `shopping.py`
  (storage, unchanged). `send_shopping_list` uses `discord_utils.notify()`
  instead of its own copy of the whitelist/fetch_user/DM-exception logic.
- **`plugins.py`** — the registry:
  ```python
  import notes_plugin
  import shopping_plugin

  PLUGINS = [notes_plugin, shopping_plugin]
  INTENT_HANDLERS = {intent: p for p in PLUGINS for intent in p.INTENTS}

  def all_intents() -> list[str]:
      return [i for p in PLUGINS for i in p.INTENTS]

  def all_guidelines() -> str:
      return "\n".join(p.PROMPT_GUIDELINES for p in PLUGINS)

  async def start_all(client) -> None:
      for p in PLUGINS:
          if hasattr(p, "start"):
              await p.start(client)
  ```
  Adding a plugin later means: write the module, import it here, add it to
  `PLUGINS`. No auto-discovery — explicit and easy to reason about at this
  scale.
- **`discord_utils.py`** — extracts the whitelist-lookup/`fetch_user`/DM
  try-except pattern currently duplicated in `send_to_person` and
  `send_shopping_list`:
  ```python
  async def notify(client: discord.Client, contact_name: str, text: str) -> bool:
      """Look up contact_name in the whitelist and DM them `text`.
      Returns True on success, False if unknown contact or DM failed
      (NotFound/Forbidden, logged)."""
  ```
  Any plugin — including a future background `start()` task — can push a
  Discord message through this without its own copy of the lookup/DM logic.

## What stays "core" (not plugin-owned)

`send_to_person` and `chat` remain in `bot.py`/`brain.py` directly — they're
baseline messaging behavior, not a domain feature module. `send_to_person`'s
branch is refactored to call `discord_utils.notify()` (removing its
duplication with `shopping_plugin`'s `send_shopping_list`), but its guideline
text and JSON-schema position stay hardcoded in `brain.py`'s `_SYSTEM`
template, same as `chat`.

## Avoiding a circular import

`notes_plugin.py` needs `brain.recall()`/`brain.expand()`. `brain.py` needs
every plugin's `INTENTS`/`PROMPT_GUIDELINES` to build its prompt. If
`brain.py` imported `plugins.py` directly, that would cycle: `brain` →
`plugins` → `notes_plugin` → `brain`.

Fix: `brain.py` never imports `plugins.py`. Instead it exposes:

```python
def register_plugins(intents: list[str], guidelines: str) -> None:
    """Called once at startup by bot.py. Stores the plugin-contributed
    intent names and prompt guideline text as module state, used by
    detect_intent() when composing _SYSTEM."""
```

`bot.py`, which already imports both `brain` and `plugins`, calls
`brain.register_plugins(plugins.all_intents(), plugins.all_guidelines())`
once at module level, right after its imports. `brain.py`'s `_SYSTEM`
template gains two placeholders (`{plugin_intents}`, `{plugin_guidelines}`)
filled from that registered state inside `detect_intent()`, alongside the
existing `{date}`/`{contacts}` placeholders.

If `register_plugins()` is never called (e.g. a test importing `brain.py`
directly without going through `bot.py`), the placeholders default to an
empty list/string — `detect_intent()` still works, it just builds a prompt
missing the plugin-contributed sections. This is fine for `tests/test_brain.py`
since its tests mock the LLM response directly and never depend on the
prompt's actual content — a pre-existing property of those tests, not
something this change weakens.

## Dispatch (bot.py)

```python
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
    # existing logic, now calling discord_utils.notify()
    ...
else:  # chat
    reply = brain.chat(text)
    await message.channel.send(reply)
```

`on_ready` adds `await plugins.start_all(client)` alongside the existing
`notes.init_db()`/`shopping.init_db()` calls (those stay as direct calls —
not worth adding an init-hook to the plugin contract for two lines of
existing code).

## Testing

- **New `tests/test_discord_utils.py`**: `notify()` with a mocked
  `discord.Client`/`fetch_user` — success case, unknown-contact case (no
  `fetch_user` call attempted), `NotFound`/`Forbidden` case. This is
  genuinely new coverage — this logic had none before (it lived directly in
  the untestable `bot.py`).
- **New `tests/test_notes_plugin.py`** / **`tests/test_shopping_plugin.py`**:
  `handle()` for each owned intent, using a mocked `message`
  (`message.channel.send` as an `AsyncMock`) and a mocked `client` where
  needed (`send_shopping_list`), against the real tmp-db storage fixture
  pattern already used by `tests/test_notes.py`/`tests/test_shopping.py`,
  with `brain.recall`/`brain.expand` mocked the same way `tests/test_brain.py`
  already mocks `_get_client`. This is also new coverage, enabled by moving
  dispatch logic out of the unimportable `bot.py` into plainly-importable
  plugin modules.
- **No changes needed** to `tests/test_notes.py`, `tests/test_shopping.py`,
  or `tests/test_brain.py`'s existing intent-JSON-shape tests — storage and
  JSON-parsing behavior are unchanged.
- **Still no `tests/test_bot.py`** — `bot.py` itself is thinner after this
  refactor (a lookup + two `elif` branches instead of 12), but its
  module-level `client.run(...)` call still makes it unimportable without a
  real token. Manual read-back remains the verification method for `bot.py`
  changes specifically.

## Out of scope

- Building any real event-driven plugin (email watcher, Node-RED, Grafana)
  — this sub-project only adds the `start()` hook and `notify()` helper they
  will use. That work is sub-project 4.
- Auto-discovery of plugins from a directory — a static `PLUGINS` list in
  `plugins.py` is the whole registration mechanism.
- Per-plugin configuration/settings framework — none of the two existing
  plugins need configuration beyond what `config.py` already provides.
- Changing `notes.py`/`shopping.py` storage — both are wrapped as-is.
