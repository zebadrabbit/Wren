# Memory — Design

Wren notices durable facts about the people it talks to and recalls them
later, without being asked to. Two passes: the conversation is untouched,
and a separate background pass reads what was said and decides what is worth
keeping.

## Why this shape

The engine runs on a 7B local instruct model. That model is not good enough
to judge "is this worth remembering?" *while* also answering, and it is not
fast enough to do both inside one turn. So extraction is its own pass, with
its own narrow prompt, off the hot path entirely.

## Verified constraints (checked 2026-08-18, not assumed)

1. **The inference server cannot embed.** `POST /v1/embeddings` against
   `http://192.168.1.70:30068/v1` returns
   `This server does not support embeddings. Start it with --embeddings`.
   Any design that needs vectors is an infrastructure change first and a code
   change second.
2. **FTS5 is available** in the venv's sqlite (3.45.1) — but see D2, we don't
   need it.
3. **The classifier is fragile in a specific way.** On 2026-08-18 a reminder
   was silently dropped because conversational prose in `detect_intent`'s
   prompt made the model answer in prose instead of emitting JSON. Reproduced
   3/3. Fixed by `response_format={"type":"json_object"}`, but the lesson
   stands: *every byte added to the classifier prompt is a risk*, and memory
   injection must stay out of it.

## Decisions

**D1 — Similarity is one stdlib function, not embeddings.**
Dedup and retrieval both call `memory_skill.similarity(a, b)`: Jaccard over
stop-word-stripped token sets. Zero new dependencies, no model to keep loaded,
no infra to change. Calibrated against fixtures:

| pair | score |
|---|---|
| `prefers oat milk` / `prefers oat milk in coffee` | 0.75 |
| `sister Kate lives in Denver` / `… in Denver now` | 0.80 |
| `works on a project called Wren` / `works on the Wren project` | 0.75 |
| `sister is Kate` / `sister is Kim` | 0.33 |
| `likes coffee` / `likes tea` | 0.33 |

Merges land 0.75+, wrong merges land 0.33 — so `MEMORY_DEDUP_THRESHOLD`
defaults to **0.7**, not the spec's 0.85 (that number was for cosine
similarity and does not transfer).

*Known limitation, accepted:* `dislikes cilantro` vs `hates cilantro` scores
0.33 and will store a duplicate. Lexical similarity cannot see synonyms. This
is the one thing embeddings would buy, which is why `similarity()` is a single
function with a single call site each for dedup and retrieval — swapping in
cosine over real vectors later touches that function and nothing else.

**D2 — Retrieval scores in Python, no FTS5 table.**
One household's memories are hundreds of rows, not millions. Reading them and
scoring with the *same* `similarity()` used for dedup means one behaviour to
learn, one function to test, and no shadow table to keep in sync. Injection
floor is 0.1 (relevant fixtures scored ~0.17, irrelevant 0.00), top-k 5.

**D3 — Memories are injected into `brain.chat()` only. Never `detect_intent`.**
See verified constraint 3. The classifier's prompt does not grow.

**D4 — A durable queue, not a watermark over `messages`.**
`core.handle_message` runs the pure gate and, if it passes, writes one row to
`memory_queue` (a microsecond-scale INSERT, no model call). The sweeper drains
that queue every `MEMORY_SWEEP_SECONDS`. This keeps the LLM off the hot path,
works for *every* surface (only web chat writes to `messages`, so a watermark
there would mean Discord and Telegram never contribute), and survives a
restart mid-turn because the queue is on disk.

**D5 — Only turns that fell through to `chat` are observed.**
Skill turns are commands, not disclosures: "show my shopping" and "remind me
tomorrow at 6pm" tell Wren what to *do*, not who you are. Observing only the
`chat` branch cuts the false-fire rate sharply for one line of code. The cost
is that a disclosure buried inside a command ("remind me to take my insulin at
8") is not remembered — which is the right call for medical detail anyway.

**D6 — The owner can see and delete everything, in the first slice.**
Wren is going to be released for other people to self-host. A background pass
that quietly accumulates a profile with no way to read or erase it is not
shippable, so `recall_memories` and `forget_memory` ship with the extraction,
not after it. The skill also appears in the plugins panel like every other, so
it can be switched off entirely.

## Data model

```sql
CREATE TABLE memories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id      TEXT NOT NULL,
    category      TEXT NOT NULL,        -- preference | person | project | fact
    fact          TEXT NOT NULL,
    mention_count INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);
CREATE TABLE memory_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   TEXT NOT NULL,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

No `embedding` column: adding one later is the additive, nullable migration
`conversations.py` already demonstrates. A column of NULLs today is a column
of NULLs forever if the vectors never arrive.

## Flow

```
user turn ─→ core.handle_message
              ├─ detect_intent            (prompt unchanged — D3)
              ├─ skill.handle(...)        → nothing observed  (D5)
              └─ chat branch:
                   memories = memory_skill.for_prompt(user_id, text)   ← retrieval
                   brain.chat(text, history, memories=memories)        ← injection
                   memory_skill.observe(user_id, text)                 ← gate + enqueue

background ─→ memory_skill.start()  every MEMORY_SWEEP_SECONDS
               drain queue → brain.extract_facts(text)  [json_mode]
                          → parse_facts(raw)   strict; malformed ⇒ []
                          → dedupe against store: ≥ threshold ⇒ bump(), else save()
```

## Taxonomy

`preference | person | project | fact`, and `none`. Anything the parser does
not recognise is dropped. No retry loop: a small model that got it wrong once
gets it wrong again, and the same sentence will come round on the next turn
anyway.

## Config

| key | default | settable live |
|---|---|---|
| `MEMORY_SWEEP_SECONDS` | 300 | yes |
| `MEMORY_DEDUP_THRESHOLD` | 0.7 | yes |
| `MEMORY_TOP_K` | 5 | yes |

`_CORE_MENTIONS` (3) and `_MIN_TOKENS` (12) stay module constants — every knob
in `SETTABLE` is a value someone has to understand before they can change
anything, and these two are not worth that tax until tuning proves otherwise.

## Non-goals

- Memory does not answer questions from stored facts the way `recall_notes`
  does. Notes are what you *asked* Wren to keep; memory is what it noticed.
  Keeping them separate is what stops "what do I need to do" from returning a
  pile of ambient trivia.
- No editing a memory's text. Forget it and say it again.
- No cross-user memories. Facts are filed per `owner_id`, like everything else.
