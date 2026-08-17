# Wren — Project Plan (v2 restart)

## Why restart

Wren had drifted: it grew Discord-specific assumptions early, then had a
"surfaces vs capability plugins" split retrofitted on top later. That split
was the right instinct but the wrong shape for where the project is actually
headed — a chat assistant, reachable through *any* messaging app, extensible
with drop-in **skills** (shopping list, notes, reminders, ...) and drop-in
**communication channels** (Discord, Telegram, Gmail, ...), where a skill
never has to know which channel it's being used from.

This restart doesn't throw the code away. Wren's engine — LLM intent
detection, a transport-free `Channel`/`Ctx` abstraction, SQLite storage,
provider fallback — was already sound and already close to this shape. What
changed is the taxonomy on top of it, made explicit instead of implicit, and
named so a new contributor (or you, in six months) can tell at a glance what
kind of plugin they're looking at.

## The new direction

Wren is a **local-first chat assistant** with a **built-in web chat window**
(same idea as this conversation) and two plugin classes:

### 1. Communication plugins — how you reach Wren, how Wren reaches you

Live in `wren/communication/`. Each one declares a `ROLE`:

- **`chat`** — a full two-way conversational channel: receives messages,
  sends replies, optionally keeps history. Examples: **Discord**, the
  built-in **HTTP/web chat**, and **Telegram** (built; needs a bot token).
- **`input`** — a one-way event source: watches something external and
  raises an event, but has no "send" of its own. It hands off to a `chat`
  plugin via `router.notify_name(...)`. Examples: **Gmail** (IMAP watcher),
  **GitHub** (repo activity watcher).

A future **`output`** role (deliver-only, e.g. a push-notification service
with no inbound message path) is anticipated by the same `ROLE` field but
nothing uses it yet — added if/when a plugin needs it.

### 2. Skills — what Wren can actually do

Live in `wren/skills/`. A skill declares `INTENTS` (which user intents it
owns), `PROMPT_GUIDELINES` (how the LLM should recognize them), and an async
`handle(intent, ctx)`. It never imports `discord` or `aiohttp` — it only
ever sees `ctx.channel.send(...)`. That's what makes "add potatoes to
shopping" work identically whether it arrived by Discord DM, the web chat
window, voice, or Telegram.

Shipped skills, carried over from the old capability plugins: **Shopping
List**, **Notes** (+ Ideas), **Reminders**, **Pins**, **Contacts**, **Web**
(search/fetch). Each is unchanged in behavior — only where it lives and what
it's called changed.

### How they compose

```
you ──▶ communication plugin (chat) ──▶ core.handle_message ──▶ skill
         Discord / HTTP-webchat          (whitelist, intent)     (writes back via ctx.channel)

external event ──▶ communication plugin (input) ──▶ router.notify_name ──▶ communication plugin (chat)
   new email,          gmail_plugin.py /                                      discord_plugin.py, ...
   repo push            github_plugin.py
```

This is exactly the pattern you described: Discord/Telegram under
"Communication" for chatting, Gmail under "Communication" as an input-only
notifier, and Shopping List (and friends) as skills usable from the chat
window or any comms plugin.

## What's already done (this session)

The existing Wren codebase at `W:\work\Wren` has been restructured in place
to this taxonomy — reusing, not rewriting, the working code:

- `wren/surfaces/` → `wren/communication/`, with `discord.py` →
  `discord_plugin.py`, `http.py` → `http_plugin.py`; `webchat.py` and
  `discord_utils.py` moved alongside.
- `wren/email_plugin.py` → `wren/communication/gmail_plugin.py`,
  `wren/github_plugin.py` (+ `github_state.py`) → `wren/communication/` —
  reclassified from "capability plugin" to Communication (`ROLE = "input"`),
  since they're event sources, not chat skills.
- `wren/*_plugin.py` (shopping, notes, reminders, pins, contacts, web) →
  `wren/skills/*_skill.py`, with their storage modules alongside as
  `*_store.py` (e.g. `shopping.py` → `wren/skills/shopping_store.py`).
- `wren/plugins.py` → `wren/registry.py` — now only registers Skills;
  Communication plugins are started directly by `run.py` from
  `COMMUNICATION_PLUGINS`.
- New `wren/communication/telegram_plugin.py` — a complete second chat
  plugin, written to prove the Communication/Skill split (see below).
- Env vars renamed for clarity: `SURFACES` → `COMMUNICATION_PLUGINS`,
  `NOTIFY_SURFACE` → `NOTIFY_VIA`. `.env.example` and `README.md` updated.
- All 33 test files' imports updated to the new module paths; every import
  across `wren/` and `tests/` was statically verified to resolve (a custom
  checker walked every `import`/`from` statement and confirmed the target
  file exists in the new layout). `python3 -m py_compile` passes clean on
  every source and test file.
- Git: your prior local history (8 commits ahead of `origin/main`, remote
  `git@github.com:zebadrabbit/Wren.git`) was archived to
  `.git-wren-v1-archive/` (gitignored, not deleted — nothing was lost) and a
  fresh repo was initialized locally with one clean commit. GitHub itself
  was intentionally left untouched, per your call to keep this local-only
  for now.

**Not done at the time of the restart** (all three since resolved — see the
status section below):

- `pytest` could not actually be run in this session (the sandboxed shell
  used to edit your files has no network access and the project's `venv`
  targets a different Python installation) — imports were verified
  statically instead. **Run `pytest -q` yourself before trusting this** is
  the single most important next step.
- `wren/surfaces/` still contains one stale, empty `__pycache__/` directory
  that couldn't be removed from this session (sandbox can't delete files on
  your machine) — harmless, gitignored, safe to delete by hand.
- Telegram is a stub only — no live `start()`/`notify()`/`TelegramChannel`.

## Status as of 2026-08-09

Everything above this line is the record of the restart session itself and is
left as written. What has happened since:

- **Restructure verified.** The suite runs and is green (557 passed). It did
  not start that way — the static import check missed 46 breakages, all of
  them stale references living in strings rather than import statements (path
  literals, `mock.patch("wren.old_module...")` targets, renamed config attrs,
  and tests asserting the old plugin taxonomy). If you rename a module here,
  grep for the old name as a bare string too.
- **Live run smoke-tested.** `COMMUNICATION_PLUGINS=http` on a temp DB and
  port: boots clean, webchat serves, and "add potatoes to shopping" round
  trips through intent detection into the shopping skill and back.
- **`.env` was still on the v1 keys** (`SURFACES`, `NOTIFY_SURFACE`). Renamed.
  This would not have errored — `config.py` defaults `COMMUNICATION_PLUGINS`
  to `discord`, so the next restart would have silently come up without the
  web chat.
- **Telegram is built**, not a stub: Bot API over `aiohttp` long-polling, no
  new dependency. Deliberately **not wired in** — no bot token yet, so
  `TELEGRAM_TOKEN` is unset and `telegram` is absent from
  `COMMUNICATION_PLUGINS`. Enabling it is those two `.env` lines plus
  whitelisting the Telegram user id as a contact.
- **The architecture claim held.** Adding a whole new communication channel
  touched zero files under `wren/skills/`. That was the point of the restart,
  and it is now demonstrated rather than asserted.
- `wren/surfaces/` is gone (it held 5 orphaned `.pyc`, not an empty dir).

Known rough edges in the new Telegram plugin, none blocking, all recorded on
the Trello board: `notify()` treats every HTTP 400 as permanent (so an
over-4096-char message is dropped rather than split); a non-`handle_message`
exception mid-batch discards that batch's offset progress; and a cold start
drains the whole unconfirmed backlog, answering each with an inline LLM call.

## Focused next steps

1. ~~Verify the restructure~~ — done, 557 passed.
2. ~~Smoke-test a run~~ — done.
3. ~~Build the Telegram communication plugin~~ — done; needs a bot token to
   actually enable.
4. **Decide the GitHub story**: push this restructured repo to the existing
   `zebadrabbit/Wren` remote (as a new default branch, or a fresh repo if
   you'd rather cut ties with the old history entirely) once you're happy
   with the result locally.
5. **Pick the next skill or channel** to build toward your actual use case —
   the architecture now makes either a same-shaped, additive change instead
   of a special case.

## Design principles to hold onto

- A skill never imports a transport library. If it needs to, it's not a
  skill — either the transport needs a `Channel` method added, or the logic
  belongs in a communication plugin.
- A communication plugin never contains domain logic (no "if item on
  shopping list" inside `discord_plugin.py`). It authenticates, builds a
  `Channel`, and calls `core.handle_message`.
- `ROLE` is the only thing that distinguishes communication plugins from
  each other structurally — `"chat"` needs `start()` + a `Channel`; `"input"`
  needs `start()` and calls `router.notify_name(...)`; nothing else is
  special-cased.
- New capability → new file in `wren/skills/`, one line in
  `registry.py`'s `PLUGINS` list. New channel → new file in
  `wren/communication/`, one entry in `COMMUNICATION_PLUGINS`. Neither
  should ever require touching the other.
