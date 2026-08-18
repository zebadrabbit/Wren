# Skill Cards and Spaces — Design

## Goal

Skills answer in prose today. `recall_shopping` sends one line; `recall_notes`
with no query sends *one message per note*. The web chat renders each as a
markdown bubble, so reading a list means reading a wall, and acting on it means
typing another sentence and waiting on the LLM.

Give the web chat structured, interactive **cards** for a skill's data, and a
**space** per skill in the sidebar that shows the same card full-pane without a
conversation. Discord and Telegram keep getting exactly the prose they get now.

## What this introduces

- `Channel.send_card(kind, data, text)` — one new capability on the protocol
  every skill already talks to.
- A nullable `card` column on `messages`, holding *what to re-fetch*, not rows.
- `POST /api/dispatch` — run one intent directly, no LLM. Serves card buttons,
  card refresh, and space panes.
- `SPACES` on a skill module, aggregated by `registry`, exposed as
  `GET /api/spaces`.
- Three card renderers in `chat.html`: shopping, notes/ideas, reminders.

## Decisions taken

1. **Chat cards *and* skill spaces**, not one or the other. The sidebar gains a
   SPACES section below CHATS. Same renderer, two mount points.
2. **Cards are always live.** A card stores what to fetch, never the rows. Scroll
   back to an hour-old shopping card and it shows the list as it is now.
3. **First cut is Shopping, Notes & Ideas, and Reminders.** Pins, contacts and
   web results follow the same shape later.
4. **The intent dispatch is the card API.** No new per-skill read contract.

### Why live, and not a snapshot

A snapshot is the more honest transcript — it records what was true, and it
matches what the model saw in history. It was rejected because **interactive
buttons on stale rows are a trap**: a card listing milk from an hour ago, after
milk was bought, offers a ✕ that acts on nothing. Snapshot forces old cards
read-only, which means the thing you scrolled back to is the thing you cannot
use.

Live keeps one behaviour everywhere: every card, at any scroll position, in chat
or in a space, is the current state and is safe to click. The prose in
`messages.content` stays frozen, so the transcript still records what Wren
actually said, and the model's history is untouched.

### Why the intent dispatch, and not a `view()` per skill

Three options were considered:

| | |
|---|---|
| **A** — each skill grows `view(kind, user_id) -> dict` | Explicit, but every skill ends up with a second API returning what its recall intent already returns. Two paths to the same rows, free to drift. |
| **B** — reuse `registry.INTENT_HANDLERS` ✅ | The recall intent *is* the view; the mutation intent *is* the action. No new per-skill read API. |
| **C** — webchat reads the stores directly | Rejected outright: domain logic in a communication plugin, hard rule 2. |

B rests on one fact that was verified before choosing it: **every recall path
needed here is a plain DB read, with no LLM call.**

- `recall_shopping` → `shopping.active_items()`
- `recall_reminders` → `reminders.pending()`
- `recall_ideas` → `notes.search(tags=["idea"])`
- `recall_notes` → `notes.search()`, *provided `ctx.content` is empty*. With a
  non-empty question it calls `brain.recall`, an LLM round trip. The card path
  must always dispatch it with `content=""`. This is the one sharp edge in the
  design; it is load-bearing and must not be "cleaned up" by passing the user's
  question through.

B also means a skill's own authorization keeps working: dispatch goes through
`handle()`, so `contacts_skill`'s owner check (and any future one) still runs
without `/api/dispatch` knowing anything about it.

## Architecture

```
skill.handle()                     ─ ctx.channel.send_card(kind, data, text)
      │
      ├── DiscordChannel.send_card  → self.send(text)        prose, as today
      ├── TelegramChannel.send_card → self.send(text)        prose, as today
      ├── CollectingChannel         → records both           tests assert on either
      └── WebChannel.send_card      → replies + card ref, persisted to messages
                                              │
   chat.html ──────────────────────────────────┘
      │  renders card by kind
      ├── in a chat bubble
      └── in a space pane
                │
                └── POST /api/dispatch {intent, content}
                          → registry.INTENT_HANDLERS[intent].handle(intent, Ctx)
                          → returns the fresh card
```

### The Channel capability

```python
async def send_card(self, kind: str, data: dict, text: str) -> None: ...
```

Added to the `Channel` protocol in `wren/channel.py`. `text` is the string the
skill sends today, verbatim — so writing a card is never a regression for the
surfaces that cannot draw one.

Each implementation is explicit rather than inherited or `getattr`-probed:

- `DiscordChannel.send_card` / `TelegramChannel.send_card` → `await self.send(text)`
- `CollectingChannel.send_card` → appends to a new `self.cards` list *and* to
  `self.sent`, so existing skill tests that assert on `sent` keep passing
  unchanged while new ones can assert on the structure.
- `WebChannel.send_card` → appends the card to the response and persists
  `content=text, card=json` in one `messages` row.

A protocol default was rejected: `Channel` is a `typing.Protocol`, so
implementers are duck-typed and get nothing for free. Two explicit lines in
three classes is less machinery than a mixin, and it makes "this surface cannot
draw cards" a visible decision in each file rather than an absence.

### Storage

`messages` gains one nullable column:

```sql
card TEXT              -- JSON: {"kind": "...", "intent": "...", "params": {...}}
```

`params` is the `Ctx` fields to re-dispatch with, and nothing else — in practice
`content` and `tags`. A shopping card stores `{"content": ""}`; a notes card
filtered to `#house` stores `{"content": "", "tags": ["house"]}`, which is how a
filtered card stays filtered when it re-renders an hour later. `content` is
empty for every card kind, for the `recall_notes` reason given above.

**The rows are not stored.** `card` records only how to fetch current state,
which is what makes live cards live and keeps the column small.

`content` is unchanged and still holds the prose. This is deliberate and is the
reason nothing about the LLM changes: `WebChannel.history()` reads `content`, so
the model sees the same transcript it sees today and knows nothing about cards.

This repo has no migration precedent — every `init_db` is `CREATE TABLE IF NOT
EXISTS`. `conversations.init_db()` establishes the first one: check
`PRAGMA table_info(messages)` for the column, `ALTER TABLE messages ADD COLUMN
card TEXT` when absent. Idempotent, and safe on the live database, which already
holds real conversations.

### `POST /api/dispatch`

```json
→ {"intent": "remove_shopping_item", "content": "milk"}
← {"card": {"kind": "shopping", "data": {...}}, "replies": ["Removed milk."]}
```

Authenticates with the same bearer token as every other endpoint, then:

1. 400 if `intent` is absent or not a string.
2. 404 if the intent is not in `registry.INTENT_HANDLERS`, or its skill is
   disabled — the same `is_enabled` check `core.handle_message` makes, so a
   skill switched off in the plugins panel cannot be driven through this door.
3. Builds `Ctx(user_id, channel=CollectingChannel(), content, tags, person, when)`.
4. Awaits `registry.INTENT_HANDLERS[intent].handle(intent, ctx)`.
5. Returns the collected card and prose.

`CollectingChannel` is reused as-is: it already accumulates instead of
sending, already persists nothing, and already returns `None` from `history()`,
which is correct here — a dispatch is a one-shot with no conversation. A
purpose-named subclass would add a name and no behaviour.

**No new privilege.** Every intent reachable here is already reachable by typing
a sentence; skipping `detect_intent` skips a *classifier*, not a permission
check. The skills' own gates still run.

**Not written to the transcript, on purpose.** Clicking ✕ should not manufacture
a fake user message. The cost: the model does not learn about button-driven
changes conversationally. Since every skill re-reads its store on the next
question, this is only visible if you ask "what did I just remove" immediately
after clicking — accepted.

### Spaces

Each skill declares what it can show:

```python
SPACES = [
    {"key": "shopping", "label": "Shopping", "intent": "recall_shopping"},
]
```

`registry` aggregates them the way it already aggregates `INTENTS` and
`PROMPT_GUIDELINES`; `GET /api/spaces` returns the list, filtered to enabled
skills. Adding a space stays a one-line change in one file plus the existing
`PLUGINS` registration — hard rule 4 holds.

`notes_skill` declares two (`notes` and `ideas`), which is why a space is keyed
independently of the skill module.

The sidebar renders SPACES under CHATS. Clicking one calls `/api/dispatch` with
that space's intent and mounts the same renderer in a full pane. A space is not
a conversation and creates no rows.

### The cards

Each `kind` has one renderer and a fixed data shape.

**`shopping`** — `{"items": [{"text": str, "added_by": str}]}`

| control | dispatches |
|---|---|
| ✕ on a row | `remove_shopping_item`, `content=item text` |
| `+ add item…` | `add_shopping_item`, `content=typed text` |
| clear | `clear_shopping` |

**`notes`** — `{"notes": [{"content": str, "tags": [str], "created_at": str}]}`

Read-mostly. Tag filtering is client-side over the rows already fetched: the
tags are in the payload, so filtering needs no round trip. Note deletion is out
of scope — `notes_store.delete` exists but no *intent* exposes it, and inventing
an endpoint for it would breach the "intent dispatch is the API" decision.

**`ideas`** — same shape as `notes`, plus `discard_idea` and `expand_idea` per
row. `expand_idea` calls the LLM and is the one card action that is slow; it
must show a pending state.

**`reminders`** — `{"reminders": [{"content": str, "fire_at": str, "local": str}]}`

Cancel dispatches `cancel_reminder` with a phrase identifying the row. Times are
formatted server-side by the skill's existing `_format_local`, so the card does
no timezone maths.

## Failure modes

| | |
|---|---|
| Card fetch fails on render | Fall back to the stored `content` prose. A card is an enhancement of a message that already says something useful. |
| Dispatch fails mid-action | Re-render the card from the server's response and surface the skill's own error text. Never leave the row in a half-toggled state. |
| Intent disabled between render and click | 404 from `/api/dispatch`; the card reports it and stops offering the control. |
| Old message, `card` column null | Renders exactly as today. Every existing conversation keeps working untouched. |
| `expand_idea` slow | Pending state on that row only; the rest of the card stays live. |
| Two tabs open | Both are live views; each re-fetches after its own dispatch. Stale-but-clickable is impossible because the click returns fresh state. |

## Testing

- Skill tests keep their `CollectingChannel` shape. It grows `cards`, so a test
  asserts on the structure and the prose fallback in the same call.
- `send_card` on Discord and Telegram: asserts the surface sends `text` and
  nothing structural — the degradation is the contract, so it gets a test.
- `/api/dispatch`: unauthenticated 401, unknown intent 404, disabled skill 404,
  missing intent 400, and one happy path per card kind.
- The `messages.card` migration: a database created before the column, opened
  after, gains it and keeps its rows.
- `recall_notes` dispatched with empty content makes no LLM call — a regression
  test for the sharp edge called out above.
- Card rendering follows the existing substring-on-source approach in
  `tests/test_chat_renderer.py`.

## Staging

Three slices, each independently shippable and independently useful:

1. **Infrastructure + the shopping card.** `send_card` across all four channels,
   the `messages.card` migration, `/api/dispatch`, one renderer. This is the
   slice that proves the architecture, and it is the one with every hard part in
   it — live re-render, inline mutation, degradation.
2. **Notes, ideas and reminders cards.** Pure repetition of slice 1's shape if
   slice 1 was right; if it was not, this is where that shows.
3. **Spaces.** `SPACES`, `GET /api/spaces`, the sidebar section, the full-pane
   mount. Deliberately last: it is a second mount point for a renderer that must
   already work.

## Explicitly not doing

- Pins, contacts, web-search results as cards. Same shape, later.
- Note deletion (no intent exists for it).
- Drag-to-reorder, or any card that writes an order.
- Optimistic UI. Dispatch is a local DB write; add it only if measurement says
  the round trip is visible.
- Cards on Discord or Telegram in any richer form than prose. Telegram inline
  keyboards would mean a per-surface interaction model and a callback route;
  the prose fallback is the whole point of `send_card`'s signature.
- Any change to what the LLM sees. `messages.content` and `history()` are
  untouched by this design, deliberately.
