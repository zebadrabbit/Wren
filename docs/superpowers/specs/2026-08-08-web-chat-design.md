# Web Chat Surface — Design

## Goal

Wren can be reached by Discord DM, by `curl`, and by holding a hotkey and
talking. There is no way to just *open a page and chat with it* — the thing
you do with claude.ai. This adds that: a browser chat UI with a sidebar of
conversations, served by Wren itself.

## What this reverses

The transport split deliberately decided that **non-Discord surfaces keep no
conversation history**: `CollectingChannel.history()` returns `None` and voice
is one-shot. A chat UI is multi-turn by definition, so this feature gives Wren
its own conversation storage for the first time.

That leaves two history mechanisms in the codebase, on purpose:

- **Discord** reads history back off the channel — Discord already stores it.
- **Web chat** reads it from Wren's own `messages` table.

Both satisfy the same `Channel.history()` contract, so `core` is unaffected.
Voice and the machine API still return `None` and stay one-shot.

## Decisions taken

| Question | Decision | Why |
|---|---|---|
| Scope | Multiple conversations + sidebar | The real claude.ai shape, as asked for. |
| Auth | Paste a `WREN_TOKENS` value once, kept in `localStorage` | Reuses the auth already built. No sessions, no password hashing, no new table. |
| Surface | Extend `http`, don't add a `web` one | Same protocol, same tokens, same port. Two aiohttp servers on one box for one token is waste. |
| Titles | First user message, truncated | An LLM call per new conversation is a round-trip to name a thing you can rename. |
| Streaming | Not in v1 | `brain._complete` returns a finished string, and intent detection is a mandatory non-streamed step first. |
| Assets | One self-contained HTML file | A self-hosted LAN box must work with the internet unplugged. No CDN, no build step. |

## Architecture

```
wren/
  conversations.py        # NEW — conversations + messages storage
  surfaces/
    webchat.py            # NEW — chat routes + WebChannel
    chat.html             # NEW — the page (inline CSS/JS, no build)
    http.py               # mounts webchat's routes
  core.py                 # brain calls wrapped in asyncio.to_thread
```

### Storage

```sql
CREATE TABLE conversations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   TEXT NOT NULL,
    title      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    role            TEXT NOT NULL,      -- "user" | "assistant"
    content         TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
```

Indexes on `messages(conversation_id, id)` and
`conversations(owner_id, updated_at DESC)`.

Deleting a conversation deletes its messages in the same transaction.
(SQLite enforces `ON DELETE CASCADE` only with `PRAGMA foreign_keys=ON`, which
is off by default per-connection — so the delete is explicit rather than
relying on a pragma someone will forget.)

### Ownership is the security boundary

`WREN_TOKENS` can map several tokens to several users, and contacts get
whitelisted at runtime, so multi-user is real rather than theoretical.

Every conversation-scoped call verifies the conversation's `owner_id` matches
the authenticated user, and returns **404, not 403**, on mismatch — a 403
would confirm that someone else's conversation exists at that id.

Storage functions take `owner_id` as a parameter and filter on it in SQL. They
do not accept a "trust me, already checked" flag.

### Endpoints

All under the existing `Authorization: Bearer <token>` check.

| Route | Body | Returns |
|---|---|---|
| `GET /` | — | the chat page (HTML, no auth — it is a login box until you paste a token) |
| `GET /api/conversations` | — | `[{id, title, updated_at}]`, newest first |
| `POST /api/conversations` | — | `{id, title}` |
| `GET /api/conversations/{id}` | — | `{id, title, messages: [{role, content, created_at}]}` |
| `PATCH /api/conversations/{id}` | `{title}` | `{ok: true}` |
| `DELETE /api/conversations/{id}` | — | `{ok: true}` |
| `POST /api/conversations/{id}/message` | `{text}` | `{replies: [...], files: [...]}` |

`GET /` is unauthenticated because it is a static page containing no user
data — it renders a "paste your token" box and every subsequent request from
it carries the header. Serving the shell without auth avoids inventing a
second authentication path (cookies) purely to fetch some HTML.

### The turn, and the ordering subtlety

`core.handle_message` calls `channel.history()` *before* dispatch, and the
current message is passed separately as `text`. If the surface persists the
user message first — which it should, so an LLM failure does not lose what you
typed — then a naive `history()` would return that message too and the model
would see it twice.

So the sequence is:

1. Insert the user message, keep its `id`.
2. Build `WebChannel(conversation_id, owner_id, before_id=<that id>)`.
3. `history()` returns messages with `id < before_id`, capped at `limit`,
   oldest-first (the shape `brain.detect_intent` expects).
4. `core.handle_message(user_id, text, channel)`.
5. Each `channel.send(...)` appends an `assistant` row *and* collects the text
   for the JSON response.
6. Bump `conversations.updated_at`.

`send_file` collects only — files are returned base64 in the response and not
stored, matching what the machine API already does.

`ack()` is a no-op: the page shows its own thinking indicator.

### Blocking LLM calls

`core.handle_message` currently calls `brain.detect_intent` and `brain.chat`
directly. Both are synchronous `httpx` calls into the LLM that take seconds,
and they run on the event loop shared by every surface and every plugin
poller. This is pre-existing — `bot.py` did it too — and invisible with a
single Discord user.

A chat UI makes it visible: two browser tabs serialize behind each other, and
the Discord gateway heartbeat stalls for the duration of every LLM call. Both
calls get `await asyncio.to_thread(...)`, matching the convention `web_plugin`
already uses at `web_plugin.py:63` and the fix just applied to `/voice`.

### The page

One file, inline CSS and JS. Sidebar (new / switch / rename / delete),
message list, composer. Enter sends, Shift+Enter newlines. Theme follows
`prefers-color-scheme`.

**Markdown rendering is a security requirement, not polish.** Replies can
contain text Wren did not author: `web_plugin` summarizes scraped pages, and
`send_to_person` relays text from other users. The renderer therefore escapes
HTML *first*, then applies a fixed whitelist to the already-escaped string —
fenced code, inline code, bold, italic, links, unordered lists, line breaks.
It can never emit a tag the escaper did not neutralize. Link `href`s are
restricted to `http:`/`https:` so `javascript:` cannot slip through.

File responses render as a download link built from the returned base64.

## Testing

- `tests/test_conversations.py` — create/list/rename/delete, message append and
  ordering, cascade delete, and **owner isolation**: user B cannot read, rename
  or delete user A's conversation, and `list` never shows it.
- `tests/test_webchat.py` — 401 without a token; 404 (not 403) on another
  user's conversation id; `history()` excludes the in-flight message; replies
  are persisted as `assistant` rows; `updated_at` moves; `GET /` serves the
  page.
- `tests/test_core.py` — extended to prove the `brain` calls are dispatched
  through a thread rather than inline.

## Explicitly not doing

- Streaming responses (the main thing that will feel different from claude.ai)
- File uploads
- Search across conversations
- Sharing or export of a conversation
- A mobile-specific layout
- Editing or regenerating past messages
- Any change to Discord's history mechanism
