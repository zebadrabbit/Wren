# CLAUDE.md

Guidance for Claude Code (or any agent) working in this repo. Read
`PROJECT_PLAN.md` first for the why; this file is the how.

## What this is

Wren is a local-first, self-hosted chat assistant. It has one engine (LLM
intent detection + SQLite storage) and two plugin classes on top of it:

- **`wren/communication/`** — how you reach Wren / how Wren reaches you.
  Discord, HTTP + browser chat, Gmail (IMAP watcher), GitHub (repo watcher).
  A `telegram_plugin.py` stub shows the shape of the next one.
- **`wren/skills/`** — what Wren can do. Shopping List, Notes, Reminders,
  Pins, Contacts, Web search/fetch.

Run with `python3 -m wren.run`. Full architecture doc: `README.md`
("Architecture" section). Restart rationale and next steps: `PROJECT_PLAN.md`.

## Hard rules — read before editing

1. **A skill (`wren/skills/*_skill.py`) never imports `discord`, `aiohttp`,
   or anything transport-specific.** It only ever touches `ctx.channel`
   (a `Channel` from `wren/channel.py`). If a skill needs to do something a
   `Channel` can't do, add the capability to the `Channel` protocol — don't
   reach around it.
2. **A communication plugin (`wren/communication/*_plugin.py`) never
   contains domain logic.** It authenticates the caller, builds a `Channel`,
   and calls `core.handle_message(user_id, text, channel)`. No "if item on
   shopping list" inside `discord_plugin.py`.
3. **Every communication plugin declares `ROLE`**: `"chat"` (input + output,
   needs `async start()` + a `Channel` impl, e.g. `discord_plugin.py`) or
   `"input"` (one-way, needs `async start()` that calls
   `router.notify_name(...)`, e.g. `gmail_plugin.py`). Nothing else is
   special-cased between plugins of the same role.
4. **Adding a skill**: new file in `wren/skills/` exposing `INTENTS`,
   `PROMPT_GUIDELINES`, `async handle(intent, ctx)` — then add it to the
   `PLUGINS` list in `wren/registry.py`. That's the only wiring point.
5. **Adding a communication plugin**: new file in `wren/communication/`,
   then add its name to `COMMUNICATION_PLUGINS` in `.env`. `run.py` imports
   `wren.communication.<name>_plugin` dynamically — the module name must be
   exactly `<name>_plugin.py`.
6. **Never run ad-hoc scripts against the real `wren.db`.** Storage modules
   resolve their path via `wren/db.py`, which falls back to a *relative*
   `wren.db` in the repo root — the same file the running service uses. Set
   `WREN_DB=$(mktemp -d)/scratch.db` before importing anything from `wren`
   for any manual check. `tests/conftest.py` already does this per-test, so
   `pytest` is safe by construction — only manual scripts are the risk.
   (This has caused real data loss once before — see README's Development
   section for the story.)

## Env vars that changed in the v2 restart

If you're referencing old docs/specs under `docs/superpowers/`, note these
were renamed and the old names no longer work:

| Old (v1) | New (v2) |
|---|---|
| `SURFACES` | `COMMUNICATION_PLUGINS` |
| `NOTIFY_SURFACE` | `NOTIFY_VIA` |

Module path renames (for grep/context when reading old plans/specs):

| Old | New |
|---|---|
| `wren/surfaces/discord.py` | `wren/communication/discord_plugin.py` |
| `wren/surfaces/http.py` | `wren/communication/http_plugin.py` |
| `wren/email_plugin.py` | `wren/communication/gmail_plugin.py` |
| `wren/github_plugin.py`, `github_state.py` | `wren/communication/github_plugin.py`, `github_state.py` |
| `wren/plugins.py` | `wren/registry.py` |
| `wren/shopping_plugin.py` / `shopping.py` | `wren/skills/shopping_skill.py` / `shopping_store.py` |
| `wren/notes_plugin.py` / `notes.py` | `wren/skills/notes_skill.py` / `notes_store.py` |
| `wren/reminder_plugin.py` / `reminders.py` | `wren/skills/reminder_skill.py` / `reminders_store.py` |
| `wren/pins_plugin.py` / `pins.py` | `wren/skills/pins_skill.py` / `pins_store.py` |
| `wren/contacts_plugin.py` | `wren/skills/contacts_skill.py` (`wren/contacts.py` unchanged — core infra) |
| `wren/web_plugin.py` / `web.py` | `wren/skills/web_skill.py` / `web_search.py` |

## Dev loop

```bash
source venv/bin/activate
pip install -r requirements.txt
pytest -q                        # NOT yet run post-restart — do this first
python3 -m wren.run              # needs .env; see .env.example
```

**First thing to do in this repo**: run `pytest -q`. The v2 restructure
(communication/ + skills/ split, all import paths, .env var renames) was
verified by static analysis only — every import was checked to resolve to
an existing file, and `py_compile` passes clean — but the test suite itself
could not actually be executed in the session that did the restructure (no
network access in that sandbox, and its Python didn't match this repo's
venv). Treat it as "should be green" but unconfirmed until you run it.

## Known loose ends from the restart

- `wren/surfaces/` still has a stale, empty `__pycache__/` — harmless,
  gitignored, delete by hand whenever.
- `wren/communication/telegram_plugin.py` is a stub (`NotImplementedError`
  in `start()`/`notify()`) — not wired into any config, just documents the
  shape of the next chat plugin.
- Old local git history (pre-restart, 8 commits ahead of `origin/main`) is
  preserved at `.git-wren-v1-archive/` (gitignored). The current repo was
  git-init'd fresh. GitHub remote is still `git@github.com:zebadrabbit/Wren.git`
  from the old repo but has not been touched — decide when/how to push
  (new branch on the existing repo, or a clean new repo) before you do.
- `docs/superpowers/plans/` and `specs/` predate the restart and reference
  the old module/env names — still useful for the *behavioral* history of
  each feature (why reminders work the way they do, etc.), just translate
  names using the table above.

## Style notes carried over from v1 (still apply)

- Comments explain *why*, especially non-obvious ordering/timing decisions
  (see `router.py`'s docstring on `notify()`'s False-vs-raise contract, or
  `core.py`'s `asyncio.to_thread` usage) — keep that standard for new code.
- Tests mock nothing Discord-specific; they build a `CollectingChannel` and
  a `Ctx` and call `handle()` directly. Keep new skill tests in that shape.
