# Web Plugin Management — Design

## Goal

Wren can tell you *that* a plugin is off — `list plugins` prints a checkmark or
a pause icon — but never *why*, and there is no way to change anything without
editing `.env` and restarting the service. This adds a plugins panel to the web
chat: see every skill and channel with the specific reason any of them is
inactive, switch skills on and off, and edit the handful of settings that are
not credentials.

## What this introduces

Wren has had exactly one piece of runtime-mutable state since it was built:
`contacts`, a SQLite table the owner edits by DM. Everything else comes from
`.env`, is read once at import, and can only change by restarting.

This adds the second, and it deliberately copies the first: a small SQLite
table, the same module shape as `wren/contacts.py`, edited at runtime by the
owner. `.env` stays the source of defaults. It is never written to.

## Decisions taken

| Question | Decision | Why |
|---|---|---|
| Scope | Status + reasons, skill on/off, non-secret settings | Credentials stay out — see below. |
| Credentials in the UI | **No** | The bearer token currently buys a chat window. Letting it read and rewrite `DISCORD_TOKEN` and `IMAP_PASSWORD` over plain HTTP with no TLS turns one stolen token into total credential loss. |
| Where settings live | SQLite table overriding `.env` | Wren never rewrites a file holding every secret it owns. A botched write to `.env` means Wren will not boot at all. |
| Who may change them | Owner only, applies globally | One process, one shopping list, one set of watchers — the settings genuinely are global. `OWNER_ID` is already privileged in `config`. |
| Placement | Slide-over panel from a gear in the sidebar | One page, one bundle, no second HTML file or route. Open, flip, close. |
| Switching a skill off | Stops Wren offering it; in-flight work still completes | The alternative silently swallows reminders the user explicitly asked for. See "Reminders". |
| Where the logic lives | `settings.py` + `registry.py`, with `webchat.py` as thin HTTP | Hard rule 2: a communication plugin contains no domain logic. Also makes the capability channel-agnostic for free. |

## Architecture

```
panel (chat.html)
  │  GET /api/plugins · PATCH /api/plugins/{name} · PATCH /api/settings
  ▼
webchat.py            authn + owner check, nothing else
  │
  ├─▶ registry.py     is_enabled / set_enabled, enabled-aware intent lists
  └─▶ config.py       SETTABLE allowlist, validation, apply_overrides()
            │
            ▼
       settings.py    SQLite key/value — the only thing that persists
```

### The settings table

`wren/settings.py`, new, roughly forty lines and shaped exactly like
`contacts.py`:

```python
def init_db()                 # settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT)
def get(key) -> str | None
def set(key, value) -> None
def unset(key) -> bool
def all() -> dict[str, str]
```

Registered in `run.py`'s `_STORAGE` tuple so the table is created at boot like
every other store.

**The table stores only deviations from `.env`.** No row means "whatever `.env`
said". That is what makes `unset()` meaningful as *revert to the file* rather
than *set to empty*, and it means a fresh install has an empty table and
behaves exactly as it does today.

### SETTABLE is the security boundary

`config.py` gains a dict of every setting the UI may touch, mapping each name
to a coercer and a validator:

```python
SETTABLE = {"TIMEZONE": ..., "REMINDER_POLL_SECONDS": ..., ...}
def apply_overrides() -> None
```

This is a **positive allowlist, not a denylist**. `DISCORD_TOKEN` is not in it,
so no request — malformed, malicious, or merely mistaken — can reach it. Adding
a key to this dict is the one line a reviewer must look at.

The editable set, all non-secret:

| Setting | Validation |
|---|---|
| `TIMEZONE` | the existing `_validate_timezone` |
| `REMINDER_POLL_SECONDS`, `EMAIL_POLL_SECONDS`, `GITHUB_POLL_SECONDS` | int, **minimum 5** |
| `SEARXNG_URL`, `FIRECRAWL_URL` | string, may be empty to disable |
| `GITHUB_WATCH`, `EMAIL_WATCH` | the existing `_parse_github_watch` / `_parse_email_watch` |
| `NOTIFY_VIA` | must name a channel registered with `router` whose `CAN_NOTIFY` is true |

`apply_overrides()` reads the table and `setattr`s onto the `config` module.
That is the entire live-apply mechanism, and it works only because **every
consumer reads `config.X` at call time** — including `asyncio.sleep(
config.REMINDER_POLL_SECONDS)` inside the reminder loop. Nothing in `wren/`
captures a config value into a module-level constant at import. The test suite
already depends on this, via `monkeypatch.setattr(config, ...)`.

If that ever stops being true, live edits silently stop working for whichever
value got captured. Worth a comment at `apply_overrides`.

### Enabled-ness in the registry

```python
def is_enabled(plugin) -> bool     # key "skill.<module>.enabled", default True
def set_enabled(plugin, on) -> None
```

Default True, so a skill with no row is on and the table only records what the
owner changed.

`all_intents()` and `all_guidelines()` filter to enabled skills, so the LLM is
never told a disabled skill exists.

**Filtering alone is not enough, and this is the trap.** `core.py` calls
`brain.register_plugins(registry.all_intents(), registry.all_guidelines())`
once, at module import — the intent list and prompt text are copied into
`brain`'s module globals at startup and never re-read. A toggle that only
changes what `all_intents()` returns would therefore never reach the LLM.

So `set_enabled()` re-registers with `brain` after every write, using a
function-local `from . import brain`. Local because it is the one choke point
every writer goes through, which makes it impossible for a future caller — a
Discord admin command, say — to forget; and function-local keeps `registry`'s
module-level imports free of `brain`. `INTENT_HANDLERS` is deliberately left
alone: it is a plain module-level dict that `core` indexes directly and several
tests assert against, and converting it to a function would churn every caller
to no benefit. Instead `core` gains one guard at dispatch — a disabled skill's
intent is treated as unknown and falls through to chat, which covers a model
emitting an intent it was never offered.

### Ownership is the security boundary

All three routes require `user_id == config.WHITELIST["owner"]` and return 403
otherwise. This mirrors the web chat's existing rule that a token maps to a
user id and one user cannot reach another's conversations.

**`GET /api/plugins` returns current setting values, and that is safe only
because `SETTABLE` excludes secrets.** If a credential is ever added to that
dict, this endpoint leaks it. The two rules stand or fall together.

The honest description of the new exposure: the bearer token already reaches
your notes and your shopping list over plain HTTP. This adds "can change the
timezone and switch off a skill" — a household annoyance, not credential theft.
That is precisely why credentials are out of scope.

### Endpoints

| Route | Body | Returns |
|---|---|---|
| `GET /api/plugins` | — | `{skills: [...], channels: [...], settings: {...}}` |
| `PATCH /api/plugins/{module}` | `{enabled: bool}` | the updated entry |
| `PATCH /api/settings` | `{KEY: value}` | the updated settings map |

Each skill and channel carries `module`, `name` (its `PLUGIN_NAME`), `active`,
and — when inactive — `reason`.

`{module}` is the **module basename**, e.g. `notes_skill`, not the display
name — it is what the settings key `skill.<module>.enabled` is built from, and
unlike `PLUGIN_NAME` it cannot change under a stored row. A `{module}` that is
not a registered skill returns 404, and **a channel module returns 400**:
channels are not toggleable, and silently accepting the write would be worse
than refusing it.

### Enumerating channels

Skills come from `registry.PLUGINS`. Channels cannot: `run.py` only imports the
ones named in `COMMUNICATION_PLUGINS`, but the panel has to show the ones you
have *not* enabled — that is the whole point of "Telegram — no `TELEGRAM_TOKEN`".

So the channel list is discovered: scan `wren/communication/` for `*_plugin.py`,
import each, and read `ROLE`, `PLUGIN_NAME`, and the optional `is_active()` /
`inactive_reason()`. A channel is reported as running when its name appears in
`config.COMMUNICATION_PLUGINS`. Importing a module that is not enabled is safe —
these files define constants and functions at module level and start nothing
until `start()` is awaited.

### Reporting *why* something is off

`is_active()` returns a bool and no explanation. The convention gains an
optional sibling:

```python
def inactive_reason() -> str
```

Defined only on the four plugins that can be misconfigured (`web_skill`,
`gmail_plugin`, `github_plugin`, `telegram_plugin`); everything else falls back
to a generic string. This follows the existing optional-`is_active()` pattern
rather than inventing a parallel one, and it keeps the reason next to the code
that knows it instead of in a lookup table in the UI layer.

### Boot order, and the ordering subtlety

`run.py` must run `init_dbs()` → `config.apply_overrides()` → *then* start
plugins. Apply earlier and the settings table does not exist yet; apply later
and a plugin has already read a stale value during startup. The call gets a
comment saying so, because it looks arbitrary and is not.

### What still needs a restart

`COMMUNICATION_PLUGINS` is read once by `run.py` when it decides which channels
to import and start, so channels cannot hot-swap. The CHANNELS section of the
panel is therefore **read-only status plus guidance** — no toggle that silently
does nothing. Turning Telegram on remains two lines in `.env` and a restart.

### Reminders

Reminders is the only skill with a background task. Switching it off removes
its intents, so "remind me to X" stops working, but the poller keeps running
and a reminder already set for 6pm still arrives.

This is a deliberate choice and it looks like a bug until you know that. Only
the poll loop marks a reminder fired, so a stopped poller means due reminders
pile up unfired and then all deliver at once on re-enable. Suppressing the
poller *and* marking them fired would silently destroy something the user
explicitly asked for. Offering-off, honouring-what-exists is the only option
that loses nothing. There is a test asserting it.

Worth knowing while reading that loop, because an earlier draft of this spec
got it backwards: `mark_fired()` is called **unconditionally**, outside the
`if not ok` branch — a reminder is consumed once delivery has been *attempted*,
not once it has succeeded. That is not a bug either. It follows `router.notify`'s
documented contract, where `False` means a permanent failure and the caller is
expected to consume what it was delivering; transient failures raise instead, so
they never reach that line.

### The panel

A slide-over in `chat.html`, opened by a gear beside the existing "New" button,
built the same way as the rest of that page: no framework, no build step. Three
sections — SKILLS with toggles, CHANNELS as read-only status, SETTINGS as
inputs. Non-owners never see the gear, and the routes reject them regardless.

## Failure modes

| Case | Behaviour |
|---|---|
| Invalid value (bad timezone, non-numeric interval, interval < 5) | 400, **nothing written**, `config` untouched |
| `NOTIFY_VIA` naming a channel that is not running or cannot push | 400, echoing the same reasoning `run.py` warns with at boot |
| Stored key no longer in `SETTABLE` after an upgrade | Ignored at apply time with a warning — never a crash, or Wren could fail to boot on a row it wrote itself |
| DB write fails | 500, `config` unchanged — the write happens before the `setattr` |
| Every skill disabled | Allowed. Wren still chats. No special case. |
| Non-owner calls any route | 403 |

Validation runs before the write, and the write before the `setattr`, so a
rejected value never reaches the database and a failed write never leaves
memory ahead of disk. Coercion happens during validation, so the value is
already known-good by the time it is applied.

## Testing

House style: build a fake, call the thing, assert on plain values.

- `settings`: set/get/unset/all round-trip; `unset` falls back to the `.env` value
- `config.apply_overrides`: coerces types; an unknown key warns rather than raising
- **the allowlist**: attempting to set `DISCORD_TOKEN` is rejected — the security test
- interval floor: `0` and `1` are rejected, `5` is accepted
- `registry.is_enabled` defaults True; a disabled skill disappears from
  `all_intents()` and `all_guidelines()` while `INTENT_HANDLERS` is unchanged
- `core`: a disabled skill's intent falls through to chat, via `CollectingChannel` + `Ctx`
- `webchat`: non-owner gets 403; owner round-trips a change; an invalid value
  returns 400 **and persists nothing**
- toggling a *channel* module returns 400, and an unknown module returns 404
- channel discovery lists Telegram as present-but-not-enabled while it is
  absent from `COMMUNICATION_PLUGINS` — the case the panel exists to show
- **disabling Reminders leaves the poller firing** — encoding the decision above,
  so a later reader does not "fix" it into swallowing reminders

## Explicitly not doing

- **Editing credentials.** The reason this design is safe.
- **Enabling or disabling channels from the UI.** Needs a restart; a toggle that
  silently does nothing is worse than no toggle.
- **Per-user settings.** The watchers and the reminder poller are process-wide
  loops; "off for me" cannot be true, so offering it would be a lie.
- **An audit log of who changed what.** Owner-only means there is exactly one
  who.
- **Chat intents for configuration** ("turn off web lookup" from Discord).
  Tempting given the architecture, and the store is channel-agnostic so it stays
  possible — but configuration wants a deterministic switch, not LLM intent
  detection.
- **Restarting the service from the UI.** Wren would be killing the process
  serving the request. Out of scope, and probably a bad idea generally.
