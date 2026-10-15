# More doors: string contact ids, Slack, Matrix — design

Date: 2026-10-15. Planka: Phase 10 — More Doors (cards "Contact ids as
strings", "Slack chat plugin (Socket Mode)", "Matrix chat plugin").

## Goal

A self-hoster reaches Wren from Slack or Matrix with the posture Telegram
already has: the bot dials out, nothing listens on a public port, no new
Python dependency, one timeline per person. Both are `ROLE = "chat"` doors
and need nothing from core or any skill.

## Out of scope

Mattermost/Rocket.Chat, email as a two-way door, Signal (cards exist).
Slack channels, threads, slash commands, Block Kit. Matrix group rooms and
end-to-end encryption. Persisting a door's cursor across restarts (the
Telegram offset is process-local too; same `ponytail:` note).

## 1. Contact ids as strings

A contact's **per-surface id** becomes a string in the store's interface;
the **Wren id** a person's rows are filed under stays an `int`, so core,
`WREN_TOKENS`, the owner id and every store are untouched.

- `contacts.SURFACES = ("discord", "telegram", "slack", "matrix")`. `init_db`
  adds a missing `<surface>_id TEXT UNIQUE` column with `ALTER TABLE` (a
  `UNIQUE` index created separately, since `ALTER ... ADD COLUMN` cannot
  carry one), the same shape as the `wren_id` backfill.
- `contacts.ID_SHAPE`, one compiled regex per surface, is the only place an
  id's shape is known: `discord`/`telegram` `^\d+$`; `slack`
  `^[UW][A-Z0-9]{8,}$`; `matrix` `^@[^:\s]+:[^\s]+$`.
- `contacts.register(alias, surface, raw_id, reserved=())` takes the id as
  the string the person typed (stripped) and validates it against
  `ID_SHAPE`, raising `contacts.Invalid("That doesn't look like a <surface>
  id.")`. Both doors drop their own digit checks and relay the message (chat
  reply; HTTP 400). `Taken` stays a 409.
- Minting the Wren id in `add()`: a numeric surface id is the Wren id, as
  today. For a string-id surface, `wren_id = int.from_bytes(sha256(
  f"{surface}:{id}").digest()[:7], "big")` (56-bit, deterministic: a person
  removed and re-added keeps their notes). The web already returns ids as
  strings, so size is not a JSON concern.
- Readers return the per-surface id in its native type: `get()["slack"]`
  and `surface_id("slack", wren_id)` are `str`; `get()["discord"]` and
  `surface_id("discord", ...)` stay `int`. `all()`, `wren_id(...)` and
  `surfaces_of(...)` keep their types. `set_id(alias, surface, surface_id)`
  accepts `int | str`.
- `GET /api/me` gains `"surfaces"`: the entries of `config.COMMUNICATION_PLUGINS`
  that are in `contacts.SURFACES`, in that order. The People tab draws its
  add form and per-person id list from that instead of the hardcoded pair,
  with a placeholder per surface (`123456789012345678`, `123456789`,
  `U0123ABCDEF`, `@name:home.net`). Contact rows keep carrying every
  surface in `contacts.SURFACES` (null where unset).
- `chat.html` `DOOR` labels gain `slack: "Slack"` and `matrix: "Matrix"`.
- `_default_surface()` in the chat skill is unchanged (first configured
  surface with ids of its own).

## 2. Slack door (`wren/communication/slack_plugin.py`)

### Settings (all `.env`, read at import)

- `SLACK_APP_TOKEN` (`xapp-…`, app-level, scope `connections:write`) and
  `SLACK_BOT_TOKEN` (`xoxb-…`, bot scopes `chat:write`, `im:history`,
  `im:read`, `im:write`, `reactions:write`, `files:read`, `files:write`;
  event subscription `message.im`). Both join `SECRET_KEYS` in `config.py`
  and `manage.sh`.
- `SLACK_OWNER_ID` (`U…`): the owner's Slack id, mapped onto
  `WREN_OWNER_ID` both ways like `TELEGRAM_OWNER_ID`. Always needed when the
  owner uses Slack, because a Wren id is an int.
- `is_active()` is both tokens set; `inactive_reason()` names what is
  missing. `start()` raises `RuntimeError` naming the missing token.

### Transport

Socket Mode. `start()`: `POST https://slack.com/api/apps.connections.open`
(bearer app token) → `url`; `aiohttp.ClientSession.ws_connect(url)`; read
JSON frames:

- `hello`: ignore. `disconnect`: close and reconnect (new `open` call).
- `events_api`: send `{"envelope_id": …}` **before** handling (Slack
  retries unacked envelopes after 3 s, which would answer twice), then
  handle `payload.event`.
- Anything else (`slash_commands`, `interactive`): ack and ignore.

The loop has Telegram's shape: one `_run_once()` per connection, awaited
inline handling so replies stay in order, `except Exception` → warning and
exponential backoff (`5 s` doubling to `300 s`), `CancelledError`
re-raised. Every Web API call is `_api(method, json=…)` to
`https://slack.com/api/<method>` with the bot token in the `Authorization`
header, so no URL ever carries a token; `_api` raises `SlackError(method,
error)` when `ok` is false. Error strings are scrubbed of both tokens before
they can reach a log, the way `telegram_plugin._api` scrubs.

### Incoming

`_incoming(event) -> (slack_user_id, text, wanted_files) | None`. Acted on:
`type == "message"`, `channel_type == "im"`, no `bot_id`, `subtype` in
`(None, "file_share")`. Text is `text`; files are `files[]` entries whose
`mimetype` is in `filetypes.ACCEPTED` and `size <= filetypes.MAX_BYTES`,
fetched from `url_private_download` with the bot token. Over-size and failed
downloads reply as Telegram does. Stale guard: `event.ts` older than 30
minutes gets the "I was offline" notice instead of an answer (Socket Mode
redelivers for a while after a reconnect).

`core.handle_message(_wren_user_id(slack_user), text, SlackChannel(channel,
ts), files=…)`. `_wren_user_id(U…) -> int | None`: `WREN_OWNER_ID` when it
equals `SLACK_OWNER_ID`, else `contacts.wren_id("slack", U…)`, else `None`.
A sender with no Wren id is dropped by the door before core is called (core
would drop it too, but it takes an int): a stranger gets no answer, as on
Telegram.

### Channel

`SlackChannel(channel_id, ts)`, `NAME = "slack"`.

- `send(text)`: `chat.postMessage {channel, text}` per chunk from
  `chunking.chunks(text, 4000)` (Slack truncates at 4000 for display).
- `send_file(data, filename)`: the three-call upload
  (`files.getUploadURLExternal` → `POST` bytes to the URL →
  `files.completeUploadExternal {files:[{id}], channel_id}`).
- `send_card(...)`: the prose, as Telegram.
- `ack(state)`: `reactions.add {channel, timestamp: ts, name}` with
  `eyes` / `white_check_mark` / `x`, failures logged, never raised.

### Notify

`notify(wren_id, text)`: Slack id is `SLACK_OWNER_ID` for the owner, else
`contacts.surface_id("slack", wren_id)`; none → `False` (not on Slack:
permanent). `chat.postMessage {channel: <user id>}` opens the DM. `False`
only on `channel_not_found`, `user_not_found`, `cannot_dm_bot`,
`user_disabled`, `account_inactive`; everything else (`ratelimited`,
`invalid_auth`, transport) raises so the caller retries.

## 3. Matrix door (`wren/communication/matrix_plugin.py`)

### Settings

- `MATRIX_HOMESERVER` (`https://matrix.example.org`), `MATRIX_TOKEN` (an
  access token for the bot account, from a login or Element's "Help & About";
  joins `SECRET_KEYS` both places), `MATRIX_OWNER_ID` (`@you:home.net`,
  mapped onto `WREN_OWNER_ID` both ways). The bot's own id comes from
  `GET /_matrix/client/v3/account/whoami` at start, so there is no
  `MATRIX_USER` setting.
- `is_active()` is homeserver and token set.

### Transport

Client-Server API over aiohttp, bearer token in the header. `_api(method,
path, *, json=None, params=None, data=None)` → the JSON body; raises
`MatrixError(errcode, status)` on a non-2xx (`errcode` from the body).
Scrub the token from any error text as the others do.

`start()`: `whoami` → `_me`; ensure the owner's DM room (below); then the
sync loop. `GET /sync?timeout=30000&since=<next_batch>` with a filter that
lazy-loads members and drops presence and ephemeral events. The **first**
sync (no `since`) is history: take `next_batch`, learn room membership,
act on nothing. Each later batch:

- `rooms.invite`: join when the inviter (`invite_state` `m.room.member`
  sender) is a known person (owner or a contact with a matrix id); ignore
  anyone else.
- `rooms.join[room].timeline.events`: `m.room.message` from a sender that
  is not `_me`, in a room the cache says is a DM (exactly two joined
  members). `msgtype` `m.text` → `body`; `m.image` / `m.file` → download
  `url` (`mxc://server/id`) from
  `GET /_matrix/client/v1/media/download/{server}/{id}` with the token,
  `filetypes` checks as the others; the caption is `body` when it differs
  from `filename`. Stale guard on `origin_server_ts`, 30 minutes.
- An encrypted room (`m.room.encrypted` events, or `m.room.encryption` in
  its state) is logged once per room and skipped: Wren cannot read it in
  this version. The README says to let Wren open the room.

Room cache: `room_id -> set(joined members)`, built from `m.room.member`
state and timeline events in every sync; `GET /rooms/{id}/joined_members`
on a cache miss.

`core.handle_message(_wren_user_id(sender), body, MatrixChannel(room_id),
files=…)`; `_wren_user_id` mirrors Slack's.

### Rooms Wren opens

`_dm_room(matrix_id)`: the cached joined room whose members are exactly
`{_me, matrix_id}`; else `POST /createRoom {"is_direct": true, "invite":
[matrix_id], "preset": "trusted_private_chat"}` (unencrypted) and cache it.
`start()` calls it for `MATRIX_OWNER_ID` so the owner's first contact with
Wren is accepting an invite, never finding the encryption toggle. `notify`
calls it for a contact.

### Channel

`MatrixChannel(room_id)`, `NAME = "matrix"`.

- `send(text)`: `PUT /rooms/{room}/send/m.room.message/{txn}` with
  `{"msgtype": "m.text", "body": part}` per chunk of 32000 chars (the
  event ceiling is 64 KiB); `txn` is a uuid4.
- `send_file(data, filename)`: `POST /_matrix/media/v3/upload?filename=`
  → `content_uri`; then an `m.image` (for `filetypes.IMAGE_MIMES`) or
  `m.file` event with `url` and `info.mimetype`.
- `send_card(...)`: the prose.
- `ack("seen")`: `PUT /rooms/{room}/typing/{_me} {"typing": true,
  "timeout": 30000}`; other states no-op; failures logged.

### Notify

`notify(wren_id, text)`: Matrix id is `MATRIX_OWNER_ID` for the owner,
else `contacts.surface_id("matrix", wren_id)`; none → `False`. Send into
`_dm_room(matrix_id)` (a fresh room delivers the message when the invite
is accepted: `trusted_private_chat` shares history). `False` on
`M_FORBIDDEN` from the send (Wren was removed from the room); everything
else raises.

## 4. Shared wiring

- `config.py`: the six new settings beside the Telegram ones;
  `SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`, `MATRIX_TOKEN` in `SECRET_KEYS`;
  `manage.sh` `SECRET_KEYS` kept in step. `.env.example` gets a block per
  door in the communication section, and the `COMMUNICATION_PLUGINS` list
  names them.
- `run.py` boot warnings say "a chat plugin that can push (discord,
  telegram, slack, matrix)" instead of naming two.
- The plugins panel discovers the modules (`_channel_rows`); nothing to
  add. `router.named_surface` recognises "on slack" / "on matrix" once the
  door is registered or configured.
- `README.md`: `### slack — role: chat` and `### matrix — role: chat` under
  Communication plugins (setup steps: Slack app creation with the scopes
  and Socket Mode toggle, where to read your `U…` id; Matrix bot account,
  access token, inviting), the contacts paragraph (id shapes per surface),
  the People tab paragraph, "Where notifications go", the Architecture
  module list. `CLAUDE.md` rule 3's `NAME` list and the loose-ends note on
  translation. `PROJECT_PLAN.md` gets a Phase 10 paragraph.

## 5. Testing

Unit tests only, no network, in the shape of `tests/test_telegram_plugin.py`:
`_api` monkeypatched, a fake websocket for Slack (an async iterator of
frames plus a `send_json` recorder), a fake `_api` returning sync bodies for
Matrix. Per door: incoming parsing (DM only, own/bot messages ignored,
files, stale), the ack-before-handle order (Slack), envelope/reconnect on
`disconnect`, channel send chunking, file send, ack reactions/typing,
notify permanent vs transient, owner mapping both ways, `start()` refusing
without tokens and registering with the router, a transient error not
escaping `start()`, tokens never appearing in an error string. Matrix adds:
initial sync acts on nothing, invites from strangers ignored, encrypted
rooms skipped once, `_dm_room` reuse vs create. Contacts: id shapes,
minted Wren id stable, native types from readers, both doors relaying
`Invalid`. Web: `/api/me` surfaces; People tab covered by `screens.py`'s
seed adding a Slack contact.

Neither door is exercised against a live service by the build; the owner
tries Slack live, and Matrix is flagged as untested live until someone
with a homeserver runs it.

## 6. Rollout

Additive migration (two columns). Restart required. Existing installs see
no change until a door is enabled.
