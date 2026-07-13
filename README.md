# Wren

A private Discord assistant for a small household whitelist. DM it plain
English and it figures out the intent — save a note, manage a shared
shopping list, capture an idea, message someone else on the whitelist, or
just chat.

## Setup

1. `python3 -m venv venv && source venv/bin/activate`
2. `pip install -r requirements.txt`
3. `cp .env.example .env` and fill in:
   - `DISCORD_TOKEN`, `OWNER_ID`, `HUSBAND_ID` — required.
   - `LLM_PROVIDERS` — an ordered, comma-separated list of LLM providers to
     try (see below). At least one must resolve or Wren refuses to start.
   - Everything else in `.env.example` is optional.
4. `python3 -m wren.bot`

Alternatively, run `./setup.sh` for an interactive walkthrough that does all
of the above plus optional systemd install.

Wren only responds to DMs from the two whitelisted Discord user IDs. Any
other message is ignored.

## LLM providers and fallback

Wren talks to an LLM for intent detection and replies. Supported providers:
`lmstudio` (or any local/self-hosted OpenAI-compatible server), `ollama`,
`openai`, `claude` (via Anthropic's OpenAI-compatible endpoint),
`openrouter`. Set `LLM_PROVIDERS=lmstudio,openai` (priority order) and the
matching `*_BASE_URL`/`*_API_KEY`/`*_MODEL` env vars for whichever providers
you list — see `.env.example` for the exact variable names per provider.

If a provider fails an LLM call (any exception — timeout, bad response,
connection refused), Wren logs a warning and falls through to the next
provider in the list. If every provider in the chain fails, the request
fails too — this is a liveness fallback across endpoints, not a
retry/backoff mechanism.

## What you can say

**Notes**
- "remind me to call the plumber" → saves a note
- "what do I need to do" → recalls and answers from your notes

**Ideas** (separate from notes — for things to revisit or expand later)
- "remember this idea: build a treehouse"
- "what ideas have I saved"
- "discard the idea about the treehouse" — matches by a phrase, asks you to
  be more specific if it matches more than one
- "expand on the treehouse idea" — Wren elaborates via the LLM; nothing is
  saved back

**Shopping** (one shared list across the whole household)
- "add potatoes to shopping"
- "got the potatoes" / "remove potatoes from shopping"
- "what's on the shopping list" — also surfaces "you often get: ..."
  suggestions for items you've bought 3+ times before
- "send shopping to husband" — DMs the whole current list to another
  whitelisted contact

**Messaging**
- "tell husband dinner's at 7" — DMs the other whitelisted contact
- anything else falls through to open-ended chat

## Email-arrival watcher (optional)

Set `EMAIL_WATCH=alice@example.com:owner,bob@example.com:husband` plus
`IMAP_HOST`/`IMAP_USER`/`IMAP_PASSWORD` in `.env` and Wren polls that inbox
(every `EMAIL_POLL_SECONDS`, default 60) for unread mail from a listed
sender and DMs the mapped contact. Leave `EMAIL_WATCH` unset to disable —
nothing else about the bot depends on it.

## Architecture

All source lives in the `wren/` package (run as `python3 -m wren.bot`):

- `wren/bot.py` — Discord client, whitelist gate, and dispatch: routes a
  detected intent to whichever plugin owns it, or handles `send_to_person`/
  `chat` directly as core (non-plugin) behavior.
- `wren/brain.py` — LLM intent detection (`detect_intent`), the fallback
  chain across providers (`_complete`), and one-off LLM calls used by
  plugins (`recall`, `expand`, `chat`).
- `wren/config.py` / `wren/providers.py` — env-driven configuration,
  including the provider chain and the email watcher's IMAP settings.
- `wren/notes.py` / `wren/shopping.py` — SQLite storage. Notes are
  per-owner; the shopping list is one shared table with no owner scoping.
- `wren/notes_plugin.py` / `wren/shopping_plugin.py` — own their respective
  intents: declare the intent names, the LLM prompt guideline text for
  them, and an async `handle(...)` dispatch function.
- `wren/email_plugin.py` — an event-only plugin (no user-invoked intents):
  its `start()` hook runs the IMAP poll loop in the background.
- `wren/plugins.py` — the registry composing all plugins: builds the
  combined intent list and prompt text `brain.py` needs, dispatches
  intents to the right plugin, and starts each plugin's optional
  background task exactly once per process (guards against Discord's
  `on_ready` re-firing on reconnect).
- `wren/discord_utils.py` — shared whitelist-lookup/DM-send helper used by
  `bot.py` and the shopping plugin.

Adding a new intent-handling plugin: write a module in `wren/` exposing
`INTENTS`, `PROMPT_GUIDELINES`, and `async handle(intent, message, client,
user_id, content, tags, person)`, then add it to `plugins.py`'s `PLUGINS`
list. An event-only plugin (background task, no user commands) only needs
an optional `async start(client)`.

## Development

```
pip install -r requirements.txt
pytest -q
```

Design docs and implementation plans for each feature live in
`docs/superpowers/specs/` and `docs/superpowers/plans/`.

## Deployment

`wren.service` is a sample systemd unit (adjust `User`/paths for your
setup):

```
sudo cp wren.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wren
```

Once installed (e.g. via `./setup.sh`'s systemd-install prompt, which
templates the unit's paths/user for you), use `./manage.sh
{start|stop|restart|status|logs}` as the day-to-day way to control the
running service.
