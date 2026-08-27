# Voice Confirmation — Design

When a message arrived by voice, Wren asks before doing anything it cannot
undo. "Discard the idea about the kayak?" — "yes" — done. Text surfaces are
unchanged.

## Why

`POST /voice` (`http_plugin.py`) transcribes with faster-whisper and feeds the
transcript into the same single-shot path as typed text. Speech recognition
mis-hears; the classifier then fuzzy-matches. Today `discard_idea` is a hard
`DELETE`, `clear_shopping` empties the list, `cancel_reminder` and
`remove_shopping_item` act immediately, and the only undo anywhere is the
8-second button on the web chat's shopping card. A hardware device that
listens for a wake word is exactly the surface where "remove milk" becomes
"remove all" in a noisy kitchen. This has to land before that device goes
live, not after.

## Verified constraints (checked 2026-08-26)

1. Every surface reaches `core.handle_message(user_id, text, channel)`
   (`discord_plugin.py:82`, `telegram_plugin.py:254`, `http_plugin.py:51`,
   `webchat.py:269`). One entry point, so one guard covers every door.
2. `/voice` calls `_dispatch(user_id, transcript, transcript=transcript)`,
   the same helper `/message` uses. The voice request is stateless — the
   device gets `{"transcript", "replies", "files"}` back and nothing else.
   A confirmation therefore has to survive across two HTTP requests, which
   means it lives in Wren, not in the device.
3. `Ctx` is a dataclass with defaults (`channel.py`); adding a field with a
   default breaks no constructor call.
4. The classifier prompt must not grow (memory design, constraint 3).
   Yes/no detection is a regex in core, ahead of the LLM, not a new intent.

## Decisions

**D1 — `source` is a keyword argument on `handle_message`, defaulting to
`"text"`, and a field on `Ctx`.** Only `/voice` passes `source="voice"`.
The three chat plugins and `/message` are untouched. Skills can read
`ctx.source` later (a voice reply might want to be shorter); nothing in this
slice does.

**D2 — Skills declare what is destructive; core enforces it.** Each skill
gains `DESTRUCTIVE = [...]`, the subset of its `INTENTS` that delete or
irreversibly change data; `registry.destructive_intents()` unions them the
way `all_intents()` does. Core never names a skill. Initial declarations:

| skill | DESTRUCTIVE |
|---|---|
| shopping | `remove_shopping_item`, `clear_shopping` |
| notes | `discard_idea` |
| reminders | `cancel_reminder` |
| pins | `unpin_note` |
| contacts | `remove_contact` |
| memory | `forget_memory` |

Not destructive: anything that adds, saves, sets, recalls, sends. `restore_shopping_item`
is the undo, not the harm.

**D3 — The guard sits between intent detection and dispatch, in core.** When
`source == "voice"` and the intent is destructive, core stores
`_pending[user_id] = (intent, ctx, expires_at)` and sends one question built
from the skill's own words. The phrase comes from a
`CONFIRM = {intent: 'discard the idea "{content}"'}` template on the skill
beside `DESTRUCTIVE` — `{content}` is filled from `ctx.content` (or "that"
when empty) only where the template names it — so core sends
`Confirm: {phrase}? Say yes or no.` without knowing any skill's wording.
Nothing runs yet.

**D4 — The next turn from that user resolves it, before the LLM is called.**
At the top of `handle_message`, if `_pending` has an unexpired entry for this
user, `_NO` is checked first (`^(yeah,?\s*no|yes,?\s*no|no|nope|nah|never
mind|nevermind|don'?[’']?t)\b`, plus `cancel`/`stop` matched only when they
are the *entire* utterance — optionally followed by "it"/"that"/"this" and
trailing punctuation — via a separate `$`-anchored alternative, so "cancel my
6pm reminder" is a command, not an answer): it drops the question and sends
`Okay, left it alone.`. `_NO` runs before `_YES`
(`^(yes|yeah|yep|yup|do it|confirm|go ahead|sure)\b`) because "yeah no"
matches `^yeah\b` in `_YES` too and means no. A `_YES` match runs the stored
handler with the stored ctx (but the *current* channel — the reply goes
where the answer came from). Anything matching neither drops the question
silently and the message proceeds as a normal turn — the user changed the
subject, and asking again is nagging. The regexes run on the stripped text,
case-insensitively (`re.I`), not lower-cased first. No classifier call is
spent on a yes or a no.

**D5 — Pending confirmations expire after 120 seconds and are in-memory.** A
`dict` in core, one entry per user, overwritten by a newer destructive
request. `ponytail:` process-local — a restart forgets the question, which is
the safe direction. Persist it only if a multi-process deployment ever exists.

**D6 — Confirmation is per-source, not per-surface.** A "yes" typed in the
web chat resolves a question asked by voice. The dict is keyed by user, and
the person is the same person. This is also what makes the device simple:
the device does not need to know a question is outstanding.

**D7 — `restore_shopping_item` stays the undo for text.** Voice gets
confirm-first; text keeps act-first with the existing undo. Making every
destructive intent undoable on every surface was the alternative — it is
more code per skill and a new store column for most of them, for a problem
text users do not report having.

## Interfaces

```python
# wren/channel.py
@dataclass
class Ctx:
    ...
    source: str = "text"          # "text" | "voice"

# wren/core.py
async def handle_message(user_id: int, text: str, channel: Channel, *, source: str = "text") -> None

# wren/registry.py
def destructive_intents() -> set[str]        # union of enabled plugins' DESTRUCTIVE
def confirm_phrase(intent: str) -> str        # plugin.CONFIRM[intent], falls back to intent.replace("_", " ")

# each skill
DESTRUCTIVE = [...]
CONFIRM = {"discard_idea": 'discard the idea "{content}"', ...}

# wren/communication/http_plugin.py
_dispatch(user_id, transcript, source="voice", transcript=transcript)
```

`_pending: dict[int, tuple[str, Ctx, float]]` and `_YES`, `_NO`, `_CONFIRM_TTL = 120`
are module constants in core, not settings — see the memory plan's note on
why every knob costs the owner something.

## Flow

```
voice "remove milk"  -> detect_intent -> remove_shopping_item (destructive, source=voice)
                     -> _pending[uid] = (intent, ctx, now+120)
                     -> send: Confirm: remove "milk" from the shopping list? Say yes or no.
voice "yes"          -> _pending hit, _YES matches -> shopping_skill.handle(intent, ctx') -> "Removed milk."
voice "no"           -> _pending hit, _NO matches  -> "Okay, left it alone."
voice "what's the weather" -> _pending hit, neither matches -> drop, continue as a normal turn
text  "remove milk"  -> unchanged: acts immediately
```

`ctx'` is the stored ctx with `channel` replaced by the current one, so a
question asked by voice and answered in the web chat replies in the web chat.

## Error handling

- The stored handler raising is caught by the pending block's own
  `try`/`except` (a separate block from the main turn's, ahead of the
  classifier call) — it mirrors the main path exactly: the same "Something
  went wrong, try again." reply and `ack("error")`.
- A skill switched off between the question and the "yes" is checked again
  at confirmation time through the same `is_enabled` guard dispatch uses; the
  reply is `That skill is switched off.`
- An expired entry is treated as absent (the "yes" becomes a normal turn and
  the classifier does what it does with a bare "yes" today).

## Testing

`tests/test_core.py`, in its existing `CollectingChannel` + patched
`detect_intent` shape:

- voice + destructive intent → handler not called, question sent, `_pending` set
- voice + non-destructive intent → handler called immediately (unchanged)
- text + destructive intent → handler called immediately (unchanged)
- "yes" after a pending question → handler called with the stored intent/content on the *current* channel
- "no" → not called, "Okay, left it alone."
- unrelated text → not called, message handled normally, `_pending` cleared
- expiry → after `_CONFIRM_TTL`, "yes" is a normal turn
- a newer destructive request replaces the older pending one
- `registry.destructive_intents()` only includes enabled skills
- `http_plugin` test: `/voice` passes `source="voice"`, `/message` does not

Each skill's test file gets one assertion that `DESTRUCTIVE ⊆ INTENTS` and
`CONFIRM.keys() == set(DESTRUCTIVE)`.

## Deliberately not in this slice

- A confirmation *card* in the web chat (voice never renders one).
- Undo on Discord/Telegram for text-originated deletes.
- Reading `ctx.source` in any skill.
- Per-intent TTLs or a setting for the TTL.
- A "yes" carrying extra instructions ("yes and also add eggs") runs only the
  confirmed intent; the rest is dropped.
