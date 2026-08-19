# Memory — Slice 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wren notices durable facts about a person while chatting, keeps them
in SQLite without duplicating what it already knows, and quietly feeds the
relevant ones back into later conversations.

**Architecture:** Two passes. The chat turn is unchanged except that the `chat`
branch gets a bullet list of remembered facts in its system prompt and drops
one row into a durable queue afterwards. A background sweeper drains that queue
every few minutes, asks the same local model to extract facts with a narrow
JSON-mode prompt, and merges each candidate into the store by lexical
similarity — no embeddings, no new dependencies.

**Tech Stack:** Python 3.12, stdlib `sqlite3` and `re`, asyncio, pytest. No new
entries in `requirements.txt`.

**Spec:** `docs/superpowers/specs/2026-08-18-memory-design.md`

## Global Constraints

From `CLAUDE.md` and the spec. Every task implicitly includes these.

- **A skill never imports a transport.** `memory_skill.py` touches `ctx.channel`
  and nothing else transport-shaped.
- **Never run anything against the real `wren.db`.** `tests/conftest.py` points
  `WREN_DB` at a tmp file per test. Any manual check must
  `export WREN_DB=$(mktemp -d)/scratch.db` first.
- **Memories never enter `brain.detect_intent`'s prompt.** A reminder was
  silently lost on 2026-08-18 because prose in that prompt stopped the model
  emitting JSON. Injection goes in `brain.chat()` only.
- **Extraction never blocks a turn.** The hot path runs a pure regex gate and
  one INSERT. Every model call happens in the sweeper, inside
  `asyncio.to_thread`.
- **Strict parse, no retries.** Malformed extraction output is `[]`, logged,
  dropped.
- **Every test module starts with the env preamble** used by its neighbours:
  ```python
  import os
  os.environ.setdefault("DISCORD_TOKEN", "test")
  os.environ.setdefault("WREN_OWNER_ID", "1")
  os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
  os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
  os.environ.setdefault("LMSTUDIO_MODEL", "test-model")
  os.environ.setdefault("TIMEZONE", "UTC")
  ```
- **No model calls in tests.** Patch `brain._get_client` (see
  `tests/test_brain.py`'s `_client_returning`) or `brain.extract_facts`.

---

### Task 1: The store

**Files:**
- Create: `wren/skills/memory_store.py`
- Modify: `wren/run.py:18` (`_STORAGE` tuple)
- Test: `tests/test_memory.py`

**Interfaces:**
- Produces: `init_db()`, `save(owner_id:int, category:str, fact:str) -> int`,
  `all_for(owner_id:int) -> list[dict]`, `bump(memory_id:int) -> None`,
  `forget(memory_id:int) -> bool`, `enqueue(owner_id:int, text:str) -> None`,
  `drain(limit:int=20) -> list[dict]`.

- [ ] **Step 1: Write the failing tests**

```python
import os, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")
os.environ.setdefault("TIMEZONE", "UTC")

from wren.skills import memory_store as memory

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("WREN_DB", str(tmp_path / "wren.db"))
    memory.init_db()

def test_save_and_read_back():
    memory.save(1, "preference", "dislikes cilantro")
    rows = memory.all_for(1)
    assert len(rows) == 1
    assert rows[0]["fact"] == "dislikes cilantro"
    assert rows[0]["category"] == "preference"
    assert rows[0]["mention_count"] == 1

def test_memories_are_per_owner():
    memory.save(1, "fact", "mine")
    memory.save(2, "fact", "theirs")
    assert [r["fact"] for r in memory.all_for(1)] == ["mine"]

def test_bump_increments_and_touches_last_seen():
    mid = memory.save(1, "fact", "runs a home server")
    before = memory.all_for(1)[0]["last_seen_at"]
    memory.bump(mid)
    row = memory.all_for(1)[0]
    assert row["mention_count"] == 2
    assert row["last_seen_at"] >= before

def test_all_for_orders_by_mention_count():
    memory.save(1, "fact", "quiet one")
    loud = memory.save(1, "fact", "loud one")
    memory.bump(loud)
    assert memory.all_for(1)[0]["fact"] == "loud one"

def test_forget_removes_and_reports():
    mid = memory.save(1, "fact", "temporary")
    assert memory.forget(mid) is True
    assert memory.all_for(1) == []
    assert memory.forget(mid) is False

def test_queue_drains_once():
    memory.enqueue(1, "my sister Kate lives in Denver")
    drained = memory.drain()
    assert [r["text"] for r in drained] == ["my sister Kate lives in Denver"]
    assert int(drained[0]["owner_id"]) == 1
    assert memory.drain() == []          # delete-on-read: never served twice

def test_drain_respects_limit_and_keeps_the_rest():
    for i in range(3):
        memory.enqueue(1, f"turn {i}")
    assert len(memory.drain(limit=2)) == 2
    assert [r["text"] for r in memory.drain()] == ["turn 2"]
```

- [ ] **Step 2: Run them and watch them fail**

Run: `venv/bin/python3 -m pytest -q tests/test_memory.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'wren.skills.memory_store'`

- [ ] **Step 3: Write the store**

```python
import sqlite3
from datetime import datetime, timezone

from .. import db

def init_db() -> None:
    with db.conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id      TEXT NOT NULL,
                category      TEXT NOT NULL,
                fact          TEXT NOT NULL,
                mention_count INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT NOT NULL,
                last_seen_at  TEXT NOT NULL
            )
        """)
        # The queue is a table rather than an in-process list on purpose: a
        # restart between "you said it" and "the sweeper read it" must not
        # lose the turn, and every surface writes to it, not just web chat.
        con.execute("""
            CREATE TABLE IF NOT EXISTS memory_queue (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id   TEXT NOT NULL,
                text       TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_memories_owner ON memories(owner_id)")

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def save(owner_id: int, category: str, fact: str) -> int:
    ts = _now()
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO memories (owner_id, category, fact, mention_count, created_at, last_seen_at)"
            " VALUES (?,?,?,1,?,?)",
            (str(owner_id), category, fact, ts, ts),
        )
        return cur.lastrowid

def all_for(owner_id: int) -> list[dict]:
    with db.conn() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM memories WHERE owner_id=?"
            " ORDER BY mention_count DESC, last_seen_at DESC",
            (str(owner_id),),
        ).fetchall()
    return [dict(r) for r in rows]

def bump(memory_id: int) -> None:
    with db.conn() as con:
        con.execute(
            "UPDATE memories SET mention_count = mention_count + 1, last_seen_at=? WHERE id=?",
            (_now(), memory_id),
        )

def forget(memory_id: int) -> bool:
    with db.conn() as con:
        return con.execute("DELETE FROM memories WHERE id=?", (memory_id,)).rowcount > 0

def enqueue(owner_id: int, text: str) -> None:
    with db.conn() as con:
        con.execute(
            "INSERT INTO memory_queue (owner_id, text, created_at) VALUES (?,?,?)",
            (str(owner_id), text, _now()),
        )

def drain(limit: int = 20) -> list[dict]:
    """Take up to `limit` queued turns and delete them in the same transaction.

    Delete-on-read rather than mark-as-done: a turn that makes the extractor
    fall over must not come back every sweep forever, and anything genuinely
    worth remembering gets said again.
    """
    with db.conn() as con:
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM memory_queue ORDER BY id LIMIT ?", (limit,)).fetchall()]
        if rows:
            con.execute(
                "DELETE FROM memory_queue WHERE id IN (%s)" % ",".join("?" * len(rows)),
                [r["id"] for r in rows],
            )
    return rows
```

- [ ] **Step 4: Register it for boot**

In `wren/run.py`, add the import beside the other stores and extend `_STORAGE`:

```python
from .skills import memory_store as memory
...
_STORAGE = (notes, shopping, reminders, contacts, github_state, pins, conversations, settings, memory)
```

- [ ] **Step 5: Run the tests**

Run: `venv/bin/python3 -m pytest -q tests/test_memory.py`
Expected: PASS (7 tests)

- [ ] **Step 6: Commit**

```bash
git add wren/skills/memory_store.py wren/run.py tests/test_memory.py
git commit -m "feat(memory): store for remembered facts and the extraction queue"
```

---

### Task 2: The gate

**Files:**
- Create: `wren/skills/memory_skill.py`
- Test: `tests/test_memory_plugin.py`

**Interfaces:**
- Produces: `should_extract(text: str) -> bool`. Pure: no I/O, no config reads.

- [ ] **Step 1: Write the failing tests**

```python
import os, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")
os.environ.setdefault("TIMEZONE", "UTC")

from wren.skills import memory_skill as memory_plugin
from wren.skills import memory_store as memory

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("WREN_DB", str(tmp_path / "wren.db"))
    memory.init_db()

@pytest.mark.parametrize("text", [
    "I hate cilantro",                       # first person
    "my sister Kate lives in Denver",        # first person + entity
    "we're moving to Denver",                # first person plural
    "the standup is tomorrow at 9am",        # date/time
    "Kate called about the car",             # capitalised entity
    "the deploy pipeline for that service keeps failing whenever the cache is "
    "cold and nobody has worked out why yet",  # over the length threshold
])
def test_gate_fires(text):
    assert memory_plugin.should_extract(text) is True

@pytest.mark.parametrize("text", ["thanks", "ok", "", "   ", "what's the weather",
                                  "sounds good", "no worries"])
def test_gate_skips(text):
    assert memory_plugin.should_extract(text) is False

def test_gate_ignores_a_leading_capital():
    # every sentence starts with one; only a capital *inside* the sentence is
    # evidence of a name
    assert memory_plugin.should_extract("Sounds fine") is False
```

- [ ] **Step 2: Run them and watch them fail**

Run: `venv/bin/python3 -m pytest -q tests/test_memory_plugin.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'wren.skills.memory_skill'`

- [ ] **Step 3: Write the gate**

Create `wren/skills/memory_skill.py` with the imports and the gate. The rest
of the module arrives in later tasks.

```python
import asyncio
import json
import logging
import re

from .. import brain
from .. import config
from .. import flourish
from . import memory_store as memories
from ..channel import Ctx

INTENTS = ["recall_memories", "forget_memory"]
PLUGIN_NAME = "Memory"

CATEGORIES = ("preference", "person", "project", "fact")

# Module constants, not settings: every knob in config.SETTABLE is one more
# value an owner has to understand before they can change anything else.
# Promote these only if tuning proves they need it.
_MIN_TOKENS = 12
_CORE_MENTIONS = 3
_INJECT_FLOOR = 0.1

_FIRST_PERSON = re.compile(r"\b(i|i'm|im|i've|my|mine|me|we|we're|our|us)\b", re.I)
_WHEN = re.compile(
    r"\b(today|tonight|tomorrow|yesterday|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday|next\s+(week|month|year)|\d{1,2}\s?(am|pm)|\d{4}-\d{2}-\d{2})\b",
    re.I,
)

def _has_entity(text: str) -> bool:
    # words[1:]: the first word of a sentence is capitalised whatever it is,
    # so only a capital further in is evidence of a name.
    return any(w[:1].isupper() and w[1:2].islower() and len(w) > 2
               for w in text.split()[1:])

def should_extract(text: str) -> bool:
    """Is this turn worth spending a model call on?

    Deliberately permissive — a false fire costs one background call and the
    extractor answers "nothing"; a false skip loses the fact forever. Pure, so
    it can be tuned against a corpus without standing a model up.
    """
    text = (text or "").strip()
    if not text:
        return False
    return bool(_FIRST_PERSON.search(text) or _WHEN.search(text)
                or _has_entity(text) or len(text.split()) > _MIN_TOKENS)
```

- [ ] **Step 4: Run the tests**

Run: `venv/bin/python3 -m pytest -q tests/test_memory_plugin.py`
Expected: PASS (14 tests)

- [ ] **Step 5: Commit**

```bash
git add wren/skills/memory_skill.py tests/test_memory_plugin.py
git commit -m "feat(memory): pure pre-filter gate for extraction"
```

---

### Task 3: The parser

**Files:**
- Modify: `wren/skills/memory_skill.py`
- Test: `tests/test_memory_plugin.py`

**Interfaces:**
- Consumes: `CATEGORIES` from Task 2.
- Produces: `parse_facts(raw: str) -> list[dict]` where each dict is
  `{"category": str, "fact": str}` and `category` is one of `CATEGORIES`.

- [ ] **Step 1: Write the failing tests (append to `tests/test_memory_plugin.py`)**

```python
def test_parse_extracts_valid_facts():
    raw = '{"facts": [{"category": "preference", "fact": "dislikes cilantro"}]}'
    assert memory_plugin.parse_facts(raw) == [
        {"category": "preference", "fact": "dislikes cilantro"}]

def test_parse_empty_list_is_none():
    assert memory_plugin.parse_facts('{"facts": []}') == []

@pytest.mark.parametrize("raw", [
    "",
    "NONE",
    "Sure! Here's what I found:",           # the model answering instead of classifying
    '{"facts": "dislikes cilantro"}',       # right key, wrong type
    '{"nope": []}',                         # right shape, wrong key
    '{"facts": [{"category": "preference"',  # truncated at max_tokens
])
def test_parse_malformed_is_none(raw):
    assert memory_plugin.parse_facts(raw) == []

def test_parse_drops_unknown_categories():
    raw = ('{"facts": [{"category": "vibe", "fact": "seems tired"},'
           ' {"category": "fact", "fact": "runs a home server"}]}')
    assert memory_plugin.parse_facts(raw) == [
        {"category": "fact", "fact": "runs a home server"}]

def test_parse_drops_empty_fact_text():
    assert memory_plugin.parse_facts('{"facts": [{"category": "fact", "fact": "  "}]}') == []

def test_parse_tolerates_a_code_fence():
    raw = '```json\n{"facts": [{"category": "fact", "fact": "owns a kayak"}]}\n```'
    assert memory_plugin.parse_facts(raw) == [{"category": "fact", "fact": "owns a kayak"}]
```

- [ ] **Step 2: Run and watch them fail**

Run: `venv/bin/python3 -m pytest -q tests/test_memory_plugin.py -k parse`
Expected: FAIL — `AttributeError: module 'wren.skills.memory_skill' has no attribute 'parse_facts'`

- [ ] **Step 3: Write the parser**

```python
def parse_facts(raw: str) -> list[dict]:
    """Strict. Anything unexpected is "nothing to remember", never a retry.

    The model is a 7B instruct running with response_format=json_object, so
    well-formed output is the norm and malformed output means it lost the
    plot on this particular sentence. Asking it again costs a call to get the
    same confusion back; the sentence will come round again if it mattered.
    """
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        data = json.loads(raw)
    except Exception:
        logging.info(f"memory: unparseable extraction, treated as none: {raw[:200]!r}")
        return []
    items = data.get("facts") if isinstance(data, dict) else None
    if not isinstance(items, list):
        logging.info(f"memory: extraction had no 'facts' list, treated as none: {raw[:200]!r}")
        return []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category", "")).strip().lower()
        fact = str(item.get("fact", "")).strip()
        # "none" lands here too and is dropped by the same check, which is
        # why the taxonomy does not need a branch of its own.
        if category in CATEGORIES and fact:
            out.append({"category": category, "fact": fact})
    return out
```

- [ ] **Step 4: Run the tests**

Run: `venv/bin/python3 -m pytest -q tests/test_memory_plugin.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add wren/skills/memory_skill.py tests/test_memory_plugin.py
git commit -m "feat(memory): strict extraction parser, malformed output is none"
```

---

### Task 4: Similarity and dedup

**Files:**
- Modify: `wren/skills/memory_skill.py`
- Test: `tests/test_memory_plugin.py`

**Interfaces:**
- Consumes: `memory_store.all_for/save/bump` (Task 1).
- Produces: `similarity(a: str, b: str) -> float` (0.0–1.0) and
  `remember(owner_id: int, candidates: list[dict]) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
def test_similarity_is_one_for_identical_text():
    assert memory_plugin.similarity("prefers oat milk", "prefers oat milk") == 1.0

def test_similarity_high_for_a_restatement():
    assert memory_plugin.similarity(
        "sister Kate lives in Denver", "sister Kate lives in Denver now") >= 0.7

def test_similarity_low_for_a_different_fact_in_the_same_shape():
    # the case a threshold must never merge: one word apart, opposite meaning
    assert memory_plugin.similarity("sister is Kate", "sister is Kim") < 0.5
    assert memory_plugin.similarity("likes coffee", "likes tea") < 0.5

def test_similarity_ignores_stopwords_and_case():
    assert memory_plugin.similarity("Owns A Kayak", "owns the kayak") == 1.0

def test_similarity_of_empty_text_is_zero():
    assert memory_plugin.similarity("", "owns a kayak") == 0.0

def test_remember_stores_a_new_fact():
    memory_plugin.remember(1, [{"category": "preference", "fact": "dislikes cilantro"}])
    assert [r["fact"] for r in memory.all_for(1)] == ["dislikes cilantro"]

def test_remember_bumps_a_near_duplicate_instead_of_storing_it():
    memory.save(1, "person", "sister Kate lives in Denver")
    memory_plugin.remember(1, [{"category": "person", "fact": "sister Kate lives in Denver now"}])
    rows = memory.all_for(1)
    assert len(rows) == 1
    assert rows[0]["mention_count"] == 2

def test_remember_keeps_a_genuinely_different_fact():
    memory.save(1, "person", "sister is Kate")
    memory_plugin.remember(1, [{"category": "person", "fact": "sister is Kim"}])
    assert len(memory.all_for(1)) == 2

def test_remember_dedups_within_one_batch():
    memory_plugin.remember(1, [{"category": "fact", "fact": "owns a kayak"},
                               {"category": "fact", "fact": "owns a kayak"}])
    rows = memory.all_for(1)
    assert len(rows) == 1
    assert rows[0]["mention_count"] == 2

def test_remember_respects_the_configured_threshold(monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DEDUP_THRESHOLD", 0.99)
    memory.save(1, "person", "sister Kate lives in Denver")
    memory_plugin.remember(1, [{"category": "person", "fact": "sister Kate lives in Denver now"}])
    assert len(memory.all_for(1)) == 2      # 0.8 no longer clears the bar
```

Add `from wren import config` to the test module's imports.

- [ ] **Step 2: Run and watch them fail**

Run: `venv/bin/python3 -m pytest -q tests/test_memory_plugin.py -k "similarity or remember"`
Expected: FAIL — `AttributeError: ... has no attribute 'similarity'`

- [ ] **Step 3: Write similarity and dedup**

Note `config.MEMORY_DEDUP_THRESHOLD` does not exist until Task 8; add it there
and, until then, these tests pass because `monkeypatch.setattr` is only used in
the last one. **Do Task 8's config block first if you are executing out of
order.** Otherwise add this line to `wren/config.py` now, beside
`REMINDER_POLL_SECONDS`:

```python
MEMORY_DEDUP_THRESHOLD = float(os.environ.get("MEMORY_DEDUP_THRESHOLD", "0.7"))
```

```python
# Stop words carry no evidence, and leaving them in inflates every score
# toward "these two sentences are both English".
_STOP = {"a", "an", "the", "i", "im", "i'm", "is", "are", "was", "were", "to", "of",
         "and", "that", "it", "for", "in", "on", "my", "me", "we", "us", "our",
         "you", "your", "has", "have", "had", "be", "at"}

def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", (text or "").lower()) if w not in _STOP}

def similarity(a: str, b: str) -> float:
    """Jaccard overlap of content words. 0.0 (nothing shared) to 1.0 (same words).

    ponytail: lexical, so "dislikes cilantro" and "hates cilantro" read as
    different facts and both get stored. That is the one thing embeddings
    would buy, and it is not worth a torch dependency in a repo people are
    meant to self-host from a six-line requirements.txt. Swap the body for
    cosine over real vectors if duplicate drift ever gets annoying -- this
    function is the only place either caller looks.
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)

def remember(owner_id: int, candidates: list[dict]) -> None:
    """Merge candidates into the store, logging every decision.

    `known` is updated as we go, not re-read: two identical candidates in one
    batch must merge into each other rather than both being stored.
    """
    known = memories.all_for(owner_id)
    for candidate in candidates:
        best, score = None, 0.0
        for row in known:
            s = similarity(candidate["fact"], row["fact"])
            if s > score:
                best, score = row, s
        if best is not None and score >= config.MEMORY_DEDUP_THRESHOLD:
            memories.bump(best["id"])
            best["mention_count"] = best.get("mention_count", 1) + 1
            logging.info(f"memory: dedup {candidate['fact']!r} into #{best['id']} "
                         f"{best['fact']!r} (sim={score:.2f})")
        else:
            new_id = memories.save(owner_id, candidate["category"], candidate["fact"])
            known.append({"id": new_id, "fact": candidate["fact"],
                          "category": candidate["category"], "mention_count": 1})
            logging.info(f"memory: stored #{new_id} [{candidate['category']}] "
                         f"{candidate['fact']!r} (best sim={score:.2f})")
```

- [ ] **Step 4: Run the tests**

Run: `venv/bin/python3 -m pytest -q tests/test_memory_plugin.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add wren/skills/memory_skill.py wren/config.py tests/test_memory_plugin.py
git commit -m "feat(memory): lexical similarity and dedup-by-merge"
```

---

### Task 5: The extraction call

**Files:**
- Modify: `wren/brain.py`
- Test: `tests/test_brain.py`

**Interfaces:**
- Consumes: `brain._complete(messages, temperature, max_tokens, json_mode)`.
- Produces: `brain.extract_facts(text: str) -> str` — the model's raw reply,
  parsed by `memory_skill.parse_facts`.

- [ ] **Step 1: Write the failing tests (append to `tests/test_brain.py`)**

```python
def test_extract_facts_returns_raw_model_output():
    payload = '{"facts": [{"category": "preference", "fact": "dislikes cilantro"}]}'
    with patch.object(brain, "_get_client", return_value=_client_returning(payload)):
        assert brain.extract_facts("I can't stand cilantro") == payload

def test_extract_facts_asks_for_json_and_sends_no_history():
    client = _client_returning('{"facts": []}')
    with patch.object(brain, "_get_client", return_value=client):
        brain.extract_facts("thanks Wren")
    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["response_format"] == {"type": "json_object"}
    # one system prompt + the message under test, nothing else: conversation
    # history is what taught the classifier to answer in prose instead of JSON
    assert [m["role"] for m in kwargs["messages"]] == ["system", "user"]

def test_extract_facts_prompt_shows_negative_examples():
    # small models overfire; the empty answers are the important half
    assert brain._EXTRACT.count('{"facts": []}') >= 2
```

- [ ] **Step 2: Run and watch them fail**

Run: `venv/bin/python3 -m pytest -q tests/test_brain.py -k extract`
Expected: FAIL — `AttributeError: module 'wren.brain' has no attribute 'extract_facts'`

- [ ] **Step 3: Write the prompt and the call**

```python
_EXTRACT = """You extract durable facts about a user from a single message.

A durable fact is still true next week: a preference, a person in their life,
a project they work on, or a stable fact about them. Moods, questions,
commands, plans already handled elsewhere, and small talk are NOT durable
facts. When in doubt, extract nothing.

Reply ONLY with JSON in this exact shape:
{"facts": [{"category": "preference|person|project|fact", "fact": "<short third-person statement>"}]}

Message: "I can't stand cilantro, it ruins everything"
{"facts": [{"category": "preference", "fact": "dislikes cilantro"}]}

Message: "what's the weather looking like tomorrow"
{"facts": []}

Message: "thanks Wren, you're the best"
{"facts": []}

Message: "my sister Kate just moved to Denver"
{"facts": [{"category": "person", "fact": "sister Kate lives in Denver"}]}
"""

def extract_facts(text: str) -> str:
    """One message in, raw JSON out. No history, by design — see detect_intent.

    temperature 0: this is a classification, and a creative extractor invents
    facts about people, which is the worst failure this feature can have.
    """
    return _complete(
        [{"role": "system", "content": _EXTRACT},
         {"role": "user", "content": f'Message: "{text}"'}],
        temperature=0.0,
        max_tokens=200,
        json_mode=True,
    )
```

- [ ] **Step 4: Run the tests**

Run: `venv/bin/python3 -m pytest -q tests/test_brain.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add wren/brain.py tests/test_brain.py
git commit -m "feat(memory): extraction prompt and json-mode call"
```

---

### Task 6: The sweeper and the hot-path hook

**Files:**
- Modify: `wren/skills/memory_skill.py`, `wren/core.py:150-153` (the `chat`
  branch), `wren/registry.py:14` (`PLUGINS`)
- Test: `tests/test_memory_plugin.py`, `tests/test_core.py`

**Interfaces:**
- Consumes: `should_extract` (Task 2), `parse_facts` (Task 3), `remember`
  (Task 4), `brain.extract_facts` (Task 5), `memory_store.enqueue/drain` (Task 1).
- Produces: `observe(user_id: int, text: str) -> None`, `_sweep() -> None`,
  `async start() -> None`.

- [ ] **Step 1: Write the failing tests**

```python
from unittest.mock import patch

def test_observe_enqueues_a_qualifying_turn():
    memory_plugin.observe(1, "my sister Kate lives in Denver")
    assert len(memory.drain()) == 1

def test_observe_skips_a_turn_the_gate_rejects():
    memory_plugin.observe(1, "thanks")
    assert memory.drain() == []

def test_sweep_extracts_dedups_and_empties_the_queue():
    memory_plugin.observe(1, "my sister Kate lives in Denver")
    payload = '{"facts": [{"category": "person", "fact": "sister Kate lives in Denver"}]}'
    with patch.object(memory_plugin.brain, "extract_facts", return_value=payload):
        memory_plugin._sweep()
    assert [r["fact"] for r in memory.all_for(1)] == ["sister Kate lives in Denver"]
    assert memory.drain() == []

def test_sweep_survives_a_dead_model():
    memory_plugin.observe(1, "my sister Kate lives in Denver")
    with patch.object(memory_plugin.brain, "extract_facts", side_effect=RuntimeError("down")):
        memory_plugin._sweep()          # must not raise: it runs in a background task
    assert memory.all_for(1) == []

def test_sweep_stores_nothing_when_the_model_says_nothing():
    memory_plugin.observe(1, "I guess that's fine then, whatever you think")
    with patch.object(memory_plugin.brain, "extract_facts", return_value='{"facts": []}'):
        memory_plugin._sweep()
    assert memory.all_for(1) == []
```

In `tests/test_core.py`, add (matching that module's existing `CollectingChannel`
+ `handle_message` style), with `from wren.skills import memory_store as memory`
added to its imports and its `tmp_db` fixture calling `memory.init_db()`:

```python
def test_chat_turn_is_observed_for_memory():
    ch = CollectingChannel()
    with patch.object(brain, "detect_intent", return_value={"intent": "chat", "content": "x"}), \
         patch.object(brain, "chat", return_value="sure"):
        asyncio.run(core.handle_message(1, "my sister Kate lives in Denver", ch))
    assert len(memory.drain()) == 1

def test_skill_turn_is_not_observed_for_memory():
    ch = CollectingChannel()
    with patch.object(brain, "detect_intent",
                      return_value={"intent": "add_shopping_item", "content": "milk"}):
        asyncio.run(core.handle_message(1, "add milk, my usual", ch))
    assert memory.drain() == []
```

- [ ] **Step 2: Run and watch them fail**

Run: `venv/bin/python3 -m pytest -q tests/test_memory_plugin.py tests/test_core.py -k "observe or sweep or memory"`
Expected: FAIL — `AttributeError: ... has no attribute 'observe'`

- [ ] **Step 3: Write observe, the sweep and the loop**

```python
def observe(user_id: int, text: str) -> None:
    """Hot path. A regex and one INSERT — no model call, no network."""
    if not should_extract(text):
        logging.debug(f"memory: gate skipped {text[:60]!r}")
        return
    memories.enqueue(user_id, text)

def _sweep() -> None:
    """Drain the queue and merge whatever the model finds. Blocking by design:
    start() runs it in a thread so the sqlite writes and the LLM call never
    touch the event loop shared by every surface."""
    # Local imports: registry imports this module, and this module needs a
    # reference to itself to ask registry whether it is switched on. At module
    # scope either one is a cycle.
    from .. import registry
    from . import memory_skill

    # An owner who switches Memory off mid-day has queued turns already on
    # disk; draining them anyway would extract facts from a skill that is off.
    if not registry.is_enabled(memory_skill):
        return
    for row in memories.drain():
        owner_id, text = int(row["owner_id"]), row["text"]
        try:
            raw = brain.extract_facts(text)
        except Exception as e:
            logging.warning(f"memory: extraction call failed, dropping turn: {e}")
            continue
        candidates = parse_facts(raw)
        logging.info(f"memory: {len(candidates)} candidate(s) from {text[:60]!r}")
        remember(owner_id, candidates)

async def start() -> None:
    # Sleeps first: the queue is empty at boot, and a sweep racing the
    # surfaces' own startup buys nothing.
    while True:
        await asyncio.sleep(config.MEMORY_SWEEP_SECONDS)
        try:
            await asyncio.to_thread(_sweep)
        except Exception as e:
            logging.warning(f"memory sweep failed: {e}")
```

- [ ] **Step 4: Register the skill and hook the chat branch**

`wren/registry.py`:

```python
from .skills import memory_skill
PLUGINS = [notes_skill, shopping_skill, reminder_skill, web_skill, contacts_skill,
           pins_skill, memory_skill]
```

`wren/core.py`, in the `else:  # chat` branch:

```python
        else:  # chat
            # Only chat turns are observed: "add milk" and "remind me at 6"
            # are commands, not disclosures, and running the extractor on
            # them is a model call spent to be told "nothing here".
            on = registry.is_enabled(memory_skill)
            await channel.send(await asyncio.to_thread(brain.chat, text, history))
            if on:
                memory_skill.observe(user_id, text)
```

with `from .skills import memory_skill` beside core's other skill import.
(Task 7 replaces the `brain.chat` line to pass the retrieved memories.)

- [ ] **Step 5: Run the tests**

Run: `venv/bin/python3 -m pytest -q`
Expected: PASS — the whole suite, since `registry.PLUGINS` and `core` changed.

- [ ] **Step 6: Commit**

```bash
git add wren/skills/memory_skill.py wren/registry.py wren/core.py tests/
git commit -m "feat(memory): background extraction sweeper, fed from the chat path"
```

---

### Task 7: Retrieval and injection

**Files:**
- Modify: `wren/skills/memory_skill.py`, `wren/brain.py`, `wren/core.py`
- Test: `tests/test_memory_plugin.py`, `tests/test_brain.py`

**Interfaces:**
- Consumes: `similarity` (Task 4), `memory_store.all_for` (Task 1).
- Produces: `for_prompt(user_id: int, text: str) -> list[str]`;
  `brain.chat(text, history=None, memories=None)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_for_prompt_returns_nothing_when_the_store_is_empty():
    assert memory_plugin.for_prompt(1, "what should I cook") == []

def test_for_prompt_picks_the_relevant_memory():
    memory.save(1, "person", "sister Kate lives in Denver")
    memory.save(1, "fact", "drives a diesel van")
    assert memory_plugin.for_prompt(1, "remind me to call Kate") == \
        ["sister Kate lives in Denver"]

def test_for_prompt_always_includes_the_core_profile():
    mid = memory.save(1, "preference", "dislikes cilantro")
    for _ in range(2):
        memory.bump(mid)                       # mention_count == 3
    # nothing in common with the message, and it comes back anyway
    assert memory_plugin.for_prompt(1, "how tall is the Eiffel tower") == \
        ["dislikes cilantro"]

def test_for_prompt_caps_at_top_k(monkeypatch):
    monkeypatch.setattr(config, "MEMORY_TOP_K", 2)
    for i in range(5):
        memory.save(1, "fact", f"owns kayak number {i}")
    assert len(memory_plugin.for_prompt(1, "tell me about my kayak")) == 2

def test_for_prompt_drops_irrelevant_memories():
    memory.save(1, "fact", "drives a diesel van")
    assert memory_plugin.for_prompt(1, "what is the capital of Peru") == []
```

```python
def test_chat_injects_memories_into_the_system_prompt():
    client = _client_returning("sure")
    with patch.object(brain, "_get_client", return_value=client):
        brain.chat("what should I cook", memories=["dislikes cilantro"])
    system = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert "dislikes cilantro" in system

def test_chat_without_memories_is_unchanged():
    client = _client_returning("sure")
    with patch.object(brain, "_get_client", return_value=client):
        brain.chat("hello", memories=[])
    system = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert "know about" not in system

def test_detect_intent_never_sees_memories():
    # the 2026-08-18 regression, pinned: nothing may grow this prompt
    client = _client_returning('{"intent": "chat", "content": "hi", "tags": []}')
    with patch.object(brain, "_get_client", return_value=client):
        brain.detect_intent(1, "hi")
    system = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert "know about" not in system
```

- [ ] **Step 2: Run and watch them fail**

Run: `venv/bin/python3 -m pytest -q tests/test_memory_plugin.py tests/test_brain.py -k "for_prompt or inject or memories"`
Expected: FAIL — `AttributeError: ... has no attribute 'for_prompt'`

- [ ] **Step 3: Write retrieval and injection**

In `memory_skill.py`:

```python
def for_prompt(user_id: int, text: str) -> list[str]:
    """The facts worth putting in front of the model for this message.

    Two sources, because lexical matching alone is not enough: anything said
    three times is "core profile" and goes in every turn, and the rest has to
    earn its place by sharing words with what was just asked. Small models get
    distracted by irrelevant context, so the floor matters as much as the cap.
    """
    rows = memories.all_for(user_id)
    core = [r for r in rows if r["mention_count"] >= _CORE_MENTIONS]
    rest = [r for r in rows if r["mention_count"] < _CORE_MENTIONS]
    scored = sorted(((similarity(text, r["fact"]), r) for r in rest),
                    key=lambda pair: pair[0], reverse=True)
    picked = [r for score, r in scored[:config.MEMORY_TOP_K] if score >= _INJECT_FLOOR]
    return [r["fact"] for r in core + picked]
```

In `brain.py`:

```python
def chat(text: str, history: list[dict] | None = None,
         memories: list[str] | None = None) -> str:
    system = ("You are Wren, a personal assistant. Short, structured, ready. No "
              "filler. Answer directly, no reasoning or thinking process shown. "
              f"/no_think Today is {_now()}.")
    if memories:
        # Plain bullets, and an explicit licence to ignore them: without it a
        # small model treats anything in its prompt as something it was just
        # asked about and works the facts into the reply whether they fit or not.
        system += ("\n\nWhat you already know about this person:\n"
                   + "\n".join(f"- {m}" for m in memories)
                   + "\nUse these only if they are relevant. Do not list them back.")
    messages = [{"role": "system", "content": system}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": text})
    return _complete(messages, temperature=0.7, max_tokens=400)
```

In `core.py`'s chat branch, replace the `brain.chat` call from Task 6:

```python
            remembered = memory_skill.for_prompt(user_id, text) if on else []
            await channel.send(await asyncio.to_thread(brain.chat, text, history, remembered))
```

- [ ] **Step 4: Run the tests**

Run: `venv/bin/python3 -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add wren/skills/memory_skill.py wren/brain.py wren/core.py tests/
git commit -m "feat(memory): retrieve relevant facts and inject them into chat"
```

---

### Task 8: Owner controls, config and docs

**Files:**
- Modify: `wren/skills/memory_skill.py`, `wren/config.py`, `.env.example`,
  `README.md`, `wren/communication/chat.html` (SKILL_INFO)
- Test: `tests/test_memory_plugin.py`, `tests/test_config_overrides.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `async handle(intent: str, ctx: Ctx) -> None` for
  `recall_memories` and `forget_memory`; `PROMPT_GUIDELINES`;
  `config.MEMORY_SWEEP_SECONDS`, `config.MEMORY_DEDUP_THRESHOLD`,
  `config.MEMORY_TOP_K` in `config.SETTABLE`.

- [ ] **Step 1: Write the failing tests**

```python
import asyncio
from wren.channel import Ctx, CollectingChannel

def test_recall_lists_what_is_remembered():
    memory.save(1, "preference", "dislikes cilantro")
    ch = CollectingChannel()
    asyncio.run(memory_plugin.handle("recall_memories", Ctx(user_id=1, channel=ch)))
    assert "dislikes cilantro" in ch.sent[0]

def test_recall_says_so_when_there_is_nothing():
    ch = CollectingChannel()
    asyncio.run(memory_plugin.handle("recall_memories", Ctx(user_id=1, channel=ch)))
    assert ch.sent == ["I haven't remembered anything about you yet."]

def test_forget_removes_the_match():
    memory.save(1, "preference", "dislikes cilantro")
    ch = CollectingChannel()
    asyncio.run(memory_plugin.handle("forget_memory",
                                     Ctx(user_id=1, channel=ch, content="cilantro")))
    assert memory.all_for(1) == []

def test_forget_asks_which_one_when_several_match():
    memory.save(1, "person", "sister Kate lives in Denver")
    memory.save(1, "person", "Kate drives a van")
    ch = CollectingChannel()
    asyncio.run(memory_plugin.handle("forget_memory",
                                     Ctx(user_id=1, channel=ch, content="Kate")))
    assert "more than one" in ch.sent[0]
    assert len(memory.all_for(1)) == 2

def test_forget_reports_a_miss():
    ch = CollectingChannel()
    asyncio.run(memory_plugin.handle("forget_memory",
                                     Ctx(user_id=1, channel=ch, content="nothing like this")))
    assert ch.sent == ["Nothing remembered matching that."]

def test_forget_without_content_asks():
    ch = CollectingChannel()
    asyncio.run(memory_plugin.handle("forget_memory", Ctx(user_id=1, channel=ch, content="")))
    assert ch.sent == ["Which one should I forget?"]
```

In `tests/test_config_overrides.py`, matching its existing style:

```python
def test_memory_settings_are_settable():
    assert "MEMORY_SWEEP_SECONDS" in config.SETTABLE
    assert "MEMORY_DEDUP_THRESHOLD" in config.SETTABLE
    assert "MEMORY_TOP_K" in config.SETTABLE

def test_dedup_threshold_rejects_a_value_outside_zero_to_one():
    with pytest.raises(ValueError):
        config.SETTABLE["MEMORY_DEDUP_THRESHOLD"].coerce("1.5")
```

- [ ] **Step 2: Run and watch them fail**

Run: `venv/bin/python3 -m pytest -q tests/test_memory_plugin.py tests/test_config_overrides.py`
Expected: FAIL — no `handle`, no `MEMORY_*` in `SETTABLE`

- [ ] **Step 3: Write the handler and guidelines**

```python
PROMPT_GUIDELINES = """- recall_memories: user asks what Wren knows, remembers or has picked up about them (e.g. "what do you know about me")
- forget_memory: user wants Wren to forget something it remembered about them; content is a short phrase identifying which one, not the full text"""

async def handle(intent: str, ctx: Ctx) -> None:
    if intent == "recall_memories":
        rows = memories.all_for(ctx.user_id)
        if not rows:
            await ctx.channel.send("I haven't remembered anything about you yet.")
            return
        await ctx.channel.send("\n".join(
            f"- [{r['category']}] {r['fact']}"
            + (f" (×{r['mention_count']})" if r["mention_count"] > 1 else "")
            for r in rows))

    elif intent == "forget_memory":
        if not ctx.content.strip():
            await ctx.channel.send("Which one should I forget?")
            return
        needle = ctx.content.strip().lower()
        matches = [r for r in memories.all_for(ctx.user_id) if needle in r["fact"].lower()]
        if not matches:
            await ctx.channel.send("Nothing remembered matching that.")
        elif len(matches) > 1:
            # same shape as cancel_reminder: never guess which one to delete
            listing = "\n".join(f"- {m['fact']}" for m in matches)
            await ctx.channel.send(f"Found more than one match, be more specific.\n{listing}")
        else:
            memories.forget(matches[0]["id"])
            await ctx.channel.send(flourish.flourish(f"Forgotten: {matches[0]['fact']}."))
```

- [ ] **Step 4: Add the config knobs**

In `wren/config.py`, beside `REMINDER_POLL_SECONDS`:

```python
MEMORY_SWEEP_SECONDS = int(os.environ.get("MEMORY_SWEEP_SECONDS", "300"))
MEMORY_DEDUP_THRESHOLD = float(os.environ.get("MEMORY_DEDUP_THRESHOLD", "0.7"))
MEMORY_TOP_K = int(os.environ.get("MEMORY_TOP_K", "5"))


def _coerce_threshold(raw: str) -> float:
    value = float(raw)
    if not 0.0 < value <= 1.0:
        raise ValueError("similarity threshold must be greater than 0 and at most 1")
    return value


def _coerce_top_k(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise ValueError("top-k must be at least 1")
    return value
```

and in `SETTABLE`:

```python
    "MEMORY_SWEEP_SECONDS":   Setting(_coerce_poll_seconds),
    "MEMORY_DEDUP_THRESHOLD": Setting(_coerce_threshold),
    "MEMORY_TOP_K":           Setting(_coerce_top_k),
```

- [ ] **Step 5: Document it**

`.env.example` — beside the other poll knobs:

```
# Memory: how often the background pass reads queued turns (seconds), how
# similar two facts must be to count as the same one (0-1), and how many
# relevant memories to put in front of the model per turn.
MEMORY_SWEEP_SECONDS=300
MEMORY_DEDUP_THRESHOLD=0.7
MEMORY_TOP_K=5
```

`wren/communication/chat.html` — add to `SKILL_INFO`:

```javascript
  memory_skill:   "Memory — facts Wren picks up as you talk. Ask what it knows, or tell it to forget.",
```

`README.md` — a Memory subsection under the skills list, covering: what it
stores, that extraction is a separate background pass, that the owner can list
and delete, and that switching the skill off in the plugins panel stops both
the extraction and the injection.

- [ ] **Step 6: Run the whole suite**

Run: `venv/bin/python3 -m pytest -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add wren tests .env.example README.md
git commit -m "feat(memory): recall and forget intents, settings and docs"
```

---

## After the slice

Manual smoke test, against a scratch database — never `wren.db`:

```bash
export WREN_DB=$(mktemp -d)/scratch.db
venv/bin/python3 - <<'EOF'
from wren.skills import memory_store, memory_skill
memory_store.init_db()
memory_skill.observe(1, "my sister Kate just moved to Denver")
memory_skill._sweep()                      # real model call
print(memory_store.all_for(1))
print(memory_skill.for_prompt(1, "what should I get Kate for her birthday"))
EOF
```

Then watch `journalctl -u wren.service -f | grep memory:` for a day of real
use. The log lines are the tuning data: every gate skip, every candidate, every
dedup with its similarity score. `MEMORY_DEDUP_THRESHOLD` and `MEMORY_TOP_K`
are live-settable, so tuning does not need a restart.

## Deliberately not in this slice

- **A memories card in the web chat.** `recall_memories` sends prose, exactly
  as reminders did before the card slices. Add the card once the taxonomy has
  survived contact with real use.
- **Editing a memory's text.** Forget it and say it again.
- **Embeddings.** The seam is `similarity()`; see the design doc for the exact
  cost of not having them.
- **Memory for anyone but the person who spoke.** Facts file under `owner_id`
  like everything else in Wren.
