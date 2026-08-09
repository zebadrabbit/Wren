# Transport Split — Migration Plan

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax. Baseline is
> **273 passing tests** — phase 1 is a pure refactor and must end at 273+ green.

**Goal:** Turn Wren from a Discord bot into a transport-agnostic core with
pluggable surfaces. Discord becomes optional; HTTP and a desktop voice client
become possible. See `docs/superpowers/specs/2026-08-07-transport-split-design.md`.

**Ordering principle:** nothing depends on a file that does not exist yet.
Foundation (db/channel/router/config) → core + Discord surface → plugin
fan-out → HTTP → voice → docs.

## Global constraints

- **Never point tests at the real `wren.db`.** `tests/conftest.py` sets
  `WREN_DB` to a temp path *before* any `wren.*` import. Verification scripts
  do the same.
- Plugin contract becomes:
  ```python
  async def handle(intent: str, ctx: Ctx) -> None
  async def start() -> None          # optional, no client arg
  ```
- No plugin imports `discord`. Grep is the acceptance test:
  `grep -l discord wren/*.py` must return nothing.
- Plugin handler *bodies* move verbatim where possible. Only the I/O calls
  change: `message.channel.send(x)` → `ctx.channel.send(x)`,
  `discord_utils.notify*` → `router.notify`.
- No new server dependencies in phases 1–2. `aiohttp` is already present via
  `discord.py`. Phase 3 adds `faster-whisper` as an *optional* extra.
- Deliberate simplifications get a `ponytail:` comment naming the ceiling.

---

## Phase 1 — Foundation

### Task 1.1: `wren/db.py`
- [x] Create `wren/db.py`: `PATH = os.environ.get("WREN_DB", "wren.db")` read
      at call time (not import time, so conftest can set it), and
      `def conn() -> sqlite3.Connection`.
- [x] Replace the `DB_PATH`/`_conn()` pair in `notes.py`, `shopping.py`,
      `reminders.py`, `contacts.py`, `github_state.py` with `db.conn()`.
- [x] Create `tests/conftest.py` — session fixture setting `WREN_DB` to a
      tmp file plus the env vars each test module currently sets by hand.
- [x] Test: `tests/test_db.py` — `conn()` honours `WREN_DB`.

### Task 1.2: `wren/channel.py`
- [x] `Channel` protocol: `send`, `send_file`, `history`, `ack`.
- [x] `Ctx` dataclass: `user_id`, `channel`, `content`, `tags`, `person`, `when`.
- [x] `CollectingChannel` — accumulates into `.sent` (list[str]) and
      `.files` (list[tuple[bytes, str]]); `history()` → `None`; `ack()` → no-op.
- [x] Test: `tests/test_channel.py`.

### Task 1.3: `wren/router.py`
- [x] `register(name, surface)`, `async notify(user_id, text) -> bool`.
- [x] Dispatch to `config.NOTIFY_SURFACE`; log + return `False` when that
      surface is unregistered.
- [x] Test: `tests/test_router.py`.

### Task 1.4: `wren/config.py`
- [x] `DISCORD_TOKEN` → `os.environ.get`, not `_require`.
- [x] Add `SURFACES` (default `discord`), `NOTIFY_SURFACE` (default `discord`),
      `WREN_TOKENS` (parsed `token:user_id` pairs), `WREN_HTTP_HOST`
      (default `127.0.0.1`), `WREN_HTTP_PORT` (default `8787`),
      `WREN_STT_MODEL` (default `base.en`).
- [x] Remove `HUSBAND_ID` from `.env` / `.env.example`.
- [x] Extend `tests/test_config.py`.

---

## Phase 2 — Core and the Discord surface

### Task 2.1: `wren/core.py`
- [x] `async def handle_message(user_id, text, channel) -> None` containing
      `bot.py`'s `on_message` body after the whitelist gate, plus `HELP_TEXT`
      and `_format_uptime`.
- [x] `send_to_person` uses `router.notify`.
- [x] Test: `tests/test_core.py` — first-ever direct test of the dispatch loop.

### Task 2.2: `wren/surfaces/discord.py`
- [x] `DiscordChannel` implementing the protocol (`ack` → `add_reaction`,
      `send_file` → `discord.File`, `history` → existing
      `history_to_messages`).
- [x] `discord_utils.py` moves to `wren/surfaces/discord_utils.py`.
- [x] Module exposes `async def start()` — builds the client, registers with
      `router`, runs. Registers a `notify(user_id, text)` for routing.
- [x] Update `tests/test_discord_utils.py` for the new path.

### Task 2.3: `wren/run.py` and cleanup
- [x] `run.py` — init all DBs, import+start only the surfaces in
      `config.SURFACES`, call `plugins.start_all()`.
- [x] `plugins.start_all()` drops its `client` argument.
- [x] Delete `wren/bot.py`, `wren/progress.py`, `tests/test_progress.py`.
- [x] `wren.service` `ExecStart` → `-m wren.run`; description no longer says
      "Discord Bot".

---

## Phase 3 — Plugin fan-out *(parallelizable — one agent per plugin)*

Each task: change the signature to `handle(intent, ctx)`, swap the I/O calls,
drop the `discord` import, update that plugin's test file to use
`CollectingChannel`. **Handler logic is not rewritten.**

- [x] 3.1 `notes_plugin` — 7 intents; `discord.File` → `ctx.channel.send_file`.
- [x] 3.2 `shopping_plugin` — 4 intents; `send_shopping_list` → `router.notify`.
- [x] 3.3 `reminder_plugin` — 3 intents; `start()` loses `client`, uses `router.notify`.
- [x] 3.4 `email_plugin` — no intents; `start()` loses `client`, uses `router.notify`.
- [x] 3.5 `github_plugin` — no intents; `start()` loses `client`, uses `router.notify`.
- [x] 3.6 `web_plugin` — 2 intents; pure `send` swap.
- [x] 3.7 `contacts_plugin` — 3 intents; generalize "Discord ID" wording in
      `PROMPT_GUIDELINES` to "numeric user ID".
- [x] 3.8 `pins_plugin` — **rewrite.** New `wren/pins.py` (SQLite, mirrors
      `notes.py`: `init_db`, `save`, `all`, `find`, `delete`). Handler logic
      mirrors the notes discard/expand match-count pattern (0 → "No pin found",
      >1 → "be more specific", 1 → act). Drops the 50-pin-limit error.
      Tests: `tests/test_pins.py` + rewritten `tests/test_pins_plugin.py`.

---

## Phase 4 — HTTP surface

- [x] `wren/surfaces/http.py` — aiohttp app; `GET /health`,
      `POST /message`, `POST /voice`.
- [x] Bearer auth via `hmac.compare_digest` against `config.WREN_TOKENS`;
      401 before reaching `core`.
- [x] `POST /message` builds a `CollectingChannel`, awaits
      `core.handle_message`, returns `{"replies": [...], "files": [...]}`
      with files base64-encoded.
- [x] Registers with `router` so `NOTIFY_SURFACE=http` is at least coherent
      (send-only: it logs and returns `False`, since there is no push).
- [x] Test: `tests/test_http_surface.py` — including 401 on bad/missing token.

---

## Phase 5 — Voice

- [x] `wren/stt.py` — lazy-loaded `faster-whisper`; `transcribe(wav_bytes) -> str`.
      Raise a typed error if the library is absent.
- [x] `POST /voice` — transcribe, then run the transcript through
      `core.handle_message`; `503` when STT is unavailable.
- [x] `client/wren_hotkey.py` — global hotkey (`pynput`), record while held
      (`sounddevice`), stdlib `wave` encode, POST, native notification via
      `notify-send`/`osascript`/PowerShell by `sys.platform`.
- [x] `client/requirements.txt` — client-only deps, not installed on the server.
- [x] Test: `tests/test_stt.py` with a stubbed transcriber (no model download).

---

## Phase 6 — Docs and config surface

- [x] `.env.example` — new keys, `HUSBAND_ID` removed, `DISCORD_TOKEN`
      documented as optional.
- [x] `README.md` — architecture section (capability plugins vs surfaces),
      `SURFACES` config, HTTP endpoint table, voice client setup, and the
      one-time pin re-pinning note.
- [x] `requirements.txt` — `aiohttp` listed explicitly (it was transitive);
      `faster-whisper` noted as an optional extra.
- [x] `setup.sh` / `manage.sh` — check for `-m wren.bot` references.

---

## Verification

- [x] `venv/bin/python -m pytest tests/ -q` — 273+ green.
- [x] `grep -l discord wren/*.py` returns nothing (only `wren/surfaces/` may match).
- [x] `SURFACES=http` with `DISCORD_TOKEN` unset starts cleanly — proves
      Discord is genuinely optional.
- [x] `curl` round-trip against `/message` returns a real reply.
- [x] `git status` shows no stray `wren.db` after a test run.
- [x] Real `wren.db` byte-identical to the pre-work backup.
