# One Timeline — Design

**Date:** 2026-10-13
**Status:** draft, awaiting owner review
**Supersedes:** the conversation-list half of `2026-08-08-web-chat-design.md`; the
"spaces" half of `2026-08-17-skill-cards-and-spaces-design.md` is carried in here.
**Assessment it answers:** https://claude.ai/artifact/5HjLPKTj4BoUkBsVUkDU96

## The problem

Wren is a different product depending on the door. The web chat has threads,
cards and a settings panel but never sees a reminder fire; Telegram keeps no
history; Discord reads its own; voice is one-shot; nothing Wren says first
(a reminder, the briefing, a watcher alert) is written anywhere. A reminder set
in the web chat lands on Telegram and leaves no trace where it was asked for.
The phone layout is unusable, and the landing screen is a greeting and five
pills on a product whose stated goal is to speak first.

## Decisions taken with the owner (2026-10-13)

1. **One thread per person.** The list of separate conversations goes. Every
   door writes to the same timeline; day separators replace titles. Existing
   conversations are not deleted: they are read into the timeline in time
   order, untouched.
2. **All six steps in one spec**, executed in order, each independently
   shippable: transcript, web page, briefing landing, responsive, spaces,
   people.

## 1. The transcript

### Storage

`messages` gains two nullable columns, added by the same additive migration
pattern `card` used (PRAGMA guard, swallow only "duplicate column name"):

| column | values | meaning |
|---|---|---|
| `channel` | `web`, `voice`, `discord`, `telegram`, NULL | where this turn happened. NULL on rows written before today, which the page treats as `web`. |
| `kind` | `notice`, NULL | `notice` means Wren spoke first: a reminder, the briefing, a watcher alert. NULL is an ordinary turn. |

No row is rewritten. The `conversations` table stays as the owner-keyed
container it already is. New turns go to one conversation per person,
`conversations.home(owner_id)`, get-or-create with title `timeline`. The
timeline read is a join:

```sql
SELECT m.id, m.role, m.content, m.created_at, m.channel, m.kind, m.card
FROM messages m JOIN conversations c ON c.id = m.conversation_id
WHERE c.owner_id = ?  [AND m.id < ?before | AND m.id > ?after]
ORDER BY m.created_at DESC, m.id DESC LIMIT ?
```

Ordering is by `created_at` (ISO 8601 UTC with microseconds, so text order is
time order) with `id` as the tiebreak. Old conversations interleave correctly
without being touched. Pagination cursors are message ids: `before` for older
pages, `after` for the poll.

`conversations.py` keeps `init_db`, `add_message`, a new `home`, a new
`timeline(owner_id, *, before=None, after=None, limit=50)` and a new
`recent(owner_id, *, before_id, limit)` that returns the
`[{"role", "content"}]` shape `brain.detect_intent` reads. `create`, `list_for`,
`get`, `rename`, `delete`, `touch`, `title_from` and `messages` are deleted
with their tests; nothing else calls them once the routes below are gone.

### Who writes

**Core, not the plugins.** `core.handle_message` already holds the one
authorization gate "so a new surface cannot forget it"; recording the
transcript is the same kind of invariant and lives in the same place.

- Each `Channel` implementation declares `NAME` (`"web"`, `"discord"`,
  `"telegram"`). `CollectingChannel` takes `name="web"` in its constructor.
  The http plugin passes `"voice"` for `/voice` and `"web"` for `/message`.
- `handle_message` records the user turn right after the authorization gate
  and the empty-message check, before `ack("seen")`:
  `conversations.add_message(home, "user", stored, channel=channel.NAME)` where
  `stored` is the text, or `(sent photo.jpg, receipt.pdf)` for a bare file,
  exactly as `webchat.post_message` does today. The returned id is the
  `before_id` for history, so the model never sees the turn twice. Persisting
  first means a failed LLM call still keeps what was typed.
- The channel is wrapped: `channel = _Recording(channel, home)` forwards
  `send`, `send_file`, `send_card`, `ack`, and records each `send` and
  `send_card` as an assistant row (`send_card` stores the `{kind, intent,
  params}` ref in `card`, as `WebChannel` does now). It exposes
  `.written: list[int]`, the ids of every row it wrote, and `.inner`.
  Skills still see a `Channel`; nothing under `wren/skills/` changes.
- History comes from the store, not the channel:
  `conversations.recent(user_id, before_id=turn_id, limit=10)`. The
  `history()` method leaves the `Channel` protocol, and with it
  `DiscordChannel.history`, `discord_utils.history_to_messages`,
  `TelegramChannel.history`, `WebChannel.history`, and
  `CollectingChannel.history`. Discord stops reading its own backlog: the
  timeline is the backlog, and it includes what was said on Telegram.
- Notices: `router.notify` and `router._notify_any` record
  `add_message(home, "assistant", text, channel=<surface that delivered>,
  kind="notice")` after a surface returns True, and only then. A False or a
  raise records nothing, so the timeline never claims a delivery that did
  not happen. The one writer for notices is the router, so a surface's
  `notify` never records.
- The http plugin's `notify` becomes a passive delivery: it returns True
  and sends nothing, because the router's notice row is what the web page
  shows on its next poll. `CAN_NOTIFY` stays False so `_notify_any` never
  picks it while an active surface exists; `_notify_any` tries every
  `CAN_NOTIFY` surface and, if none delivered and none raised, falls back to
  a registered `http` and returns its True. Effect: on an http-only install
  reminders now appear in the web chat instead of being discarded, and
  `NOTIFY_VIA=http` becomes legal. `run._warn_if_notifications_go_nowhere`
  changes its wording for that case to "will only appear in the web chat".
- `db.dry_run()` already discards every write, so `?dry_run=1` records
  nothing. `router.notify` under dry run already returns True before any
  surface is tried; it must also return before recording.

The LLM's ten-turn history now includes notices (as assistant turns). That
is wanted: "what was that reminder about" is answerable.

### What this gives for free

Telegram has history. Voice has history. "What did I just say" resolves on
every door. The Discord plugin loses code. The README's "only Discord and the
web chat have history" paragraph is deleted.

## 2. The web page

### Routes

`webchat.register_routes` keeps `GET /`, `POST /api/dispatch`, `GET /api/me`,
`GET /api/plugins`, `PATCH /api/plugins/{module}`, `PATCH /api/settings`, and
adds:

| route | returns |
|---|---|
| `GET /api/timeline?limit=50` | newest 50 rows, oldest first |
| `GET /api/timeline?before=<id>&limit=50` | the page before that id |
| `GET /api/timeline?after=<id>` | rows newer than that id, for the poll (capped at 200) |

Rows: `{id, role, content, created_at, channel, kind, card}`. `/api/conversations*`
and `/api/conversations/{id}/message` are deleted. The page sends messages to
the existing `POST /message` (JSON or multipart, already shared with the voice
client); `http_plugin._payload` gains `"messages": [rows written this turn]`
read back by id from `_Recording.written`, so the page draws exactly what was
stored and the poll cursor advances to the last of them. `replies`, `files`
and `cards` stay as they are for the machine API.

All three timeline forms require the bearer token and the whitelist check
`_auth` already does. A contact sees only their own timeline, as before.

### Layout

Same tokens, same single self-contained file.

- **Sidebar.** Brand header, then `Today` (the timeline, selected by
  default), then a `Spaces` section (step 5), then the footer with the
  person's name, the gear and sign out. `+ New chat`, the conversation list,
  rename and delete, and the native `prompt`/`confirm` dialogs go.
- **Top bar.** `Today` and the local date. The model name moves to the
  header of the Plugins panel (operator information).
- **Log.** The timeline, oldest at the top, with a day separator wherever the
  local date of `created_at` changes. A user turn whose `channel` is not
  `web` carries a small glyph and label under the bubble ("from Telegram",
  "by voice"). A `notice` row renders as a quiet dashed row with a bell and
  the surface it went to ("sent to Telegram"). Scrolling to the top loads
  the previous page with `before`.
- **Poll.** Every 30 seconds while the tab is visible, `after=<last id>`;
  new rows are appended and scrolled into view if the user was at the
  bottom. On `visibilitychange` to visible, poll immediately.
- **Composer.** Unchanged, full width of the column; a row of chips sits
  above it at all times, one per enabled skill that has a read intent
  (`PILL_READS` grows to every skill with a card: Shopping, Notes, Ideas,
  Reminders, plus Catch me up for the briefing). A chip dispatches the read
  and draws its card as an unstored assistant bubble, as the Shopping pill
  does today. Starter-text pills go: five identical pills that behave two
  ways was the finding.
- **Cards.** Shopping rows get a checkbox: ticking is the existing remove
  with its 8-second undo, drawn as a tick and strike-through instead of a
  cross. Reminder rows tick as done (cancel). Ideas keep the cross; ideas are
  discarded, not done. The stored-card re-mount on page load is unchanged.
- **Font stack.** `system-ui` first, then the rest of the existing stack.
- **Settings panel.** The nine `.env`-locked rows use muted ink and a lock
  glyph rather than the warn colour. No other panel change.
- **Sign-in copy.** "Paste the sign-in token you were given." The host
  command moves into the Plugins panel's People tab (step 6).

## 3. The landing is the briefing

`briefing_skill.handle` sends a card: `send_card("briefing", data, text,
intent="briefing")` where `text` is the prose `compose` already builds (so
Discord and Telegram are unchanged) and `data` is the same information
structured:

```json
{"greeting": "Good morning. Tue Oct 13.",
 "weather": "54° clearing by noon" | null,
 "calendar": ["14:30  Dentist"] | null,
 "reminders": [{"local": "18:00", "content": "Take out the trash"}],
 "email": [{"sender": "...", "subject": "..."}],
 "shopping": 5 | null}
```

`compose` is split into `gather(user_id) -> dict` and `compose(user_id) ->
str` that renders the dict; the morning push keeps calling `compose`. `null`
means the section is off or unconfigured; an empty list means configured and
empty. The page draws the sections it is given as a card group: weather,
calendar, reminders, shopping (with its add row). The group is mounted in a
**Today panel** pinned above the log, dispatched on page open and on each
visible poll tick (zero model calls; the weather and calendar fetches are
already cached). Nothing is written to the timeline for it. The morning push
still records its prose as a notice, so the timeline shows that Wren
spoke at 7:00.

## 4. Responsive

Under 700px the sidebar is an off-canvas drawer, closed by default, opened by
a button in the top bar and closed by tapping the log, choosing an entry, or
the button again. This reuses the existing `railed` mechanism and drops the
hover `peek` strip on touch. The chip row scrolls horizontally. The composer
and cards take the full width; the card group stacks to one column. The
existing `640px` rule goes. Verified at 390px in both themes before merge.

## 5. Spaces

A skill may declare

```python
SPACES = [{"key": "shopping", "label": "Shopping", "intent": "recall_shopping", "params": {}}]
```

`registry.spaces()` aggregates them from enabled skills; `GET /api/spaces`
returns the list. Shopping's declaration is dynamic: one entry per list name
in `shopping_store` (new `list_names()`), so a packing list shows up the day
it is created, which also answers "what lists do I have". Notes declares
`notes` and `ideas`; Reminders declares `reminders`.

The sidebar renders them under `Spaces`. Choosing one replaces the log with
that card drawn full-pane by the same renderer, with a `Today` entry above to
return. No counts in the sidebar in this slice.

## 6. People

A `People` tab in the owner-only panel over `contacts.py`, which is already
runtime-mutable state (CLAUDE.md rule 7):

| route | body | effect |
|---|---|---|
| `GET /api/contacts` | | `[{alias, discord_id, telegram_id}]` |
| `POST /api/contacts` | `{alias, surface, id}` | `contacts.add` or `contacts.set_id` |
| `DELETE /api/contacts/{alias}` | | `contacts.remove` |

Owner only, same `_owner` guard as the rest of the panel. The tab lists each
person with their per-surface ids, an add form, and a remove control with an
inline confirm (no native dialog).

**Web sign-in tokens stay on the host.** They are credentials, so by rule 7
they are never runtime-mutable and never returned by an endpoint. The tab
shows the command (`./manage.sh token add <id>`) and explains the invite
link: the page accepts `/#token=<token>`, stores it as if pasted, and strips
it from the URL with `history.replaceState` before any request. It is a fragment rather than a query string because a fragment is never sent to the server, so the bearer token stays out of the aiohttp access log. The operator
sends a family member one link instead of a token and instructions.

## Out of scope

Push notifications and the installable app (parked on HTTPS on the board).
Streaming replies. Telegram inline keyboards. Calendar write. Multi-intent
turns. Editing the Discord plugin for inbound files.

## Testing

Tests stay in the `CollectingChannel` + `Ctx` shape; no Discord mocked.

- `test_conversations.py`: migration adds both columns to a pre-existing
  table; `home` is idempotent; `timeline` interleaves two old conversations
  with new home rows in time order; `before`/`after` cursors; `recent` shape
  and `before_id` exclusion.
- `test_core.py`: a turn records user then assistant rows with the channel
  name; a bare photo records the placeholder; a failed LLM call leaves the
  user row; `send_card` stores the ref; history passed to `detect_intent`
  excludes the in-flight turn and includes a Telegram turn.
- `test_router.py`: a True delivery records a notice with the delivering
  surface; False and raise record nothing; `_notify_any` falls back to
  passive http only when nothing active delivered; dry run records nothing.
- `test_http_surface.py` / `test_webchat.py`: timeline paging, poll cap,
  cross-user isolation (404 shape unchanged), `messages` in the `/message`
  payload, `#token=` is not a server concern (page only). The conversation
  route tests are deleted.
- `test_briefing_skill.py`: `gather` shape; `compose(gather(...))` equals
  today's prose for the same inputs; the card's `text` is that prose.
- `test_registry_enabled.py`: `spaces()` honours the enable flag; the
  shopping entries follow `list_names()`.
- `test_contacts_plugin.py`: the three routes, owner-only.
- `test_discord_surface.py` / `test_telegram_plugin.py`: history tests go;
  `NAME` is declared.
- The chat page: `test_chat_renderer.py` already drives the page's render
  functions; it gains day separators, channel labels, notice rows, the
  briefing group, and the 390px drawer. One Playwright screenshot pass at
  1280 and 390 in both themes before merge, on a scratch DB and port, with
  the scratch process stopped by its PID.

## Rollout

The service runs this tree in place. Each step merges to `main` green and
restarts the service once. Step 1 changes the storage and every door; it
ships with step 2 in the same restart, because step 1 alone deletes the
routes the current page calls. Steps 3 to 6 restart independently. Before
the first restart, back up `wren.db` as the existing `wren.db.bak-*` files
were.
