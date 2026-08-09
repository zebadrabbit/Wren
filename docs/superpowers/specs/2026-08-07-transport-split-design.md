# Transport Split — Wren as a General Bot — Design

## Goal

Wren is a Discord bot. Every plugin imports `discord`, every reply goes out
through `message.channel.send(...)`, and `config.py` calls
`_require("DISCORD_TOKEN")` at *import time* — so Wren cannot start at all
without a Discord account. For a self-hosted application that is the wrong
shape: Discord should be one way to reach Wren, not a precondition for
running it.

This work turns Wren into a transport-agnostic core with **surfaces**
plugged into it. Discord becomes the first surface. A local HTTP surface
becomes the second, and a desktop hotkey client speaks to it so voice input
works from any machine on the LAN.

## The two axes

Wren already has a plugin system, but "Discord" is not a peer of
`notes_plugin`. There are two different kinds of thing:

- **Capability plugins** (`notes`, `shopping`, `reminders`, `web`, `pins`,
  `contacts`, `email`, `github`) — *what Wren can do*. These stay exactly as
  they are, minus their Discord imports.
- **Surfaces** (`discord`, `http`) — *how you reach Wren*. New concept.

Nothing about the capability-plugin contract changes except its I/O.

## Scope

This spec covers all three phases, which ship in order:

1. **Core/transport split** — the `Channel` seam, `router.notify`, Discord
   demoted to an optional surface. Zero user-visible behavior change.
2. **HTTP surface** — a local aiohttp server, bearer-token auth, so `curl`
   can drive Wren.
3. **Voice** — server-side speech-to-text plus a desktop hotkey client.

## Decisions taken

| Question | Decision | Why |
|---|---|---|
| Notification routing | One configured default surface per install | No dedupe problem, one config key. Replies still go back where asked. |
| Desktop client | Native hotkey daemon, no browser extension | A true OS-global hotkey is the one thing an extension genuinely cannot do. Python already runs on win/linux/mac. |
| Topology | Home LAN, shared bearer token | This box stays the always-on brain; other machines are thin clients. |
| Push to desktop | None — send only | Reminders go to the configured surface. No persistent connection, no subscriber registry, no reconnect logic. |
| Identity | `user_id` stays the integer it already is | It is currently a Discord snowflake threaded through every table. Renaming the *concept* costs nothing; migrating the column costs a rewrite. |
| STT location | Server-side, on the box with the GPU | Matches the existing local-LLM posture. Nothing leaves the house. |
| HTTP library | `aiohttp` | Already installed as a `discord.py` dependency. Zero new deps. |

### Identity, precisely

`user_id` stops meaning "Discord user ID" and starts meaning "Wren user ID,
which on this install happens to have come from Discord." No table changes.
HTTP clients authenticate with a token that maps to a `user_id`:

```
WREN_TOKENS=s3cret:412341234123,other:998877665544
```

Routing to Discord is then free, because the ID already *is* the snowflake.

## Architecture

```
wren/
  db.py          # NEW — one conn(), one env-configurable path
  channel.py     # NEW — Channel protocol, CollectingChannel
  router.py      # NEW — surface registry + notify(user_id, text)
  core.py        # NEW — transport-free dispatch (was bot.py's on_message)
  run.py         # NEW — entrypoint; starts enabled surfaces
  pins.py        # NEW — pin storage (pins_plugin had none)
  stt.py         # NEW — speech-to-text (phase 3)
  surfaces/
    discord.py   # NEW — client, DiscordChannel, history, reactions
    http.py      # NEW — aiohttp app (phase 2)
  plugins.py     # registry; start_all() loses its client arg
  *_plugin.py    # handle(intent, ctx); no discord import
  brain.py       # untouched
  notes.py shopping.py reminders.py contacts.py github_state.py web.py
                 # storage; DB_PATH replaced by db.conn()
client/
  wren_hotkey.py # NEW — desktop client (phase 3)
```

Deleted: `bot.py` (dissolved into `core.py` + `surfaces/discord.py`),
`progress.py` (dead code — nothing imports it but its own test),
`discord_utils.py` (moves under `surfaces/`).

### `channel.py`

```python
class Channel(Protocol):
    async def send(self, text: str) -> None: ...
    async def send_file(self, data: bytes, filename: str) -> None: ...
    async def history(self, limit: int = 10) -> list[dict] | None: ...
    async def ack(self, state: str) -> None: ...   # "seen" | "done" | "error"

@dataclass
class Ctx:
    user_id: int
    channel: Channel
    content: str
    tags: list[str]
    person: str | None
    when: str | None
```

`ack` is how the 👀 / ✅ / ❌ reactions survive without leaking Discord into
core: Discord implements it as `add_reaction`, every other surface no-ops.

`CollectingChannel` accumulates sends into a list. It is what the HTTP
surface returns as JSON *and* what tests assert against — the Discord mocks
in the existing suite go away.

### `router.py`

```python
def register(name: str, surface) -> None
async def notify(user_id: int, text: str) -> bool
```

A surface registers itself at startup if it can deliver unprompted messages.
`notify` dispatches to `config.NOTIFY_SURFACE`, returning `False` (logged) if
that surface is not registered. `reminder_plugin`, `email_plugin`, and
`github_plugin` swap `discord_utils.notify*` for `router.notify`.

### `core.py`

```python
async def handle_message(user_id: int, text: str, channel: Channel) -> None
```

Everything in `bot.py`'s `on_message` after the whitelist gate moves here
verbatim: history fetch, `detect_intent`, plugin dispatch, and the
`help` / `status` / `list_plugins` / `send_to_person` / `chat` branches.
`HELP_TEXT` comes with it.

Surfaces own only: authenticating the user, building a `Channel`, and
calling `handle_message`.

### Optional Discord

`config.DISCORD_TOKEN` becomes `os.environ.get(...)`, not `_require(...)`.
`config.SURFACES` is a comma-separated list (default `discord`), and
`run.py` starts only what is listed. A self-hoster with no Discord account
sets `SURFACES=http` and Wren runs.

`surfaces/discord.py` is only imported when Discord is enabled, so `discord.py`
becomes an optional dependency in practice.

## Phase 2 — HTTP surface

`aiohttp` app, bound per `WREN_HTTP_HOST` / `WREN_HTTP_PORT`
(default `127.0.0.1:8787` — LAN exposure is opt-in, not the default).

| Route | Body | Returns |
|---|---|---|
| `GET /health` | — | `{"ok": true}` |
| `POST /message` | `{"text": "..."}` | `{"replies": [...], "files": [...]}` |
| `POST /voice` | `audio/wav` bytes | `{"transcript": "...", "replies": [...]}` |

Auth is `Authorization: Bearer <token>`, compared with
`hmac.compare_digest` against `config.WREN_TOKENS`. A request with no or
unknown token gets `401` and never reaches `core`. `files` are
base64-encoded, since `notes_plugin`'s export is the only producer and it is
small.

## Phase 3 — Voice

**Server:** `stt.py` wraps `faster-whisper`, loading the model lazily on
first use and caching it. `WREN_STT_MODEL` defaults to `base.en`. If
`faster-whisper` is not installed, `/voice` returns `503` and everything else
keeps working — STT is an optional extra, not a hard dependency.

**Client:** `client/wren_hotkey.py`. Holds a global hotkey (`pynput`),
records while held (`sounddevice`), writes a WAV with the stdlib `wave`
module, POSTs it, and shows the reply as a native notification via
`notify-send` / `osascript` / PowerShell — chosen by `sys.platform`, so no
notification dependency. Client deps live in `client/requirements.txt` and
are not installed on the server.

No tray icon. A background process with a global hotkey is the whole
requirement; an icon is decoration that costs a fourth dependency.

## Consequences worth stating

**`contacts_plugin`'s prompt guidelines were reworded.** "raw numeric Discord
ID" became "raw numeric user ID". This edits the LLM system prompt, so it is a
third behaviour-affecting change beyond the two error strings — small, and
correct for a bot that is no longer Discord-specific, but not a no-op.

**Pins get rewritten, not moved.** `pins_plugin.py` today has no storage —
it calls `message.channel.pins()` and `message.pin()`, i.e. Discord's own
pin feature. That cannot work on any other surface. So pins move to a
SQLite table mirroring `notes.py`. Side effect: Discord's 50-pin limit stops
applying, and the "you may be at Discord's 50-pin limit" error disappears.

*Migration:* existing pins live in Discord and are not copied over. They
remain visible in the Discord channel's pin list, so nothing is lost — but
they must be re-pinned to appear in `list_pins`. This is a one-time manual
step, called out in the README.

**History is Discord-only.** Discord gives conversation history for free by
reading back the channel (`bot.py:126`). HTTP has none unless we store it.
We do not store it: `history()` returns `None` there and voice stays
one-shot. `brain.detect_intent` already handles `history=None`.

The bound this creates: no multi-turn follow-ups over voice. `web_plugin`'s
"read me the first one" still works, because it keys off its own
`_last_results` dict rather than chat history — but a bare "what did I just
say" will not. Marked with a `ponytail:` comment naming the upgrade path.

**`DB_PATH` becomes `WREN_DB`.** Five modules each declare
`DB_PATH = "wren.db"` — a *relative* path, so which database you get depends
on the working directory. One `wren/db.py` replaces all five copies and
reads `WREN_DB` from the environment. This also lets `tests/conftest.py`
point the whole suite at a temp file, permanently fixing the known hazard
where running the tests writes a stray `wren.db`.

**`HUSBAND_ID` is dead.** Contacts replaced it on 2026-07-13 but it is still
in `.env`. Swept.

## Testing

The suite is 273 passing tests today and must still be 273+ green at the
end, since phase 1 is a pure refactor.

- `tests/conftest.py` points `WREN_DB` at a `tmp_path` and sets the env vars
  every test module currently sets by hand.
- Plugin tests drop their Discord mocks in favour of `CollectingChannel`,
  asserting on `channel.sent` — a plain list of strings.
- `core.py` becomes directly testable for the first time; `bot.py` never was,
  because its module-level `client.run(...)` made it unimportable.
- New: `test_channel.py`, `test_router.py`, `test_core.py`, `test_db.py`,
  `test_pins.py`, `test_http_surface.py`, `test_stt.py`.
- `stt.py` tests stub the transcriber — no model download in CI.

Verification runs against a temp `WREN_DB`, never the real `wren.db`.

## Explicitly not doing

- Browser extension — the mic + notification job it was for is fully covered
  by the hotkey client.
- WebSocket / SSE push — send-only, per the routing decision.
- Multi-turn history on non-Discord surfaces.
- TLS / public exposure — LAN plus a bearer token is the stated topology.
- Text-to-speech replies.
- A user table or ID migration.
