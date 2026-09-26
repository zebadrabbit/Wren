# Wren — Project Plan (v2 restart)

## Why restart

Wren had drifted: it grew Discord-specific assumptions early, then had a
"surfaces vs capability plugins" split retrofitted on top later. That split
was the right instinct but the wrong shape for where the project is actually
headed — a chat assistant, reachable through *any* messaging app, extensible
with drop-in **skills** (shopping list, notes, reminders, ...) and drop-in
**communication channels** (Discord, Telegram, Gmail, ...), where a skill
never has to know which channel it's being used from.

This restart doesn't throw the code away. Wren's engine — LLM intent
detection, a transport-free `Channel`/`Ctx` abstraction, SQLite storage,
provider fallback — was already sound and already close to this shape. What
changed is the taxonomy on top of it, made explicit instead of implicit, and
named so a new contributor (or you, in six months) can tell at a glance what
kind of plugin they're looking at.

## The new direction

Wren is a **local-first chat assistant** with a **built-in web chat window**
(same idea as this conversation) and two plugin classes:

### 1. Communication plugins — how you reach Wren, how Wren reaches you

Live in `wren/communication/`. Each one declares a `ROLE`:

- **`chat`** — a full two-way conversational channel: receives messages,
  sends replies, optionally keeps history. Examples: **Discord**, the
  built-in **HTTP/web chat**, and **Telegram** (built; needs a bot token).
- **`input`** — a one-way event source: watches something external and
  raises an event, but has no "send" of its own. It hands off to a `chat`
  plugin via `router.notify_name(...)`. Examples: **Gmail** (IMAP watcher),
  **GitHub** (repo activity watcher).

A future **`output`** role (deliver-only, e.g. a push-notification service
with no inbound message path) is anticipated by the same `ROLE` field but
nothing uses it yet — added if/when a plugin needs it.

### 2. Skills — what Wren can actually do

Live in `wren/skills/`. A skill declares `INTENTS` (which user intents it
owns), `PROMPT_GUIDELINES` (how the LLM should recognize them), and an async
`handle(intent, ctx)`. It never imports `discord` or `aiohttp` — it only
ever sees `ctx.channel.send(...)`. That's what makes "add potatoes to
shopping" work identically whether it arrived by Discord DM, the web chat
window, voice, or Telegram.

Shipped skills, carried over from the old capability plugins: **Shopping
List**, **Notes** (+ Ideas), **Reminders**, **Pins**, **Contacts**, **Web**
(search/fetch). Each is unchanged in behavior — only where it lives and what
it's called changed.

### How they compose

```
you ──▶ communication plugin (chat) ──▶ core.handle_message ──▶ skill
         Discord / HTTP-webchat          (whitelist, intent)     (writes back via ctx.channel)

external event ──▶ communication plugin (input) ──▶ router.notify_name ──▶ communication plugin (chat)
   new email,          gmail_plugin.py /                                      discord_plugin.py, ...
   repo push            github_plugin.py
```

This is exactly the pattern you described: Discord/Telegram under
"Communication" for chatting, Gmail under "Communication" as an input-only
notifier, and Shopping List (and friends) as skills usable from the chat
window or any comms plugin.

## What's already done (this session)

The existing Wren codebase at `W:\work\Wren` has been restructured in place
to this taxonomy — reusing, not rewriting, the working code:

- `wren/surfaces/` → `wren/communication/`, with `discord.py` →
  `discord_plugin.py`, `http.py` → `http_plugin.py`; `webchat.py` and
  `discord_utils.py` moved alongside.
- `wren/email_plugin.py` → `wren/communication/gmail_plugin.py`,
  `wren/github_plugin.py` (+ `github_state.py`) → `wren/communication/` —
  reclassified from "capability plugin" to Communication (`ROLE = "input"`),
  since they're event sources, not chat skills.
- `wren/*_plugin.py` (shopping, notes, reminders, pins, contacts, web) →
  `wren/skills/*_skill.py`, with their storage modules alongside as
  `*_store.py` (e.g. `shopping.py` → `wren/skills/shopping_store.py`).
- `wren/plugins.py` → `wren/registry.py` — now only registers Skills;
  Communication plugins are started directly by `run.py` from
  `COMMUNICATION_PLUGINS`.
- New `wren/communication/telegram_plugin.py` — a complete second chat
  plugin, written to prove the Communication/Skill split (see below).
- Env vars renamed for clarity: `SURFACES` → `COMMUNICATION_PLUGINS`,
  `NOTIFY_SURFACE` → `NOTIFY_VIA`. `.env.example` and `README.md` updated.
- All 33 test files' imports updated to the new module paths; every import
  across `wren/` and `tests/` was statically verified to resolve (a custom
  checker walked every `import`/`from` statement and confirmed the target
  file exists in the new layout). `python3 -m py_compile` passes clean on
  every source and test file.
- Git: your prior local history (8 commits ahead of `origin/main`, remote
  `git@github.com:zebadrabbit/Wren.git`) was archived to
  `.git-wren-v1-archive/` (gitignored, not deleted — nothing was lost) and a
  fresh repo was initialized locally with one clean commit. GitHub itself
  was intentionally left untouched, per your call to keep this local-only
  for now.

**Not done at the time of the restart** (all three since resolved — see the
status section below):

- `pytest` could not actually be run in this session (the sandboxed shell
  used to edit your files has no network access and the project's `venv`
  targets a different Python installation) — imports were verified
  statically instead. **Run `pytest -q` yourself before trusting this** is
  the single most important next step.
- `wren/surfaces/` still contains one stale, empty `__pycache__/` directory
  that couldn't be removed from this session (sandbox can't delete files on
  your machine) — harmless, gitignored, safe to delete by hand.
- Telegram is a stub only — no live `start()`/`notify()`/`TelegramChannel`.

## Status as of 2026-08-09

Everything above this line is the record of the restart session itself and is
left as written. What has happened since:

- **Restructure verified.** The suite runs and is green (557 passed). It did
  not start that way — the static import check missed 46 breakages, all of
  them stale references living in strings rather than import statements (path
  literals, `mock.patch("wren.old_module...")` targets, renamed config attrs,
  and tests asserting the old plugin taxonomy). If you rename a module here,
  grep for the old name as a bare string too.
- **Live run smoke-tested.** `COMMUNICATION_PLUGINS=http` on a temp DB and
  port: boots clean, webchat serves, and "add potatoes to shopping" round
  trips through intent detection into the shopping skill and back.
- **`.env` was still on the v1 keys** (`SURFACES`, `NOTIFY_SURFACE`). Renamed.
  This would not have errored — `config.py` defaults `COMMUNICATION_PLUGINS`
  to `discord`, so the next restart would have silently come up without the
  web chat.
- **Telegram is built**, not a stub: Bot API over `aiohttp` long-polling, no
  new dependency. Deliberately **not wired in** — no bot token yet, so
  `TELEGRAM_TOKEN` is unset and `telegram` is absent from
  `COMMUNICATION_PLUGINS`. Enabling it is those two `.env` lines plus
  whitelisting the Telegram user id as a contact.
- **The architecture claim held.** Adding a whole new communication channel
  touched zero files under `wren/skills/`. That was the point of the restart,
  and it is now demonstrated rather than asserted.
- `wren/surfaces/` is gone (it held 5 orphaned `.pyc`, not an empty dir).

Known rough edges in the new Telegram plugin, none blocking, all recorded on
the Trello board: `notify()` treats every HTTP 400 as permanent (so an
over-4096-char message is dropped rather than split); a non-`handle_message`
exception mid-batch discards that batch's offset progress; and a cold start
drains the whole unconfirmed backlog, answering each with an inline LLM call.

## Focused next steps

1. ~~Verify the restructure~~ — done, 557 passed.
2. ~~Smoke-test a run~~ — done.
3. ~~Build the Telegram communication plugin~~ — done; needs a bot token to
   actually enable.
4. **Decide the GitHub story**: push this restructured repo to the existing
   `zebadrabbit/Wren` remote (as a new default branch, or a fresh repo if
   you'd rather cut ties with the old history entirely) once you're happy
   with the result locally.
5. **Pick the next skill or channel** to build toward your actual use case —
   the architecture now makes either a same-shaped, additive change instead
   of a special case.

## Design principles to hold onto

- A skill never imports a transport library. If it needs to, it's not a
  skill — either the transport needs a `Channel` method added, or the logic
  belongs in a communication plugin.
- A communication plugin never contains domain logic (no "if item on
  shopping list" inside `discord_plugin.py`). It authenticates, builds a
  `Channel`, and calls `core.handle_message`.
- `ROLE` is the only thing that distinguishes communication plugins from
  each other structurally — `"chat"` needs `start()` + a `Channel`; `"input"`
  needs `start()` and calls `router.notify_name(...)`; nothing else is
  special-cased.
- New capability → new file in `wren/skills/`, one line in
  `registry.py`'s `PLUGINS` list. New channel → new file in
  `wren/communication/`, one entry in `COMMUNICATION_PLUGINS`. Neither
  should ever require touching the other.

## Backlog after the skill-cards work (2026-08-17)

Slices 1 and 2 are merged and live: the browser chat answers shopping, notes,
ideas and reminders with interactive cards, backed by `Channel.send_card`, a
`messages.card` column, and `POST /api/dispatch` (one intent, no LLM). Discord
and Telegram get prose, each plugin chunking at its own platform's limit.

Ordered roughly by value, not by size.

**Inbound images and files.** Landed 2026-09-26 (spec:
docs/superpowers/specs/2026-09-26-inbound-images-design.md; Telegram and web
chat, images and PDFs, no vision). Discord and vision remain open. History: a
screenshot or a photo could not enter through any door. `telegram_plugin._incoming`
skipped every message without a `text` field, which is exactly how photos,
stickers and voice notes arrive; Discord attachments were ignored the same way.
Needed blob storage, an inbound-file capability on the `Channel` protocol (the
mirror of the `send_file` that already exists for exports), and per-surface
handling. A real feature, not an afternoon — and it unlocked a use that existed
rather than improving one that already worked.

**Summarise large collections in prose.** Landed 2026-09-26: past
`notes_skill.SUMMARY_AFTER` (15) rows a text surface gets the tag breakdown,
or the newest 15 and a count when there is only one tag to narrow by; the
card still gets every row. History: already specified in
`docs/superpowers/specs/2026-08-17-skill-cards-and-spaces-design.md`: past ~15
items, text surfaces get a tag breakdown and an invitation to narrow, while the
card keeps showing everything (it is scrollable and already filters by tag).
Chunking made a 40-note dump deliverable; this makes it readable.

**Slice 3: spaces.** The sidebar SPACES section from the design — open a skill's
card full-pane without a conversation. All the machinery exists; this is a
second mount point for renderers that already work.

**First-run path.** Now that this is meant to be self-hostable by other people,
bugs that only bite fresh installs matter most, and they are invisible from a
machine whose `.env` was hand-edited months ago. `setup.sh` was writing the v1
key `SURFACES` until 1f32b0d — that class, plus a check that Wren tells you when
it is pointed at a model too small to classify intent reliably.

**Parked findings from the slice reviews.**
- `discord_utils.py` and `discord_plugin.py` each define `_MAX_MESSAGE_CHARS =
  2000`. Unforced: defining it in `discord_utils` and referencing it from
  `discord_plugin` is non-circular. Two hand-synced literals, no test imports
  either, so drift fails silently.
- `notes_store.find` / `reminders_store.find_pending` apply exact-match-wins to
  every caller, including the free-text path where `PROMPT_GUIDELINES` asks the
  model for SHORT phrases. `discard_idea` is an irreversible DELETE where
  `cancel_reminder` is a soft flip, so the destructive caller is the one that
  lost its "be more specific" prompt. A narrower fix exists: exact-first only
  when the phrase came from a card button.
- One recall row now carries a whole collection and rides in
  `brain.detect_intent`'s 20-turn history window at full size.
- `chat.html` renders cards *instead of* replies, so a turn producing a card
  plus unrelated prose would drop the prose. Not reachable today; the trap
  widens with every new card.
- No `/api/dispatch` test uses a real skill — every one patches
  `INTENT_HANDLERS` with a mock, so skill → dispatch → renderer is two
  half-tests that meet.
- `notify_id` can half-deliver a chunked Discord reminder on a mid-send
  failure. Matches the tradeoff `telegram.notify()` documents in a `ponytail:`
  comment; Discord's has no equivalent note at the point of risk.

**Keep-style notes grid.** Cosmetic, an afternoon: tiles instead of a list.
Worth doing because it reads better daily, not because it wins a comparison
against the dozen mature self-hosted notes apps.

## Model: Gemma 4 E4B (2026-09-26)

qwen2.5:7b was chosen because it was quick and easy; the purpose grew. A
thirty-phrase household intent probe (scratch script, offline, live service
untouched) scored qwen 29/30 at 0.7 s median and Gemma 4 E4B 27/30 at 1.5 s
through the OpenAI-compatible endpoint — every Gemma miss a JSON cut off by
its own reasoning, which Ollama 0.34 cannot switch off on that endpoint.
Through Ollama's native `/api/chat` with `think: false`, Gemma scored 30/30 at
0.8 s, with cleaner extractions and the right Tuesday where qwen picked a past
date. So `providers.ollama_chat` speaks the native endpoint (thinking off,
`format: json` when the classifier asks), every other provider keeps the
OpenAI-shaped path, and `.env` moved to `gemma4-e4b-32k`. Chat, memory
extraction and recall were spot-checked on Gemma before the switch. The same
transport carries images, which is where vision starts.

## Household gaps — Phase 7 (2026-09-26)

Ten items chosen on 2026-09-26 after asking what a household actually hits,
ranked by how often. The Planka board (project "Wren", list "Phase 7 —
Household Gaps") holds the state; this is the order and the why.

1. ~~**Recurring reminders and routines.**~~ Landed 2026-09-26: "every
   tuesday at 8pm", "every morning", "every 3 days", "every other week",
   daily/weekly/hourly are parsed in the skill (not the classifier, whose
   clock maths cannot be trusted) into a fixed interval in a `repeat` column;
   the model still supplies the first occurrence. A fired row re-arms from
   its scheduled time — day steps on the local wall clock so 8am survives
   DST, hour/minute steps in UTC — and a service that was down for a week
   fires once and skips forward. Cancel stops the series. "Every weekday"
   and "skip next time" are out of scope.
2. ~~**Second person.**~~ Landed 2026-09-26: `contacts` holds one id column
   per surface (`SURFACES`), a contact's Wren id is `COALESCE(discord_id,
   telegram_id)`, "add 555 as hubby on telegram" gives an existing contact a
   second door, the Telegram plugin translates everyone in both directions,
   and `router.notify` with no `via` routes a contact to a surface they are
   on. The old table is rebuilt in place with its rows.
3. ~~**Generic named lists.**~~ Landed 2026-09-26: a `list` column on
   `shopping_items` (default `shopping`), the name parsed in the skill from
   "…my packing list" / "…the hardware store list" (one or two words), the
   six shopping intents unchanged, replies name the list when it is not
   shopping, and the card carries the name in its data and params (a new
   optional `list` field on `Ctx` and `/api/dispatch`) so a reload re-reads
   the same list. Not yet: "what lists do I have", rename, delete.
4. **Web chat as an installable app with push.** Also the answer to where
   device replies land.
5. **Calendar write (CalDAV).**
6. ~~**Vision on inbound images.**~~ Landed 2026-09-26: pictures go to
   Gemma on the native Ollama transport (`brain.describe`, `brain.items_in`).
   A question caption is answered from the picture and not kept; "add these
   to the list" reads the items off a receipt, fridge or handwritten list;
   anything else is a note as before. Skills declare `ACCEPTS_FILES`. Live:
   five items read off a rendered list in 0.6 s. Out of scope: Discord
   attachments, describing a photo unasked.
7. ~~**Cross-skill recall.**~~ Landed 2026-09-26: a question routed to
   recall_notes now gathers from notes, memories, pending reminders and the
   next thirty days of calendar (`wren/recall.py`), ranked by word overlap
   and capped at twenty, and the answer prompt labels each record with its
   source and date. No new intent; the classifier is untouched. Attachments
   come back only for the notes that matched. Ceiling: lexical matching.
8. ~~**Timers.**~~ Landed 2026-09-26: "timer 20 minutes" / "set a 10 minute
   timer" is parsed in the reminder skill (no content needed) and fires "20
   minute timer is up"; any reminder due within the hour reads back as "in N
   min", so "how long is left on the timer" is recall_reminders.
9. ~~**Undo for typed messages.**~~ Landed 2026-09-26: skills declare
   `UNDO = {intent: async fn(ctx)}`; core remembers the last destructive
   intent per person for ten minutes and runs the inverse on "undo that" /
   "put it back" / "revert that", before the classifier. Inverses for
   remove and clear (exactly the rows the clear removed), cancel (one or
   all), discard idea, unpin, forget, remove contact (every surface id).
10. ~~**Gmail digest / sender allowlist.**~~ Landed 2026-09-26: `EMAIL_WATCH`
    was already the allowlist; `EMAIL_DIGEST=on` (settable, Watchers group)
    queues a watched sender's mail for a day (`gmail_state`) instead of
    pushing it, and the briefing lists it under "Email:", five at most.

## Roadmap 2026-08-26 — "Jarvis, not Alexa"

The honest read of Wren at this point was: a household message bus with an
NLU front end, structurally the same shape as Alexa (one message, one intent,
one skill, one reply) with better language understanding, more doors, and no
cloud. What separates a Jarvis from an Alexa is not more nouns; it is
initiative (it speaks first), memory (it knows who it is talking to), and
composition (it chains things). This roadmap is the first three steps in
that direction, plus the one piece of safety the hardware device needs
before it goes live. The Trello board holds the state; this is the why.

1. **Memory slice 1** (spec `2026-08-18-memory-design.md`). Wren notices
   durable facts while chatting, keeps them without duplicating, and feeds
   the relevant ones back into later chat turns. Never into the classifier.
2. **Calendar + weather skills**, and **the daily briefing** (spec
   `2026-08-26-daily-briefing-design.md`). "What's today look like" becomes
   answerable, and once a day Wren composes the answer unprompted and pushes
   it through `NOTIFY_VIA` — with zero model calls, so it cannot invent an
   appointment. This is the first thing that composes several skills into
   one message, and the first proactive content worth a device.
3. **Voice confirmation** (spec `2026-08-26-voice-confirmation-design.md`).
   A message that arrived by `/voice` never deletes or cancels anything
   without a "yes". Skills declare what is destructive; core asks.

Deliberately not on this list: multi-intent turns ("look up X and save it")
— the 7B classifier is too fragile to grow its prompt for that yet — and the
device's own output channel, which cannot be designed until it is known
where the device's replies should land (a DM, a phone push, or the device).

Status as of 2026-08-27: all three are implemented and reviewed on stacked
branches (`worktree-memory-slice-1`, then `worktree-daily-briefing`, which
also carries voice confirmation) and await a merge to `main` plus a service
restart. `PROJECT_PLAN.md`'s "Backlog after the skill-cards work" above is
still valid and now sits behind these.

Merged to `main` and live since 2026-09-21 (fast-forward, 973 tests green; the
worktrees are gone). Memory needs no configuration; weather, calendar and the
briefing each stay off until their `.env` keys are set.
