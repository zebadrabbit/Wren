# CLAUDE.md

Guidance for Claude Code (or any agent) working in this repo. Read
`PROJECT_PLAN.md` first for the why; this file is the how.

## What this is

Wren is a local-first, self-hosted chat assistant. It has one engine (LLM
intent detection + SQLite storage) and two plugin classes on top of it:

- **`wren/communication/`** — how you reach Wren / how Wren reaches you.
  Discord, Telegram, HTTP + browser chat, Gmail (IMAP watcher), GitHub
  (repo watcher).
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
7. **Runtime-mutable state is `contacts` and `settings`, nothing else.**
   Everything else comes from `.env` and is read at import. `settings` stores
   only deviations, and `config.SETTABLE` is a positive allowlist — never add
   a credential to it, because `GET /api/plugins` returns its values.
   `SETTABLE` reaches further than plain scalar knobs, though: it also
   includes the five provider `*_MODEL` keys, whose `apply()` writes
   `os.environ` and rebuilds `config.LLM_CHAIN` (not just a `config` module
   attribute), because `providers.py` reads the environment and the chain
   bakes the model in at import. That rebuild only re-resolves providers
   already named in `LLM_PROVIDERS` at import (`_provider_names`, captured
   once and itself not in `SETTABLE`) — so a live `*_MODEL` change can revive
   one of those providers if its model was left blank, but can never add a
   provider absent from `LLM_PROVIDERS`. `SETTABLE` does not know the
   difference: it lists all five `*_MODEL` keys unconditionally, so setting
   the model of a provider `LLM_PROVIDERS` never named still returns success
   and changes nothing observable.

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
pytest -q                        # 657 passed as of 2026-08-09
python3 -m wren.run              # needs .env; see .env.example
```

**The restructure is now verified** — the suite has actually been run and is
green. It did not start that way: the static import check that the restructure
session relied on missed 46 breakages, because they were not import statements.
Worth knowing, since the same blind spot applies to any future rename here:

- path literals — `Path(...) / "wren" / "surfaces" / "chat.html"`
- `mock.patch("wren.reminder_plugin.asyncio.sleep")` string targets
- renamed config attributes reached via `monkeypatch.setattr(config, ...)`
- tests asserting the *old taxonomy* (e.g. the Gmail watcher living in
  `PLUGINS`), which are wrong in a way no import checker can see

Grep for the old name as a bare string, not just as an import, when you move
a module.

## Known loose ends from the restart

- **User ids are per-surface, and only Telegram translates.** Wren keys the
  authz gate in `core.handle_message` plus every note, reminder and pin off one
  id per person, but the number Telegram calls you is not the number Discord
  calls you. `telegram_plugin._wren_user_id` / `_telegram_chat_id` map the
  owner's `TELEGRAM_OWNER_ID` to and from `config.WHITELIST["owner"]`, so
  Telegram is a second door into the same Wren rather than a second, empty one.
  Translation is authn, which is the surface's job — do not push it into
  `core`. It covers the owner only; a second person on Telegram needs a real
  per-surface id column in `contacts`.
- `wren.service` runs this working tree **in place**
  (`/home/winter/work/Wren/venv/bin/python3 -m wren.run`), so an edit here is
  a production edit the moment anything restarts it. As of 2026-08-17 the live
  PID *is* v2 (restarted 17:47 that day), so the old "still running v1 from
  memory" caveat no longer applies — but the reverse now does: on-disk breakage
  is live at the next restart, and there is no stale-process grace period left.
  Assume any breakage you leave on disk is armed, not inert.
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
