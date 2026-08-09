
# <img width="40" height="40" align="center" alt="wren" src="https://github.com/user-attachments/assets/7198043c-20ef-41aa-a92e-29164b485306" /> Wren

A private, self-hosted assistant for a small household whitelist. Talk to it
in plain English and it figures out the intent — save a note, manage a shared
shopping list, capture an idea, search the web, message someone else on the
whitelist, or just chat.

Wren is transport-agnostic. It's built from two kinds of plugin: **Communication**
plugins (Discord, HTTP/web chat, Gmail, GitHub, ...) are how you reach Wren and
how Wren reaches you, and **Skills** (Shopping List, Notes, Reminders, ...) are
what Wren can actually do — usable through any Communication plugin you enable.
Discord is one Communication plugin, not a requirement: you can run Wren with
Discord alone, with the local HTTP API alone (for the desktop voice client or
the browser chat window), or several communication plugins at once.

## Setup

1. `python3 -m venv venv && source venv/bin/activate`
2. `pip install -r requirements.txt`
3. `cp .env.example .env` and fill in:
   - `OWNER_ID` — required. Your Wren user id; everything you save is filed
     under it. On a Discord install, use your Discord user ID.
   - `COMMUNICATION_PLUGINS` — comma-separated, default `discord`. See
     [Communication plugins](#communication-plugins).
   - `DISCORD_TOKEN` — required *only* if `COMMUNICATION_PLUGINS` includes `discord`.
   - `LLM_PROVIDERS` — an ordered, comma-separated list of LLM providers to
     try (see below). At least one must resolve or Wren refuses to start.
   - `TIMEZONE` — optional, default UTC. Set to your IANA timezone (e.g.
     `America/Chicago`) so reminder times like "9am" are interpreted
     correctly.
   - Everything else in `.env.example` is optional.
4. Discord only: in the [Discord Developer Portal](https://discord.com/developers/applications/),
   select your bot application → **Bot** tab → under **Privileged Gateway
   Intents**, enable **MESSAGE CONTENT INTENT** and save. Wren reads DM text
   to detect intent, and Discord treats that as a privileged intent that
   must be turned on here — without it, the bot crashes on startup with
   `discord.errors.PrivilegedIntentsRequired`.
5. `python3 -m wren.run`

Alternatively, run `./setup.sh` for an interactive walkthrough that does all
of the above except the Developer Portal step (that one's manual, Discord
doesn't expose it via API) plus optional systemd install.

Whichever surface you use, Wren only answers whitelisted users (`owner`, plus
any contacts added at runtime). Everything else is ignored.

## Communication plugins

A Communication plugin is how you reach Wren, or how Wren reaches you. Skills
(notes, reminders, shopping, …) work identically across every chat-capable one.

Each Communication plugin declares a `ROLE`:
- **`chat`** — full input + output, a real conversational surface (`discord`, `http`)
- **`input`** — one-way event source; watches something external and calls
  `router.notify_name(...)` to hand off to a `chat` plugin (`gmail`, `github`)

### `discord` — role: chat

DM the bot. This is the only plugin with conversation history, so multi-turn
follow-ups ("what did I just say") work here and nowhere else. Requires
`DISCORD_TOKEN`.

### `http` — role: chat (send-only for proactive notifications)

A local HTTP API plus the browser chat UI. Set
`WREN_TOKENS=<token>:<user_id>` (generate with `openssl rand -hex 24`), then
open `http://<wren-host>:8787/` and paste that token once — it is kept in
`localStorage` after that. See [Web chat](#web-chat).

### `gmail` — role: input only

Watches an IMAP inbox and notifies a `chat` plugin when mail arrives from
someone on your whitelist. See [Gmail-arrival watcher](#gmail-arrival-watcher).
Never talks back directly — it has no "send" of its own.

### `github` — role: input only

Watches repo activity (stars, pushes, issues/PRs) and notifies a `chat`
plugin. See [GitHub watcher](#github-repo-activity-watcher). Also input-only.

The machine API, used by the desktop voice client:

| Route | Body | Returns |
|---|---|---|
| `GET /health` | — | `{"ok": true}` |
| `POST /message` | `{"text": "..."}` | `{"replies": [...], "files": [...]}` |
| `POST /voice` | raw WAV bytes | `{"transcript": "...", "replies": [...]}` |

All routes but `/health` need `Authorization: Bearer <token>`. Files come
back base64-encoded.

```bash
curl -s localhost:8787/message \
  -H "Authorization: Bearer $WREN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"text":"add potatoes to shopping"}'
```

It binds `127.0.0.1` by default. To reach it from other machines set
`WREN_HTTP_HOST=0.0.0.0` — on a trusted LAN only, since the bearer token is
the only thing guarding it. There is no TLS.

**The http plugin is send-only.** It has no way to push, so
`NOTIFY_VIA=http` cannot deliver reminders — it logs and drops them.
Keep `NOTIFY_VIA=discord` if you want reminders to reach you.

## Web chat

`COMMUNICATION_PLUGINS=http` also serves a chat page at `/` — a sidebar of conversations,
markdown replies, the whole assistant behind it. Say "add potatoes to
shopping" there and it goes on the same list Discord sees.

Open `http://<wren-host>:8787/`, paste a `WREN_TOKENS` value once, and it is
remembered per browser. The page itself is served without auth because it is a
static shell holding no user data; every request it makes carries the token.

**This is the only surface besides Discord with conversation history.** Voice
and the machine API are one-shot by design. Chat history lives in Wren's own
`conversations`/`messages` tables, so it follows you between machines.

Conversations are per-user: a token maps to a user id, and one user cannot see
or touch another's conversations.

| Route | Body | Returns |
|---|---|---|
| `GET /` | — | the chat page |
| `GET /api/conversations` | — | `[{id, title, updated_at}]` |
| `POST /api/conversations` | — | `{id, title}` |
| `GET /api/conversations/{id}` | — | `{id, title, messages}` |
| `PATCH /api/conversations/{id}` | `{title}` | rename |
| `DELETE /api/conversations/{id}` | — | delete it and its messages |
| `POST /api/conversations/{id}/message` | `{text}` | `{replies, files}` |

Titles come from the first message and can be renamed. Replies are **not**
streamed — `brain` returns a finished string and intent detection has to
happen first, so you get a thinking indicator rather than tokens appearing.
That is the main thing that will feel different from claude.ai.

### Where notifications go

Replies always return to whichever communication plugin asked. *Unprompted*
messages — a reminder firing, a Gmail or GitHub watcher alert — go to a
single configured `NOTIFY_VIA`, defaulting to the first entry in
`COMMUNICATION_PLUGINS`. So you can set a reminder by voice from a laptop
and have it ping you on Discord.

## Voice (optional)

Hold a global hotkey on any machine on your LAN, talk, release. Audio is
transcribed **on the Wren host** — it never leaves your network.

On the Wren host:

```bash
pip install faster-whisper       # without this, /voice returns 503
# .env: COMMUNICATION_PLUGINS=discord,http  +  WREN_TOKENS=...  +  WREN_HTTP_HOST=0.0.0.0
```

On each desktop (Windows, macOS, or Linux):

```bash
pip install -r client/requirements.txt
WREN_URL=http://wren-host:8787 WREN_TOKEN=<token> python client/wren_hotkey.py
```

Hold **F9** (override with `WREN_HOTKEY`) to record, release to send. The
reply arrives as a native desktop notification. Linux also needs PortAudio
(`apt install libportaudio2`).

Tune `WREN_STT_MODEL` (`tiny.en` → `large-v3`) and `WREN_STT_DEVICE` /
`WREN_STT_COMPUTE` to your hardware — the defaults (`base.en`, `auto`,
`int8`) are chosen to stay usable on a CPU-only host.

Voice requests are one-shot: non-Discord surfaces keep no conversation
history, so follow-ups that depend on the previous turn won't resolve.

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

**Model choice (local models via `lmstudio`/`ollama`):** use a plain
instruct model, not a "thinking"/reasoning model. Reasoning models (e.g.
Qwen3's default thinking mode) emit long chain-of-thought before answering
and can churn for a very long time on even a simple message — bad fit for
fast intent classification + short replies. `brain.py` sets `max_tokens` and
adds a `/no_think` hint as a safety net, but the model itself matters more.
Known-good, tested here: [`Qwen2.5-7B-Instruct-GGUF`](https://huggingface.co/lmstudio-community/Qwen2.5-7B-Instruct-GGUF)
(not Qwen3) — fast, follows JSON-schema instructions cleanly, no reasoning
overhead. `Llama-3.1-8B-Instruct` and `Mistral-7B-Instruct-v0.3` are solid
alternatives with the same profile.

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

**Reminders**
- "remind me to take out the trash at 6pm" / "in 20 minutes" / "tomorrow morning"
- "what are my reminders" — shows all upcoming reminders with times
- "cancel the trash reminder" — matches by phrase, asks for specifics if needed
- Times are interpreted in the configured `TIMEZONE` (default UTC) — set
  it in `.env` to your local zone.

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

**Status**
- "show status" / "what backend are you using" / "show model" — reports the
  active LLM backend/model/endpoint, process uptime, and token usage since
  the last restart (resets on restart, not persisted)

## Web lookup (optional)

Enable web search and page reading by setting `SEARXNG_URL` (and optionally
`FIRECRAWL_URL`) in `.env`. See `.env.example` for details.

Your SearXNG must expose its JSON API — add `json` to `search.formats` in its
`settings.yml` (not on by default) and, since its bot limiter 429s non-browser
callers, set `server.limiter: false` on a private LAN. Otherwise every search
just reports "Search is unavailable right now." Firecrawl works out of the box.

- "what's the weather in Chicago tomorrow" / "any news on the port strike" —
  searches the web and summarizes the results with links
- "read me the first one" / "read https://…" — fetches a page in full and
  summarizes it

## Gmail-arrival watcher (optional)

Add `gmail` to `COMMUNICATION_PLUGINS`, set
`EMAIL_WATCH=alice@example.com:owner,bob@example.com:husband` plus
`IMAP_HOST`/`IMAP_USER`/`IMAP_PASSWORD` in `.env` and Wren polls that inbox
(every `EMAIL_POLL_SECONDS`, default 60) for unread mail from a listed
sender and notifies the mapped contact via `NOTIFY_VIA`. Leave `EMAIL_WATCH`
unset (or drop `gmail` from `COMMUNICATION_PLUGINS`) to disable — nothing
else about the bot depends on it. Despite the name this is plain IMAP, so
any provider works, Gmail included.

## Architecture

All source lives in the `wren/` package (run as `python3 -m wren.run`). See
[PROJECT_PLAN.md](PROJECT_PLAN.md) for the fuller rationale behind this split.

There are **two** plugin classes, on different axes:

- **`wren/skills/`** — *what Wren can do*. Shopping List, Notes, Reminders,
  Web, Pins, Contacts. None of them import `discord`, `aiohttp`, or anything
  transport-specific — they only ever see a `Channel`.
- **`wren/communication/`** — *how you reach Wren, and how Wren reaches you*.
  Discord, HTTP/web chat (both `ROLE = "chat"`: input + output), Gmail,
  GitHub (both `ROLE = "input"`: watch something external, never receive a
  reply — they hand off through `router.notify_name(...)` to whichever chat
  plugin `NOTIFY_VIA` points at).

```
you ──▶ communication plugin ──▶ core.handle_message ──▶ skill
         (authn)                 (authz, intent)          (writes back via ctx.channel)

external event ──▶ communication plugin (input-only) ──▶ router.notify_name ──▶ communication plugin (chat)
   (new email,        (gmail_plugin.py,                                            (discord_plugin.py, ...)
    repo push)          github_plugin.py)
```

- `wren/run.py` — entrypoint. Initialises storage, starts each communication
  plugin named in `COMMUNICATION_PLUGINS`, starts every skill's background task.
- `wren/core.py` — transport-free dispatch: whitelist gate, intent detection,
  routing to whichever skill owns the intent, plus `help`/`status`/
  `list_plugins`/`send_to_person`/`chat`.
- `wren/channel.py` — the `Channel` protocol (`send`, `send_file`, `history`,
  `ack`), the `Ctx` dataclass handed to skills, and `CollectingChannel`
  (accumulates output; used by the HTTP plugin and by every skill's tests).
- `wren/router.py` — communication-plugin registry and `notify(user_id, text)`
  for unprompted messages, dispatched to `NOTIFY_VIA`.
- `wren/communication/discord_plugin.py` — Discord client, `DiscordChannel`
  (reactions implement `ack`, channel history implements `history`). `ROLE = "chat"`.
- `wren/communication/http_plugin.py` + `webchat.py` — aiohttp app, bearer-token
  auth, and the browser chat UI (conversations with real history). `ROLE = "chat"`.
- `wren/communication/gmail_plugin.py` — IMAP inbox watcher. `ROLE = "input"`.
- `wren/communication/github_plugin.py` (+ `github_state.py`) — repo activity
  watcher. `ROLE = "input"`.
- `wren/communication/telegram_plugin.py` — **stub**, not wired in; shows the
  shape of a new chat plugin.
- `wren/brain.py` — LLM intent detection (`detect_intent`), the fallback
  chain across providers (`_complete`), and one-off LLM calls used by
  skills (`recall`, `expand`, `chat`).
- `wren/config.py` / `wren/providers.py` — env-driven configuration.
- `wren/db.py` — one connection helper; `WREN_DB` sets the path.
- `wren/skills/notes_store.py` / `shopping_store.py` / `reminders_store.py` /
  `wren/contacts.py` / `wren/skills/pins_store.py` — SQLite storage. Notes,
  reminders and pins are per-owner; the shopping list is one shared table
  with no owner scoping.
- `wren/skills/*_skill.py` — Skills: intent names, the LLM prompt guideline
  text for them, and an async `handle(intent, ctx)`.
- `wren/stt.py` — speech-to-text, lazily loaded and entirely optional.
- `wren/registry.py` — the Skills registry: builds the combined intent list
  and prompt text `brain.py` needs, dispatches intents, and starts each
  skill's optional background task exactly once per process. Deliberately
  does **not** include `gmail_plugin`/`github_plugin` — those are
  Communication plugins, started directly by `run.py`, not intent handlers.

Adding a Skill: write a module in `wren/skills/` exposing `INTENTS`,
`PROMPT_GUIDELINES`, and `async handle(intent, ctx)`, then add it to
`registry.py`'s `PLUGINS` list. Write output with `await ctx.channel.send(...)`
— never import `discord`/`aiohttp`, or the skill stops working on every other
communication plugin. An event-only skill (background task, no user commands)
only needs an optional `async start()`, and should use `router.notify(...)`
to reach a user.

Adding a Communication plugin: create `wren/communication/<name>_plugin.py`
exposing a module-level `ROLE` (`"chat"` or `"input"`), `PLUGIN_NAME`, and —
for `"chat"` — `async start()` plus a `Channel` implementation; for
`"input"`, just `async start()` that polls/watches and calls
`router.notify_name(...)`. Register with `router.register(name, module)` in
`start()` if it can deliver unprompted messages. Add the name to
`COMMUNICATION_PLUGINS` in `.env` to enable it. `telegram_plugin.py` is a
worked stub of exactly this shape.

### Note on upgrading

Pins used to use Discord's own pin feature and had no storage of their own;
they now live in a `pins` table so they work on every surface. Existing
Discord pins are **not** migrated — they remain visible in Discord's pin list,
but need re-pinning through Wren to show up in `what's pinned`.

## Development

```
pip install -r requirements.txt
pytest -q
```

Design docs and implementation plans for each feature live in
`docs/superpowers/specs/` and `docs/superpowers/plans/`.

**Never run ad-hoc/throwaway scripts against the real `wren.db`.** Every
storage module resolves its path through `wren/db.py`, which reads `WREN_DB`
and falls back to a *relative* `wren.db` — the same file the deployed service
uses when run from the repo root. A manual verification script therefore hits
the live database unless you say otherwise. This has already caused real data
loss once (a "throwaway" check that ran `reminders.init_db()` against the real
path, then deleted the file afterward believing it was self-created test
output — it was actually the production DB).

The test suite is safe by construction: `tests/conftest.py` points `WREN_DB`
at a fresh `tmp_path` for every test, so nothing under `pytest` can reach the
real database. Isolate manual checks the same way — set the env var *before*
importing anything from `wren`:

```bash
WREN_DB=$(mktemp -d)/scratch.db python3 -c "
from wren import notes
notes.init_db()
# ... now safe to read/write/delete this path
"
```

Setting `WREN_DB` to an absolute path in `.env` is also the right call for a
deployed install — it stops the database from depending on which directory
the process happened to start in.

Never delete `wren.db` (or any file you didn't create within that same
script) without first confirming its provenance — `git status`/`ls -la`/
`stat` to check it isn't the real, currently-in-use database.

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
