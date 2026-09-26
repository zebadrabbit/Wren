# Wren Asks Claude — Delegated Builds (DRAFT for review)

Status: **draft, not approved, not implemented.** Written 2026-09-26 while the
owner was away, from their description: *"Wren, ask Claude to make an endpoint
for Slack that lets you ingest, notify and communicate on" — and both of you
work out how that will be created and implemented.* Everything below is a
proposal with the open questions marked; nothing is built until the owner
answers them and approves.

## Why

Wren's plugin contract is small and stable: a communication plugin is a file
that declares `ROLE`, implements `start()` and a `Channel`, and calls
`core.handle_message`; a skill is a file with `INTENTS`, `PROMPT_GUIDELINES`
and `handle()`, wired by one line in `registry.py`. That is exactly the shape
of task an agent builds well from a brief. Today the loop is: the owner opens
a terminal, starts Claude Code, and explains what Wren is. The idea is that
the owner says it *to Wren*, on the phone, and Wren runs the loop.

The `?dry_run=1` flag on `POST /message` and `/voice` (landed 2026-09-26)
already exists partly for this: an agent can exercise the live service against
a discarded database snapshot without touching real data.

## What the owner says, and what happens

1. "Wren, ask Claude to add a Slack channel that can ingest, notify and
   communicate." — a new skill intent, `ask_claude`, with `content` = the
   request as spoken.
2. Wren records a **delegation** (id, request, status, branch, log) and
   answers at once: "Asked. I'll tell you when there's something to look at."
3. A runner starts Claude Code **headless** in a fresh worktree on a branch
   named `delegation-<id>`, with a brief (below). It runs in the background;
   the skill's `start()` polls the delegation's state.
4. Claude reads `CLAUDE.md`, `PROJECT_PLAN.md`, the plugin contract, writes
   the plugin and its tests, runs `pytest`, and checks behaviour through the
   live API with `?dry_run=1` where that applies (e.g. a new skill intent).
5. When it finishes, it writes a short report into the delegation record:
   what it built, what it tested, what the owner has to supply (a Slack bot
   token, a scope list), and what it could not verify. Wren relays that over
   `NOTIFY_VIA`.
6. The owner reviews: "show me the delegation", "merge it", "drop it". Merge
   is a fast-forward of the branch into `main` **by Wren only after the owner
   says so**; a restart of the live service remains the owner's manual step
   (see open questions).

## Components

**`wren/skills/delegate_skill.py`** — intents `ask_claude`, `delegation_status`,
`merge_delegation`, `drop_delegation`. `DESTRUCTIVE = ["drop_delegation"]`;
`merge_delegation` is *not* destructive but is gated on an explicit owner
message, never inferred. Owner-only, like contacts.

**`wren/skills/delegations_store.py`** — one table: id, request, status
(`queued`, `running`, `done`, `failed`, `merged`, `dropped`), branch,
worktree path, started/finished timestamps, report text, last 4 KB of log.

**The runner** — the one real design decision. Three options:

- **A. Headless Claude Code CLI on the same host (recommended).**
  `claude -p "<brief>" --output-format json` run as a subprocess in the
  worktree, under the existing user, with `--allowedTools` restricted to
  Read/Edit/Write/Bash and a `--max-turns` cap. Uses the owner's existing
  Claude Code login; no API key in Wren; the venv and the repo are right
  there. Wren streams stdout to the delegation log.
- **B. Claude Agent SDK.** A Python dependency and an API key in `.env`;
  finer control over tools and budget; costs per token rather than the
  subscription. More code, one more secret.
- **C. Scheduled cloud agent (Claude Code routines).** Runs off-host, needs
  the repo reachable from the cloud, cannot hit the local Ollama or the
  local API for dry runs. Rejected for this reason alone.

Recommendation: A. The whole point is that Claude works *inside* this
install, next to the model and the live API.

**The brief** — assembled by Wren, not typed by the owner. It contains: the
request verbatim; the pointers `CLAUDE.md` already gives an agent (hard rules,
plugin contract, dev loop); the delegation's branch and worktree; the
constraint that main is never touched; the instruction to run `pytest -q`;
the dry-run recipe with a token Wren generates for the run and revokes after;
and the required shape of the final report. It ends with "when done, write
REPORT.md in the worktree root and stop."

**Safety rails** — a delegation can only: write inside its worktree, run the
test suite, and call the local API with `?dry_run=1` (its token is minted
with a `dry_run_only` marker the API enforces). It cannot push, merge, edit
`.env`, restart the service, or reach other repos. `git stash` is forbidden
in the brief (a known foot-gun with parallel sessions). Two delegations do
not run at once.

## The Slack example, end to end

"Add a Slack channel that can ingest, notify and communicate" maps onto the
existing contract precisely, which is why the brief can be exact:

- `ROLE = "chat"` — input and output, like Discord and Telegram.
- `start()` — Socket Mode over `aiohttp` (no new dependency: the Bolt SDK is
  not needed for one DM bot), authenticating with a bot token from `.env`.
- A `SlackChannel` with `send`, `send_file`, `send_card` (prose fallback,
  like Telegram), `history`, `ack` (reaction emoji, like Discord).
- `notify(user_id, text)` for reminders and the briefing, and an entry in
  `contacts.SURFACES` so a person's Slack id maps to their Wren id.
- Tests in the shape of `test_telegram_plugin.py`: fake the API, assert what
  reaches `core.handle_message`.

What Claude would report back as needing the owner: the Slack app creation
(one click), the bot token, the app-level token for Socket Mode, and the
Slack user id to map to the owner. What it could verify without them: every
test, and `?dry_run=1` for anything text-side.

## Open questions for the owner

1. **Runner**: A (headless CLI on this host) as recommended, or B (SDK, API
   key)? A assumes `claude` is installed and logged in on the Wren host.
2. **Merge authority**: Wren merges on "merge it", or Wren only reports and
   the owner merges from a terminal as today? (The draft says Wren merges on
   an explicit message; the restart stays manual.)
3. **Restart**: leave manual (current), or add a systemd path unit / a
   polkit rule so `manage.sh restart` works without sudo? This is the only
   step in the loop Wren cannot do today.
4. **Budget**: a max-turns cap per delegation, and a daily cap on delegations?
5. **Visibility**: a `delegations` card in the web chat, or prose only?

## Out of scope for the first slice

Concurrent delegations; delegations that change `.env` or secrets; Wren
restarting itself; delegating to any agent other than Claude Code; a general
"ask Claude a question" chat passthrough (the owner has that already).
