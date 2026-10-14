# One Timeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every door into Wren writes to one transcript per person, the web chat becomes that transcript with the briefing on top, and it works on a phone.

**Architecture:** `core.handle_message` records the user turn and wraps the surface's `Channel` so every reply is recorded; the router records every notice it delivers. `messages` gains `channel` and `kind`; a per-person home conversation receives new turns and a join reads old and new in time order. The page drops threads, polls `GET /api/timeline`, draws the briefing card group pinned above the log, lists skill spaces in the sidebar, and manages contacts in a People tab.

**Tech Stack:** Python 3.12, aiohttp, sqlite3, one self-contained `chat.html` (vanilla JS), pytest, node for the page's pure functions, Playwright (system python) for the screenshot smoke.

**Spec:** `docs/superpowers/specs/2026-10-13-one-timeline-design.md` — read it first; the plan argues from it.

## Global Constraints

- CLAUDE.md rules apply verbatim. In particular: a skill never imports a transport; a communication plugin holds no domain logic; runtime-mutable state is `contacts` and `settings` only (so tokens are never stored or returned).
- Never touch the real `wren.db`. `pytest` is safe by construction; any manual run sets `WREN_DB=$(mktemp -d)/scratch.db` first.
- The systemd service runs this working tree in place. Work on a branch in a worktree **outside** the repo directory (`git worktree add ../Wren-timeline -b one-timeline`); python-dotenv searches parent directories, and a worktree under the repo would load the live `.env`. Even outside it, tests load the live `.env`; `conftest.py` resets `ENV_DEFINED`, so do not add tests that depend on a specific `.env`.
- Never run `git stash`. Never stop a scratch Wren with `pkill -f wren.run`: the live service matches that pattern. Stop it by the PID you launched.
- Channel names are exactly `web`, `voice`, `discord`, `telegram`. A NULL `channel` on an old row reads as `web`.
- Copy in the page is for a person new to assistants: plain words, no shell commands outside the owner's panel, no env var names outside the owner's panel.
- Every deliberate shortcut gets a `ponytail:` comment naming its ceiling.
- Each task ends green (`pytest -q`) and committed. Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Review Focus

Inputs the spec implies but no step below would otherwise test. Each has its test pinned to the owning task.

1. Two concurrent first turns create two home conversations for one person; the timeline must still show both sets of rows in order. (Task 1)
2. A `before` cursor that names another user's row must page nothing of theirs and leak nothing. (Task 3)
3. Every surface fails transiently on a notice: nothing is recorded and the exception still propagates so the reminder is retried. (Task 4)
4. `created_at` carries six fractional digits (`2026-10-13T22:40:49.123456+00:00`); the page's date helpers must parse it, not return `Invalid Date`. (Task 5)
5. A dry-run `/message` returns the rows it wrote in `messages` but persists none of them. (Task 3)

---

### Task 1: The store — columns, home, timeline, recent, by_ids, notice

**Files:**
- Modify: `wren/conversations.py`
- Test: `tests/test_conversations.py`

**Interfaces:**
- Produces:
  - `HOME_TITLE = "timeline"`
  - `home(owner_id: int) -> int` — get-or-create the person's home conversation id.
  - `add_message(conversation_id: int, role: str, content: str, card: dict | None = None, *, channel: str | None = None, kind: str | None = None) -> int`
  - `timeline(owner_id: int, *, before: int | None = None, after: int | None = None, limit: int = 50) -> list[dict]` — rows `{id, role, content, created_at, channel, kind, card}`, oldest first. `before`: the newest `limit` rows older than that row. `after`: the oldest `limit` rows newer than that row. A cursor that is not this owner's row returns `[]`.
  - `recent(owner_id: int, *, before_id: int, limit: int = 10) -> list[dict]` — `[{"role", "content"}]`, oldest first, for `brain.detect_intent`.
  - `by_ids(ids: list[int]) -> list[dict]` — timeline-shaped rows for those ids, in `(created_at, id)` order.
  - `notice(owner_id: int, text: str, channel: str) -> int` — an assistant row with `kind="notice"` on the home conversation.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_conversations.py`:

```python
# --------------------------------------------------------------------------
# the timeline (2026-10-13): one thread per person across every conversation
# --------------------------------------------------------------------------

def test_home_is_created_once_per_owner():
    a = conversations.home(ALICE)
    assert conversations.home(ALICE) == a
    assert conversations.home(BOB) != a


def test_home_row_carries_the_sentinel_title():
    h = conversations.home(ALICE)
    with db.conn() as con:
        title, owner = con.execute("SELECT title, owner_id FROM conversations WHERE id=?", (h,)).fetchone()
    assert (title, owner) == (conversations.HOME_TITLE, str(ALICE))


def test_add_message_stores_channel_and_kind(clock):
    h = conversations.home(ALICE)
    conversations.add_message(h, "user", "hi", channel="telegram")
    conversations.add_message(h, "assistant", "⏰ Reminder: bins", channel="telegram", kind="notice")
    rows = conversations.timeline(ALICE)
    assert [(r["role"], r["channel"], r["kind"]) for r in rows] == [
        ("user", "telegram", None), ("assistant", "telegram", "notice")]


def test_timeline_row_shape(clock):
    h = conversations.home(ALICE)
    mid = conversations.add_message(h, "assistant", "list", channel="web",
                                    card={"kind": "shopping", "intent": "recall_shopping", "params": {}})
    (row,) = conversations.timeline(ALICE)
    assert row == {"id": mid, "role": "assistant", "content": "list",
                   "created_at": "2026-08-08T12:00:00.000002+00:00",
                   "channel": "web", "kind": None,
                   "card": {"kind": "shopping", "intent": "recall_shopping", "params": {}}}


def test_timeline_interleaves_old_conversations_by_time(clock):
    """Rows written before today live in several conversations with ids in
    creation order, not time order. The timeline orders by created_at, so a
    row with a higher id but an older timestamp sorts earlier."""
    with db.conn() as con:
        con.execute("INSERT INTO conversations (owner_id, title, created_at, updated_at) VALUES (?,?,?,?)",
                    (str(ALICE), "old chat", "t", "t"))
        old = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    h = conversations.home(ALICE)
    conversations.add_message(h, "user", "newest-in-home", channel="web")       # tick 1
    conversations.add_message(old, "user", "later-in-old", channel="web")       # tick 2
    assert [r["content"] for r in conversations.timeline(ALICE)] == ["newest-in-home", "later-in-old"]


def test_timeline_never_shows_another_owners_rows(clock):
    conversations.add_message(conversations.home(BOB), "user", "bob", channel="web")
    assert conversations.timeline(ALICE) == []


def test_timeline_limit_keeps_the_newest_oldest_first(clock):
    h = conversations.home(ALICE)
    for n in range(5):
        conversations.add_message(h, "user", f"m{n}", channel="web")
    assert [r["content"] for r in conversations.timeline(ALICE, limit=2)] == ["m3", "m4"]


def test_timeline_before_pages_backwards(clock):
    h = conversations.home(ALICE)
    ids = [conversations.add_message(h, "user", f"m{n}", channel="web") for n in range(5)]
    page = conversations.timeline(ALICE, before=ids[3], limit=2)
    assert [r["content"] for r in page] == ["m1", "m2"]


def test_timeline_after_returns_the_oldest_newer_rows(clock):
    h = conversations.home(ALICE)
    ids = [conversations.add_message(h, "user", f"m{n}", channel="web") for n in range(5)]
    page = conversations.timeline(ALICE, after=ids[1], limit=2)
    assert [r["content"] for r in page] == ["m2", "m3"]


def test_timeline_cursor_of_another_owner_returns_nothing(clock):
    bob_id = conversations.add_message(conversations.home(BOB), "user", "bob", channel="web")
    conversations.add_message(conversations.home(ALICE), "user", "alice", channel="web")
    assert conversations.timeline(ALICE, before=bob_id) == []
    assert conversations.timeline(ALICE, after=bob_id) == []


def test_timeline_cursor_that_does_not_exist_returns_nothing(clock):
    conversations.add_message(conversations.home(ALICE), "user", "alice", channel="web")
    assert conversations.timeline(ALICE, before=999) == []


def test_two_home_rows_both_read(clock):
    """Two first turns racing can each create a home. Harmless: the timeline
    joins on owner, so both are read. Pin that so a later 'fix' cannot start
    hiding one of them."""
    with db.conn() as con:
        for _ in range(2):
            con.execute("INSERT INTO conversations (owner_id, title, created_at, updated_at) VALUES (?,?,?,?)",
                        (str(ALICE), conversations.HOME_TITLE, "t", "t"))
        ids = [r[0] for r in con.execute("SELECT id FROM conversations WHERE owner_id=?", (str(ALICE),))]
    conversations.add_message(ids[0], "user", "one", channel="web")
    conversations.add_message(ids[1], "user", "two", channel="web")
    assert [r["content"] for r in conversations.timeline(ALICE)] == ["one", "two"]


def test_recent_is_the_model_shape_and_excludes_the_in_flight_turn(clock):
    h = conversations.home(ALICE)
    conversations.add_message(h, "user", "first", channel="telegram")
    conversations.add_message(h, "assistant", "hello", channel="telegram")
    turn = conversations.add_message(h, "user", "second", channel="web")
    assert conversations.recent(ALICE, before_id=turn) == [
        {"role": "user", "content": "first"}, {"role": "assistant", "content": "hello"}]


def test_recent_keeps_the_newest_n(clock):
    h = conversations.home(ALICE)
    for n in range(30):
        conversations.add_message(h, "user", f"m{n}", channel="web")
    turn = conversations.add_message(h, "user", "now", channel="web")
    got = conversations.recent(ALICE, before_id=turn, limit=10)
    assert [m["content"] for m in got] == [f"m{n}" for n in range(20, 30)]


def test_by_ids_returns_rows_in_time_order(clock):
    h = conversations.home(ALICE)
    a = conversations.add_message(h, "user", "a", channel="web")
    b = conversations.add_message(h, "assistant", "b", channel="web")
    rows = conversations.by_ids([b, a])
    assert [r["id"] for r in rows] == [a, b]
    assert set(rows[0]) == {"id", "role", "content", "created_at", "channel", "kind", "card"}


def test_by_ids_empty():
    assert conversations.by_ids([]) == []


def test_notice_is_an_assistant_row_of_kind_notice(clock):
    nid = conversations.notice(ALICE, "⏰ Reminder: bins", "telegram")
    (row,) = conversations.timeline(ALICE)
    assert row["id"] == nid
    assert (row["role"], row["kind"], row["channel"]) == ("assistant", "notice", "telegram")


def test_init_db_adds_channel_and_kind_to_a_table_that_predates_them(tmp_path, monkeypatch):
    monkeypatch.setenv("WREN_DB", str(tmp_path / "old.db"))
    with db.conn() as con:
        con.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id TEXT NOT NULL, "
                    "title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER NOT NULL, "
                    "role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL, card TEXT)")
        con.execute("INSERT INTO conversations VALUES (1, '1', 'old', 't', 't')")
        con.execute("INSERT INTO messages (conversation_id, role, content, created_at) VALUES (1, 'user', 'hi', 't')")
    conversations.init_db()
    (row,) = conversations.timeline(ALICE)
    assert (row["content"], row["channel"], row["kind"]) == ("hi", None, None)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_conversations.py -q -k "home or timeline or recent or by_ids or notice or channel_and_kind"`
Expected: FAIL with `AttributeError: module 'wren.conversations' has no attribute 'home'` and similar.

- [ ] **Step 3: Implement**

In `wren/conversations.py`:

Replace the `CREATE TABLE IF NOT EXISTS messages` block and the `card` migration with:

```python
        con.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role            TEXT NOT NULL,
                content         TEXT NOT NULL,
                created_at      TEXT NOT NULL,
                card            TEXT,
                channel         TEXT,
                kind            TEXT
            )
        """)
        # Additive, nullable migrations for a database that predates each
        # column: `card` (2026-08-17), then `channel` and `kind` (2026-10-13,
        # the one-timeline change). A fresh install gets all three from the
        # CREATE TABLE above. Two overlapping init_db() calls (systemd
        # restarting Wren while someone runs it by hand) can both see a column
        # missing and both ALTER; sqlite runs DDL in autocommit so the loser
        # raises "duplicate column name". That is harmless -- the column is
        # there either way -- so exactly that error is swallowed.
        cols = {row[1] for row in con.execute("PRAGMA table_info(messages)")}
        for col in ("card", "channel", "kind"):
            if col not in cols:
                try:
                    con.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT")
                except sqlite3.OperationalError as e:
                    if "duplicate column name" not in str(e):
                        raise
```

Add after `TITLE_MAX`:

```python
# The one conversation every new turn for a person goes to. Old conversations
# keep their rows; timeline() reads them all by owner, so nothing is migrated.
HOME_TITLE = "timeline"
```

Add after `create`:

```python
def home(owner_id: int) -> int:
    """This person's home conversation, created on first use.

    ponytail: no lock. Two first turns racing can each insert a home; the
    timeline joins on owner_id so both are read, and later turns land in
    whichever this SELECT finds first. Add a UNIQUE(owner_id, title) partial
    index if a duplicate is ever observed to matter.
    """
    with db.conn() as con:
        row = con.execute(
            "SELECT id FROM conversations WHERE owner_id=? AND title=? ORDER BY id LIMIT 1",
            (str(owner_id), HOME_TITLE),
        ).fetchone()
        if row:
            return row[0]
        ts = _now()
        cur = con.execute(
            "INSERT INTO conversations (owner_id, title, created_at, updated_at) VALUES (?,?,?,?)",
            (str(owner_id), HOME_TITLE, ts, ts),
        )
        return cur.lastrowid
```

Change `add_message` to:

```python
def add_message(conversation_id: int, role: str, content: str,
                card: dict | None = None, *, channel: str | None = None,
                kind: str | None = None) -> int:
    """channel: which door the turn happened on (web, voice, discord,
    telegram). kind: "notice" when Wren spoke first; None for a turn."""
    if role not in ("user", "assistant"):
        raise ValueError(f"role must be 'user' or 'assistant', got {role!r}")
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at, card, channel, kind) "
            "VALUES (?,?,?,?,?,?,?)",
            (conversation_id, role, content, _now(),
             json.dumps(card) if card is not None else None, channel, kind),
        )
        return cur.lastrowid
```

Add at the end of the file:

```python
# ---------------------------------------------------------------------------
# the timeline: every conversation a person owns, read as one thread
# ---------------------------------------------------------------------------

_ROW = "m.id, m.role, m.content, m.created_at, m.channel, m.kind, m.card"


def _shape(rows) -> list[dict]:
    return [
        {"id": r[0], "role": r[1], "content": r[2], "created_at": r[3],
         "channel": r[4], "kind": r[5],
         # stored as JSON text; a NULL (any row before the column) is None
         "card": json.loads(r[6]) if r[6] else None}
        for r in rows
    ]


def _cursor(con, owner_id: int, message_id: int) -> tuple[str, int] | None:
    """(created_at, id) of one of this owner's rows, or None. Scoped to the
    owner so a cursor naming someone else's row pages nothing of theirs."""
    return con.execute(
        "SELECT m.created_at, m.id FROM messages m JOIN conversations c ON c.id=m.conversation_id "
        "WHERE m.id=? AND c.owner_id=?",
        (message_id, str(owner_id)),
    ).fetchone()


def timeline(owner_id: int, *, before: int | None = None, after: int | None = None,
             limit: int = 50) -> list[dict]:
    """Oldest-first rows of this person's whole timeline.

    Ordered by (created_at, id), not id: rows written before today live in
    several conversations whose ids follow creation order, not time order,
    and this join is what lets them stay where they are. Cursors are ids and
    resolve to that row's (created_at, id) so paging follows the same order.
    `before` returns the newest `limit` rows older than the cursor;
    `after` the oldest `limit` rows newer than it (the poll reads forward).
    """
    sql = (f"SELECT {_ROW} FROM messages m JOIN conversations c ON c.id=m.conversation_id "
           "WHERE c.owner_id=?")
    params: list = [str(owner_id)]
    with db.conn() as con:
        for name, op in (("before", "<"), ("after", ">")):
            value = locals()[name]
            if value is None:
                continue
            cur = _cursor(con, owner_id, value)
            if cur is None:
                return []
            sql += f" AND (m.created_at, m.id) {op} (?, ?)"
            params.extend(cur)
        if after is not None:
            sql += " ORDER BY m.created_at ASC, m.id ASC LIMIT ?"
            params.append(limit)
            return _shape(con.execute(sql, params).fetchall())
        sql += " ORDER BY m.created_at DESC, m.id DESC LIMIT ?"
        params.append(limit)
        return _shape(reversed(con.execute(sql, params).fetchall()))


def recent(owner_id: int, *, before_id: int, limit: int = 10) -> list[dict]:
    """The model's history window: newest `limit` turns before the in-flight
    one, in the {role, content} shape brain.detect_intent reads. Cards and
    channels are deliberately absent -- the model gets words only."""
    return [{"role": r["role"], "content": r["content"]}
            for r in timeline(owner_id, before=before_id, limit=limit)]


def by_ids(ids: list[int]) -> list[dict]:
    """Timeline-shaped rows for exactly these ids, in timeline order. What the
    HTTP surface hands the page after a turn, so the page draws what was
    stored and not a second rendering of the same reply."""
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    with db.conn() as con:
        rows = con.execute(
            f"SELECT {_ROW} FROM messages m WHERE m.id IN ({marks}) ORDER BY m.created_at, m.id",
            list(ids),
        ).fetchall()
    return _shape(rows)


def notice(owner_id: int, text: str, channel: str) -> int:
    """Wren spoke first: a reminder, the briefing, a watcher alert. Written by
    the router after a surface delivered it, naming that surface."""
    return add_message(home(owner_id), "assistant", text, channel=channel, kind="notice")
```

Note: `locals()[name]` inside the loop is fragile; write it plainly instead:

```python
        for value, op in ((before, "<"), (after, ">")):
            if value is None:
                continue
            cur = _cursor(con, owner_id, value)
            if cur is None:
                return []
            sql += f" AND (m.created_at, m.id) {op} (?, ?)"
            params.extend(cur)
```

Use the plain form; delete the `locals()` version.

- [ ] **Step 4: Run the whole store test file**

Run: `pytest tests/test_conversations.py -q`
Expected: all pass, including the pre-existing `messages`/`card` tests (unchanged in this task).

- [ ] **Step 5: Commit**

```bash
git add wren/conversations.py tests/test_conversations.py
git commit -m "feat(conversations): channel and kind columns, per-person home, timeline reads

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Core records the transcript; channels are named; history leaves the protocol

**Files:**
- Modify: `wren/channel.py`
- Modify: `wren/core.py`
- Modify: `wren/communication/discord_plugin.py`, `wren/communication/discord_utils.py`
- Modify: `wren/communication/telegram_plugin.py`
- Modify: `wren/communication/http_plugin.py`
- Modify: `wren/communication/webchat.py` (the `WebChannel` class and `post_message` only)
- Test: `tests/test_core.py`, `tests/test_channel.py`, `tests/test_discord_surface.py`, `tests/test_telegram_plugin.py`, `tests/test_webchat.py`, `tests/test_http_surface.py`

**Interfaces:**
- Consumes: Task 1's `home`, `add_message(..., channel=, kind=)`, `recent`.
- Produces:
  - `Channel.NAME: str` on the protocol; `CollectingChannel(name: str = "web")` sets `self.NAME`; `DiscordChannel.NAME = "discord"`; `TelegramChannel.NAME = "telegram"`.
  - `core.handle_message(...) -> list[int]`: ids of every row written this turn (user first), `[]` when the gate refused.
  - `core._Recording(inner, home)` with `.written: list[int]`, `.inner`, `.NAME`.
  - `history()` is gone from every channel.

- [ ] **Step 1: Write the failing core tests**

In `tests/test_core.py`, add `from wren import conversations` to the imports and `conversations.init_db()` as the first line of the `authorized` fixture body. Then append:

```python
# --- the transcript (2026-10-13): core records every turn ----------------

def test_turn_is_recorded_with_the_channel_name(detected):
    detected(intent="help")
    ch = CollectingChannel(name="telegram")
    written = asyncio.run(core.handle_message(OWNER, "what can you do", ch))
    rows = conversations.timeline(OWNER)
    assert [(r["role"], r["channel"], r["kind"]) for r in rows] == [
        ("user", "telegram", None), ("assistant", "telegram", None)]
    assert rows[0]["content"] == "what can you do"
    assert rows[1]["content"] == ch.sent[0]
    assert written == [rows[0]["id"], rows[1]["id"]]


def test_refused_user_writes_nothing_and_returns_no_ids(detected):
    detected(intent="help")
    assert asyncio.run(core.handle_message(999, "hi", CollectingChannel())) == []
    assert conversations.timeline(999) == []


def test_history_excludes_the_in_flight_turn_and_crosses_channels(detected):
    calls = detected(intent="help")
    asyncio.run(core.handle_message(OWNER, "first", CollectingChannel(name="telegram")))
    asyncio.run(core.handle_message(OWNER, "second", CollectingChannel(name="web")))
    history = calls[-1][2]
    assert history[0] == {"role": "user", "content": "first"}
    assert history[1]["role"] == "assistant"
    assert "second" not in [h["content"] for h in history]
    assert all(set(h) == {"role", "content"} for h in history)


def test_card_reply_stores_its_ref_not_its_rows(detected):
    detected(intent="recall_shopping", content="")
    shopping_store.init_db()
    shopping_store.add("milk", "owner")
    asyncio.run(core.handle_message(OWNER, "what's on the list", CollectingChannel()))
    row = conversations.timeline(OWNER)[-1]
    assert row["card"] == {"kind": "shopping", "intent": "recall_shopping",
                           "params": {"content": "", "list": "shopping"}}
    assert "milk" in row["content"]            # the prose fallback is the content


def test_bare_photo_records_a_placeholder_and_the_question(detected):
    from wren.channel import Inbound
    asyncio.run(core.handle_message(
        OWNER, "", CollectingChannel(), files=[Inbound("receipt.jpg", "image/jpeg", b"x")]))
    rows = conversations.timeline(OWNER)
    assert [r["content"] for r in rows] == ["(sent receipt.jpg)", "What is this?"]


def test_failed_turn_keeps_what_was_typed(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("llm down")
    monkeypatch.setattr(brain, "detect_intent", boom)
    asyncio.run(core.handle_message(OWNER, "remind me later", CollectingChannel()))
    rows = conversations.timeline(OWNER)
    assert [r["content"] for r in rows] == ["remind me later", "Something went wrong, try again."]


def test_a_reply_that_fails_to_send_is_not_recorded(detected):
    detected(intent="help")

    class Broken(CollectingChannel):
        async def send(self, text):
            raise RuntimeError("rate limited")

    asyncio.run(core.handle_message(OWNER, "help", Broken()))
    rows = conversations.timeline(OWNER)
    assert [r["role"] for r in rows] == ["user"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_core.py -q -k "recorded or refused_user_writes or crosses_channels or stores_its_ref or placeholder_and or keeps_what_was_typed or fails_to_send"`
Expected: FAIL (`TypeError: CollectingChannel() got an unexpected keyword argument 'name'`, then assertion failures).

- [ ] **Step 3: Channel protocol and CollectingChannel**

In `wren/channel.py`, change the protocol to:

```python
class Channel(Protocol):
    """How a surface talks back. Implemented by each surface; capability
    plugins only ever see this, never a discord.Message.

    NAME is which door this is -- "web", "voice", "discord" or "telegram".
    core records it with every turn so the timeline can say where a message
    came from; it is the only thing a skill could learn about the transport,
    and none reads it."""

    NAME: str

    async def send(self, text: str) -> None: ...

    async def send_file(self, data: bytes, filename: str) -> None: ...

    async def ack(self, state: str) -> None:
        """state is "seen" | "done" | "error". Discord draws these as
        reactions; surfaces with no such concept no-op."""
        ...

    async def send_card(self, kind: str, data: dict, text: str,
                        *, intent: str = "", params: dict | None = None) -> None:
        """(docstring unchanged)"""
        ...
```

Delete `history` from the protocol. In `CollectingChannel`:

```python
    def __init__(self, name: str = "web") -> None:
        self.NAME = name
        self.sent: list[str] = []
        self.files: list[tuple[bytes, str]] = []
        self.acks: list[str] = []
        self.cards: list[dict] = []
```

and delete its `history` method. In `tests/test_channel.py` delete `test_history_returns_none` and `test_history_returns_none_with_explicit_limit`; add:

```python
def test_default_name_is_web():
    assert CollectingChannel().NAME == "web"


def test_name_is_settable():
    assert CollectingChannel(name="voice").NAME == "voice"
```

- [ ] **Step 4: Core**

In `wren/core.py`, add `from . import conversations` to the imports. Add this class before `handle_message`:

```python
class _Recording:
    """The surface's Channel, with every reply written to the person's
    timeline as it goes out. Skills still see a Channel and nothing under
    wren/skills/ knows this exists. A reply that fails to send is not
    recorded: send first, then write, so the timeline never claims a
    delivery that raised."""

    def __init__(self, inner: Channel, home: int) -> None:
        self.inner = inner
        self.NAME = inner.NAME
        self._home = home
        self.written: list[int] = []

    async def send(self, text: str) -> None:
        await self.inner.send(text)
        self.written.append(conversations.add_message(self._home, "assistant", text, channel=self.NAME))

    async def send_file(self, data: bytes, filename: str) -> None:
        # files are not persisted (see WebChannel's old send_file): the reply
        # text that accompanies them is the row
        await self.inner.send_file(data, filename)

    async def send_card(self, kind: str, data: dict, text: str,
                        *, intent: str = "", params: dict | None = None) -> None:
        await self.inner.send_card(kind, data, text, intent=intent, params=params)
        # the ref, never the rows: a card is live and re-reads itself from
        # intent+params (skill-cards design); `data` would be stale by tomorrow
        self.written.append(conversations.add_message(
            self._home, "assistant", text, channel=self.NAME,
            card={"kind": kind, "intent": intent, "params": params or {}}))

    async def ack(self, state: str) -> None:
        await self.inner.ack(state)
```

Split `handle_message`. The new outer function:

```python
async def handle_message(user_id: int, text: str, channel: Channel, *, source: str = "text",
                         files: list[Inbound] | None = None) -> list[int]:
    """Transport-free dispatch. Surfaces authenticate the caller, build a
    Channel, and call this. Nothing below here knows what a Discord is.

    Returns the ids of every timeline row this turn wrote, the user's own
    turn first, so a surface that wants to show exactly what was stored (the
    web page) can read them back. Empty when the gate refused.

    `source` is "text" or "voice" -- voice gets a yes/no confirmation before
    running anything destructive, since transcription mis-hears; text is
    unaffected and always acts immediately.

    `files` are attachments that arrived with the words. With a caption they
    become a note; without one Wren asks what the photo is and the next
    message answers."""
    # authorization gate — surfaces do authn (who are you), this does authz.
    # Kept here rather than per-surface so a new surface cannot forget it.
    if user_id not in config.id_to_name():
        return []

    text = (text or "").strip()
    files = list(files or [])
    if not text and not files:
        return []

    # The transcript is recorded here for the same reason the gate is: one
    # place, so no surface can forget. The user's turn goes in before anything
    # else happens, so a failed LLM call keeps what was typed; the reply side
    # is the _Recording wrapper. A bare photo leaves a placeholder so the turn
    # is visible in the timeline.
    home = conversations.home(user_id)
    stored = text or "(sent " + ", ".join(f.filename for f in files) + ")"
    turn_id = conversations.add_message(home, "user", stored, channel=channel.NAME)
    recording = _Recording(channel, home)
    await _handle(user_id, text, recording, source=source, files=files, before_id=turn_id)
    return [turn_id, *recording.written]


async def _handle(user_id: int, text: str, channel: Channel, *, source: str,
                  files: list[Inbound], before_id: int) -> None:
    await channel.ack("seen")
    ... the rest of the old body, unchanged, from the `pending = _take_pending(user_id)` line down ...
```

Inside `_handle`, replace

```python
        history = await channel.history(limit=10)
```

with

```python
        # From the store, not the channel: the timeline is every door's
        # history at once, so "what did I just say" on Telegram sees what was
        # typed in the web chat. before_id excludes this very turn, which the
        # model already receives as `text`.
        history = conversations.recent(user_id, before_id=before_id, limit=10)
```

Everything else in the old body stays as it was (the `text = (text or "").strip()` and `files = list(files or [])` lines and the two early returns move to the outer function and are deleted from `_handle`).

- [ ] **Step 5: Discord, Telegram, http, webchat**

`wren/communication/discord_plugin.py`: add `NAME = "discord"` as the first line of `DiscordChannel`'s body (before `__init__`), delete the `history` method. In `wren/communication/discord_utils.py` delete `history_to_messages`. In `tests/test_discord_surface.py` delete the four `test_history_to_messages_*` tests and any helper only they used.

`wren/communication/telegram_plugin.py`: add `NAME = "telegram"` as the first line of `TelegramChannel`'s body, delete the `history` method and its docstring. In `tests/test_telegram_plugin.py` delete `test_channel_history_is_none`.

`wren/communication/http_plugin.py`: `_dispatch` gains `name: str = "web"` and builds `CollectingChannel(name=name)`; `voice()` passes `name="voice"`. (The `messages` payload comes in Task 3; here only the name.)

`wren/communication/webchat.py`: delete the `WebChannel` class and `HISTORY_LIMIT`. In `post_message`, replace everything from `stored = text.strip() or ...` through `conversations.touch(convo_id, user_id)` with:

```python
        channel = CollectingChannel()
        await core.handle_message(user_id, text, channel, files=files)
```

(the route itself is deleted in Task 3; this keeps the module importable and the surviving tests honest meanwhile).

- [ ] **Step 6: Delete the tests that pinned WebChannel persistence**

Run: `pytest tests/test_webchat.py -q`

Every failure is a test of behaviour that no longer exists (a `WebChannel`, rows written under a conversation by the web route, auto-titles, the per-conversation history window). Delete exactly these tests from `tests/test_webchat.py` — do not patch them to pass: `test_get_returns_messages`, `test_message_persists_both_roles`, `test_send_file_does_not_write_a_message_row`, `test_webchannel_send_file_collects_without_persisting`, `test_webchannel_send_persists_an_assistant_row`, `test_message_updates_updated_at`, `test_first_message_auto_titles_a_new_chat`, `test_second_message_does_not_retitle`, `test_renamed_conversation_is_not_auto_titled`, `test_history_excludes_the_in_flight_message`, `test_history_is_scoped_to_one_conversation`, `test_history_is_capped_and_keeps_the_newest`, `test_conversation_renamed_before_first_message_keeps_its_name`, `test_established_conversation_renamed_to_new_chat_is_not_retitled`, `test_a_card_survives_a_reload_but_its_rows_do_not`, `test_the_model_does_not_see_cards`, `test_post_message_accepts_multipart_and_stores_the_caption`, `test_post_message_with_a_file_and_no_caption_stores_a_placeholder`, `test_delete_removes_conversation_and_its_messages` (its message half). If a test not on this list fails, read it: if it asserts a row under a conversation id, delete it; otherwise fix the code.

- [ ] **Step 7: Run the whole suite**

Run: `pytest -q`
Expected: all pass. `grep -rn "\.history(" wren/ tests/` returns nothing.

- [ ] **Step 8: Commit**

```bash
git add -A wren/ tests/
git commit -m "feat(core): record every turn on the person's timeline; channels declare NAME; history leaves the protocol

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: The timeline API; the conversation routes go

**Files:**
- Modify: `wren/communication/http_plugin.py`
- Modify: `wren/communication/webchat.py`
- Modify: `wren/conversations.py` (deletions)
- Test: `tests/test_webchat.py`, `tests/test_http_surface.py`, `tests/test_conversations.py`

**Interfaces:**
- Consumes: Task 1 `timeline`, `by_ids`; Task 2 `handle_message -> list[int]`.
- Produces:
  - `GET /api/timeline?limit=50`, `?before=<id>&limit=`, `?after=<id>&limit=` → JSON list of timeline rows. `limit` clamped to 1..200, default 50. `before` with `after` → 400. Non-integer or out-of-range cursor → 400.
  - `POST /message` and `POST /voice` payloads gain `"messages": [rows]` and `"cards": [...]`.
  - Deleted: every `/api/conversations*` route; `conversations.create, list_for, get, rename, delete, touch, title_from, messages, TITLE_MAX`.

- [ ] **Step 1: Write the failing route tests**

In `tests/test_webchat.py`, delete the `new_convo` helper and every remaining test that calls it or hits `/api/conversations` (`test_create_then_list`, `test_list_is_empty_for_a_fresh_user`, `test_get_on_a_brand_new_conversation_has_no_messages`, `test_patch_renames`, `test_patch_with_blank_title_is_400`, `test_patch_with_non_json_body_is_400`, `test_delete_removes_conversation_and_its_messages`, `test_non_integer_id_is_404_not_500`, `test_trailing_slash_id_is_404_not_500`, `test_unknown_id_is_404`, `test_non_integer_id_on_message_route_is_404_not_500`, `test_other_users_conversation_is_404_on_*` (four), `test_list_does_not_leak_other_users_conversations`, `test_message_dispatches_and_returns_replies`, `test_message_returns_files_base64`, `test_missing_text_is_400`, `test_blank_text_is_400`, `test_non_string_text_is_400`, `test_non_json_body_is_400`, `test_non_whitelisted_user_is_refused_and_writes_nothing`, `test_oversized_conversation_id_is_404_not_500`, `test_non_object_json_body_is_400_not_500`, `test_non_string_title_is_400_not_500`, `test_a_card_comes_back_in_the_message_response`, `test_dispatch_writes_nothing_to_any_conversation`, `test_reopen_does_not_remount_cards_without_a_refresh_intent`, `test_post_message_reply_files_carry_mime`). The parametrized `test_api_route_without_token_is_401` / `test_api_route_with_wrong_token_is_401` lists: replace the conversation paths with `("get", "/api/timeline")` and keep the dispatch/me/plugins ones.

Then add:

```python
# ── the timeline ───────────────────────────────────────────────────────────

def _seed(user, *rows):
    h = conversations.home(user)
    return [conversations.add_message(h, role, text, channel=ch) for role, text, ch in rows]


def test_timeline_is_per_user_oldest_first_with_the_row_shape():
    _seed(USER_A, ("user", "hi", "web"), ("assistant", "hello", "web"))
    _seed(USER_B, ("user", "bob's", "telegram"))
    status, rows = call("get", "/api/timeline", token=TOKEN_A)
    assert status == 200
    assert [r["content"] for r in rows] == ["hi", "hello"]
    assert set(rows[0]) == {"id", "role", "content", "created_at", "channel", "kind", "card"}
    assert rows[0]["channel"] == "web" and rows[0]["kind"] is None and rows[0]["card"] is None


def test_timeline_after_returns_only_newer_rows():
    ids = _seed(USER_A, ("user", "a", "web"), ("assistant", "b", "web"), ("user", "c", "web"))
    status, rows = call("get", f"/api/timeline?after={ids[0]}", token=TOKEN_A)
    assert [r["content"] for r in rows] == ["b", "c"]


def test_timeline_before_pages_backwards():
    ids = _seed(USER_A, *[("user", f"m{n}", "web") for n in range(5)])
    status, rows = call("get", f"/api/timeline?before={ids[3]}&limit=2", token=TOKEN_A)
    assert [r["content"] for r in rows] == ["m1", "m2"]


def test_timeline_cursor_naming_another_users_row_pages_nothing():
    (bob,) = _seed(USER_B, ("user", "secret", "web"))
    _seed(USER_A, ("user", "mine", "web"))
    for q in (f"before={bob}", f"after={bob}"):
        status, rows = call("get", f"/api/timeline?{q}", token=TOKEN_A)
        assert (status, rows) == (200, [])


@pytest.mark.parametrize("query", ["before=abc", "after=1.5", "limit=x", "before=1&after=2",
                                   "before=-1", "before=99999999999999999999"])
def test_timeline_rejects_bad_parameters(query):
    status, body = call("get", f"/api/timeline?{query}", token=TOKEN_A)
    assert status == 400 and "error" in body


def test_timeline_limit_is_clamped_not_refused():
    _seed(USER_A, *[("user", f"m{n}", "web") for n in range(3)])
    status, rows = call("get", "/api/timeline?limit=999", token=TOKEN_A)
    assert status == 200 and len(rows) == 3
    status, rows = call("get", "/api/timeline?limit=0", token=TOKEN_A)
    assert status == 200 and len(rows) == 1


def test_timeline_requires_a_whitelisted_user(monkeypatch):
    monkeypatch.setattr(config, "id_to_name", lambda: {USER_A: "ann"})
    status, body = call("get", "/api/timeline", token=TOKEN_B)
    assert status == 403


def test_conversation_routes_are_gone():
    for method, path in (("get", "/api/conversations"), ("post", "/api/conversations"),
                         ("get", "/api/conversations/1"), ("post", "/api/conversations/1/message")):
        status, _ = call(method, path, token=TOKEN_A, expect_json=False)
        assert status in (404, 405), (method, path)
```

In `tests/test_http_surface.py` add (the `_replies` helper there patches `core.handle_message` and must now return a list — change it to `return []` at the end of `fake`):

```python
def test_message_payload_carries_the_rows_written(monkeypatch):
    from wren import brain, conversations
    conversations.init_db()
    monkeypatch.setattr(brain, "detect_intent", lambda *a, **k: {"intent": "help", "content": "", "tags": []})
    status, body = call("post", "/message", token="good-token", json={"text": "help"})
    assert status == 200
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert body["messages"][0]["content"] == "help"
    assert body["messages"][0]["channel"] == "web"
    assert body["messages"][1]["content"] == body["replies"][0]
    assert body["cards"] == []
    assert [r["id"] for r in conversations.timeline(1)] == [m["id"] for m in body["messages"]]


def test_voice_rows_are_recorded_as_voice(monkeypatch):
    from wren import brain, conversations, stt
    conversations.init_db()
    monkeypatch.setattr(stt, "transcribe", lambda audio: "help")
    monkeypatch.setattr(brain, "detect_intent", lambda *a, **k: {"intent": "help", "content": "", "tags": []})
    status, body = call("post", "/voice", token="good-token", data=b"RIFF")
    assert status == 200
    assert body["messages"][0]["channel"] == "voice"


def test_dry_run_returns_the_rows_but_persists_none(monkeypatch):
    from wren import brain, conversations
    conversations.init_db()
    monkeypatch.setattr(brain, "detect_intent", lambda *a, **k: {"intent": "help", "content": "", "tags": []})
    status, body = call("post", "/message?dry_run=1", token="good-token", json={"text": "help"})
    assert status == 200 and body["dry_run"] is True
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert conversations.timeline(1) == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_webchat.py tests/test_http_surface.py -q`
Expected: the new tests FAIL (404 on `/api/timeline`, `KeyError: 'messages'`).

- [ ] **Step 3: http payload**

In `wren/communication/http_plugin.py` add `from .. import conversations` and change `_payload` and `_dispatch`:

```python
def _payload(channel: CollectingChannel, written: list[int], **extra) -> dict:
    return {
        "replies": channel.sent,
        "files": file_entries(channel.files),
        "cards": channel.cards,
        # exactly the rows this turn stored, so the page draws what the
        # timeline will show on its next poll and nothing twice
        "messages": conversations.by_ids(written),
        **extra,
    }


async def _dispatch(user_id: int, text: str, *, name: str = "web", source: str = "text",
                    dry: bool = False, files: list[Inbound] | None = None, **extra) -> web.Response:
    """dry: run against a discarded snapshot of the database (see db.dry_run)
    and echo `dry_run: true` so a caller can never mistake which one it got.
    For testing a live install without touching its data. The payload is
    built inside the snapshot: the rows it reports exist only there."""
    channel = CollectingChannel(name=name)
    if dry:
        extra["dry_run"] = True
        with db.dry_run():
            written = await core.handle_message(user_id, text, channel, source=source, files=files)
            payload = _payload(channel, written, **extra)
    else:
        written = await core.handle_message(user_id, text, channel, source=source, files=files)
        payload = _payload(channel, written, **extra)
    if not channel.sent and not channel.files:
        if user_id not in config.id_to_name():
            return web.json_response({"error": "user not whitelisted"}, status=403)
    return web.json_response(payload)
```

- [ ] **Step 4: Routes**

In `wren/communication/webchat.py`: delete `_conversation_or_404`, `list_conversations`, `create_conversation`, `get_conversation`, `rename_conversation`, `delete_conversation`, `post_message`, their five `add_*` route lines, the `from .http_plugin import file_entries, read_message` import if nothing else uses it, and `_id_from` (keep `_SQLITE_INT_MAX`). Add inside `register_routes`:

```python
    def _query_int(request, key, default=None):
        raw = request.query.get(key)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError:
            raise web.HTTPBadRequest(text=f'{{"error": "{key} must be an integer"}}',
                                     content_type="application/json")
        if not (0 <= value <= _SQLITE_INT_MAX):
            # sqlite binds 64-bit signed ints; an unbounded Python int would
            # blow up at bind time as a 500 instead of an honest 400
            raise web.HTTPBadRequest(text=f'{{"error": "{key} is out of range"}}',
                                     content_type="application/json")
        return value

    async def timeline(request):
        """The person's whole thread, paged. No conversation ids anywhere:
        a token maps to a person and a person has one timeline."""
        user_id = _auth(request)
        before = _query_int(request, "before")
        after = _query_int(request, "after")
        if before is not None and after is not None:
            return web.json_response({"error": "use before or after, not both"}, status=400)
        # clamped, not refused: a page asking for 999 wants "a lot", and 200
        # is the most one response should carry
        limit = max(1, min(_query_int(request, "limit", 50), 200))
        return web.json_response(
            conversations.timeline(user_id, before=before, after=after, limit=limit))
```

and `app.router.add_get("/api/timeline", timeline)`.

- [ ] **Step 5: Delete the dead store functions**

In `wren/conversations.py` delete `TITLE_MAX`, `title_from`, `create`, `list_for`, `get`, `rename`, `delete`, `touch`, `messages`. In `tests/test_conversations.py` delete every test from `test_create_returns_an_id` through `test_messages_before_id_and_limit_combined`, the `title_from` tests, the `test_list_for_never_shows…` / `test_get_by_another_owner…` / `test_rename_by_another_owner…` / `test_delete_by_another_owner…` / the three `test_touch_*` tests, and rewrite `test_add_message_stores_and_returns_a_card`, `test_a_message_without_a_card_reads_back_as_none`, `test_add_message_returns_an_id`, `test_add_message_rejects_an_unknown_role` and `test_init_db_adds_the_card_column_to_a_table_that_predates_it` to use `conversations.home(ALICE)` for the conversation id and `conversations.timeline(ALICE)` to read back. Keep `test_init_db_is_idempotent`. Delete `_all_message_rows` if nothing uses it.

- [ ] **Step 6: Run the suite**

Run: `pytest -q`
Expected: all pass. `grep -rn "list_for\|title_from\|conversations.create\|conversations.messages\|conversations.get(\|conversations.rename\|conversations.delete\|conversations.touch" wren/ tests/` returns nothing.

- [ ] **Step 7: Commit**

```bash
git add -A wren/ tests/
git commit -m "feat(web): GET /api/timeline; /message returns the rows it wrote; conversation routes removed

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: The router records notices; http is a passive door

**Files:**
- Modify: `wren/router.py`
- Modify: `wren/communication/http_plugin.py` (`notify`, new `PASSIVE`, `CHANNEL`)
- Modify: `wren/config.py` (`_coerce_notify_via`)
- Modify: `wren/run.py` (`_warn_if_notifications_go_nowhere`)
- Test: `tests/test_router.py`, `tests/test_http_surface.py`, `tests/test_run_boot_checks.py`, `tests/test_config_overrides.py` (if it covers `NOTIFY_VIA` coercion; check with `grep -n notify_via tests/`)

**Interfaces:**
- Consumes: Task 1 `conversations.notice`.
- Produces:
  - A surface module may declare `PASSIVE = True` (delivery means "it will be on the timeline") and `CHANNEL = "<timeline channel name>"` (defaults to its registered name). `http_plugin` declares `PASSIVE = True`, `CHANNEL = "web"`, `CAN_NOTIFY = False` (unchanged).
  - `router.notify` / `_notify_any` call `conversations.notice(user_id, text, channel)` after a surface returns True. `_notify_any` falls back to a passive surface when no `CAN_NOTIFY` surface delivered and none raised.
  - `config._coerce_notify_via` accepts a `PASSIVE` surface.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_router.py` (add `from wren import conversations` and `conversations.init_db()` inside `clean_registry` after `contacts.init_db()`):

```python
# --- notices on the timeline (2026-10-13) --------------------------------

def _notices(user_id):
    return [(r["content"], r["channel"], r["kind"]) for r in conversations.timeline(user_id)]


def test_a_delivered_notice_is_recorded_with_its_surface(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_VIA", "telegram")
    router.register("telegram", FakeSurface(True))
    assert asyncio.run(router.notify(1, "⏰ Reminder: bins")) is True
    assert _notices(1) == [("⏰ Reminder: bins", "telegram", "notice")]


def test_a_permanent_failure_records_nothing(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_VIA", "telegram")
    router.register("telegram", FakeSurface(False))
    assert asyncio.run(router.notify(1, "x")) is False
    assert _notices(1) == []


def test_a_transient_failure_raises_and_records_nothing(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_VIA", "telegram")
    router.register("telegram", FakeSurface(raises=RuntimeError("503")))
    with pytest.raises(RuntimeError):
        asyncio.run(router.notify(1, "x"))
    assert _notices(1) == []


def test_any_records_under_the_surface_that_delivered(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_VIA", "any")
    monkeypatch.setattr(config, "COMMUNICATION_PLUGINS", ["discord", "telegram"])
    router.register("discord", FakeSurface(False))
    router.register("telegram", FakeSurface(True))
    assert asyncio.run(router.notify(1, "x")) is True
    assert _notices(1) == [("x", "telegram", "notice")]


class PassiveSurface(FakeSurface):
    PASSIVE = True
    CAN_NOTIFY = False
    CHANNEL = "web"


def test_any_falls_back_to_a_passive_surface_when_nothing_active_delivered(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_VIA", "any")
    monkeypatch.setattr(config, "COMMUNICATION_PLUGINS", ["http", "telegram"])
    router.register("http", PassiveSurface(True))
    router.register("telegram", FakeSurface(False))
    assert asyncio.run(router.notify(1, "x")) is True
    assert _notices(1) == [("x", "web", "notice")]


def test_any_prefers_an_active_surface_over_the_passive_one(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_VIA", "any")
    monkeypatch.setattr(config, "COMMUNICATION_PLUGINS", ["http", "telegram"])
    passive = PassiveSurface(True)
    router.register("http", passive)
    router.register("telegram", FakeSurface(True))
    asyncio.run(router.notify(1, "x"))
    assert passive.calls == []
    assert _notices(1) == [("x", "telegram", "notice")]


def test_any_does_not_fall_back_to_passive_after_a_transient_failure(monkeypatch):
    """A raise means retry next poll; landing it on the timeline now AND
    delivering it to the phone later would say it twice."""
    monkeypatch.setattr(config, "NOTIFY_VIA", "any")
    monkeypatch.setattr(config, "COMMUNICATION_PLUGINS", ["http", "telegram"])
    router.register("http", PassiveSurface(True))
    router.register("telegram", FakeSurface(raises=RuntimeError("503")))
    with pytest.raises(RuntimeError):
        asyncio.run(router.notify(1, "x"))
    assert _notices(1) == []


def test_explicit_http_records_as_web(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_VIA", "any")
    router.register("http", PassiveSurface(True))
    assert asyncio.run(router.notify(1, "x", via="http")) is True
    assert _notices(1) == [("x", "web", "notice")]


def test_dry_run_records_nothing(monkeypatch):
    from wren import db
    monkeypatch.setattr(config, "NOTIFY_VIA", "telegram")
    router.register("telegram", FakeSurface(True))
    with db.dry_run():
        assert asyncio.run(router.notify(1, "x")) is True
    assert _notices(1) == []
```

In `tests/test_http_surface.py` replace `test_http_notify_is_send_only` with:

```python
def test_http_notify_is_passive_and_answers_true():
    # nothing is sent: the router records the notice and the page polls it
    assert http_surface.PASSIVE is True
    assert http_surface.CAN_NOTIFY is False
    assert http_surface.CHANNEL == "web"
    assert asyncio.run(http_surface.notify(1, "reminder")) is True
```

In `tests/test_run_boot_checks.py` change `test_notify_any_warns_when_nothing_loaded_can_push` to assert `"only appear in the web chat" in caplog.text` and add:

```python
def test_notify_via_http_warns_it_is_web_only(monkeypatch, caplog):
    from wren.communication import http_plugin
    monkeypatch.setattr(config, "NOTIFY_VIA", "http")
    with caplog.at_level(logging.WARNING):
        run._warn_if_notifications_go_nowhere({"http": http_plugin})
    assert "only appear in the web chat" in caplog.text


def test_notify_any_warns_discarded_when_nothing_at_all_can_deliver(monkeypatch, caplog):
    from wren.communication import gmail_plugin
    monkeypatch.setattr(config, "NOTIFY_VIA", "any")
    with caplog.at_level(logging.WARNING):
        run._warn_if_notifications_go_nowhere({"gmail": gmail_plugin})
    assert "DISCARDED" in caplog.text
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_router.py tests/test_http_surface.py tests/test_run_boot_checks.py -q`
Expected: the new tests FAIL.

- [ ] **Step 3: Router**

In `wren/router.py` add `from . import conversations`. In `notify`, replace the final `return await surface.notify(user_id, text)` with:

```python
    ok = await surface.notify(user_id, text)
    if ok:
        # Recorded only on True: False is "unreachable, consumed" and a raise
        # is "retry next poll" -- neither happened yet as far as the person
        # is concerned, and the timeline must never say Wren told them
        # something it did not.
        conversations.notice(user_id, text, _channel_of(name, surface))
    return ok
```

Add near the top, after `_surfaces`:

```python
def _channel_of(name: str, surface) -> str:
    """What the timeline calls a delivery on this surface. http records as
    "web" because that is the door the person reads it through."""
    return getattr(surface, "CHANNEL", name)
```

In `_notify_any`, change the loop and the tail to:

```python
    for name in candidates:
        try:
            if await _surfaces[name].notify(user_id, text):
                conversations.notice(user_id, text, _channel_of(name, _surfaces[name]))
                return True
        except Exception as e:
            logging.warning(f"notify {user_id} on {name} failed, trying the next surface: {e}")
            transient = e
    if transient is not None:
        raise transient
    # Nothing that can push reached them and nothing is worth retrying. A
    # passive surface (the web chat) can still "deliver" by letting the
    # router put it on the timeline, where the page shows it on its next
    # poll. Last, never first: a phone buzz beats a row the person has to
    # open a browser to see. Not after a transient failure either -- that
    # message is coming back next poll, and would then be said twice.
    for name in config.COMMUNICATION_PLUGINS:
        surface = _surfaces.get(name)
        if surface is not None and getattr(surface, "PASSIVE", False):
            if await surface.notify(user_id, text):
                conversations.notice(user_id, text, _channel_of(name, surface))
                return True
    if not candidates:
        logging.warning(f"Cannot notify {user_id}: no running surface can push "
                        f"(have: {registered() or 'none'})")
    return False
```

The existing dry-run early return at the top of `notify` already precedes all of this; leave it.

- [ ] **Step 4: http plugin, config, boot warning**

`wren/communication/http_plugin.py`, replacing the `ROLE`/`PLUGIN_NAME`/`CAN_NOTIFY` lines and `notify`:

```python
ROLE = "chat"
PLUGIN_NAME = "HTTP / Web Chat"
CAN_NOTIFY = False   # cannot push to a phone; see PASSIVE
PASSIVE = True       # a notice is "delivered" by landing on the timeline the page polls
CHANNEL = "web"      # what the timeline calls this door


async def notify(user_id: int, text: str) -> bool:
    # Passive: nothing is sent. The router records every notice it delivers
    # on the person's timeline, and the web page polls that, so answering
    # True means "it will be there when they open the page". The router only
    # lands here when no surface that can push did, or when NOTIFY_VIA names
    # http on purpose. The body is deliberately not logged: reminders and
    # person-to-person messages are private.
    return True
```

`wren/config.py` `_coerce_notify_via`: change the `CAN_NOTIFY` check to

```python
    if not getattr(surface, "CAN_NOTIFY", True) and not getattr(surface, "PASSIVE", False):
        raise ValueError(f"'{name}' cannot deliver unprompted messages")
```

`wren/run.py` `_warn_if_notifications_go_nowhere`:

```python
    target = config.NOTIFY_VIA
    web_only = ("Reminders and watcher alerts will only appear in the web chat, "
                "not on a phone. Add discord or telegram to COMMUNICATION_PLUGINS for a push.")
    if target == config.NOTIFY_ANY:
        if not any(getattr(p, "CAN_NOTIFY", True) for p in loaded.values()):
            if any(getattr(p, "PASSIVE", False) for p in loaded.values()):
                logging.warning(f"No running communication plugin can push ({', '.join(loaded)}). {web_only}")
            else:
                logging.warning(
                    f"No running communication plugin can deliver unprompted messages "
                    f"({', '.join(loaded) or 'none'}). Reminders and watcher alerts will be DISCARDED. "
                    f"Add one that can push, e.g. discord or telegram, to COMMUNICATION_PLUGINS."
                )
        return
    surface = loaded.get(target)
    if surface is None:
        logging.warning(
            f"NOTIFY_VIA='{target}' is not in COMMUNICATION_PLUGINS ({', '.join(config.COMMUNICATION_PLUGINS)}). "
            f"Reminders and watcher alerts will NOT be delivered.")
    elif getattr(surface, "PASSIVE", False):
        logging.warning(f"NOTIFY_VIA='{target}'. {web_only}")
    elif not getattr(surface, "CAN_NOTIFY", True):
        logging.warning(
            f"NOTIFY_VIA='{target}' cannot deliver unprompted messages. Reminders and watcher "
            f"alerts will be DISCARDED. Set NOTIFY_VIA to a communication plugin that can push.")
```

- [ ] **Step 5: Run the suite**

Run: `pytest -q`
Expected: all pass. If a test in `tests/test_config_overrides.py` or `tests/test_settings.py` asserted that `NOTIFY_VIA=http` is refused, change it to assert it is accepted.

- [ ] **Step 6: Commit**

```bash
git add -A wren/ tests/
git commit -m "feat(router): record every delivered notice on the timeline; http is a passive door

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: The page becomes the timeline

**Files:**
- Modify: `wren/communication/chat.html` (substantial rewrite of the sidebar, log, composer and their JS; the plugins panel JS stays)
- Test: `tests/test_chat_renderer.py`, `tests/test_webchat.py` (the page-shell string tests)

**Interfaces:**
- Consumes: `GET /api/timeline`, `POST /message` (`messages`, `cards`, `files`, `replies`), `/api/dispatch`, `/api/me`.
- Produces, as pure functions placed **between** `function esc(s)` and `async function api` so the node harness extracts them:
  - `parseIso(iso) -> Date` (trims fractional seconds to 3 digits)
  - `sameDay(isoA, isoB) -> bool`
  - `dayLabel(iso, now = new Date()) -> "Today" | "Yesterday" | "Tuesday 13 October"`
  - `turnLabel(row) -> string` (`""` for a plain web turn, `"from Telegram"`, `"from Discord"`, `"by voice"`, `"sent to Telegram"` for a notice; a notice with channel `web` or `null` → `"sent here"`)
  - `takeToken(href) -> {token: string|null, clean: string}` (pulls `token` out of a URL's query and returns the URL without it)
- DOM ids later tasks rely on: `#nav-today` (sidebar entry), `#spaces` (empty container under a `Spaces` label, hidden when empty), `#today` (empty container above `#log`, hidden; Task 7 fills it), `#reads` (chip row above the composer), `#attach` (pending attachment chips, was `#chips`), `#menu` (top-bar button, hidden until Task 8), `#modeltext` now inside the panel header.

- [ ] **Step 1: Write the failing node tests**

Append to `tests/test_chat_renderer.py`:

```python
# ── timeline helpers (pure, extracted the same way as md()) ────────────────

def run_js(expr: str, tmp_path) -> str:
    js = tmp_path / "h.mjs"
    js.write_text(_renderer_js() + f"\nprocess.stdout.write(String({expr}));", encoding="utf-8")
    out = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_parse_iso_accepts_microseconds(tmp_path):
    # sqlite rows carry six fractional digits; Date() must not come back invalid
    assert run_js('parseIso("2026-10-13T22:40:49.123456+00:00").getTime()', tmp_path) != "NaN"
    assert run_js('parseIso("2026-10-13T22:40:49.123456+00:00").getUTCHours()', tmp_path) == "22"


def test_same_day_compares_local_dates(tmp_path):
    assert run_js('sameDay("2026-10-13T10:00:00+00:00", "2026-10-13T11:00:00.000001+00:00")', tmp_path) == "true"
    assert run_js('sameDay("2026-10-13T10:00:00+00:00", "2026-10-14T10:00:00+00:00")', tmp_path) == "false"


def test_day_label_today_yesterday_and_a_date(tmp_path):
    now = 'new Date(2026, 9, 13, 12)'
    assert run_js(f'dayLabel("2026-10-13T01:00:00.000000", {now})', tmp_path) == "Today"
    assert run_js(f'dayLabel("2026-10-12T23:00:00.000000", {now})', tmp_path) == "Yesterday"
    assert "October" in run_js(f'dayLabel("2026-10-01T09:00:00.000000", {now})', tmp_path)


@pytest.mark.parametrize("row, label", [
    ('{"channel": "web", "kind": null}', ""),
    ('{"channel": null, "kind": null}', ""),
    ('{"channel": "telegram", "kind": null}', "from Telegram"),
    ('{"channel": "discord", "kind": null}', "from Discord"),
    ('{"channel": "voice", "kind": null}', "by voice"),
    ('{"channel": "telegram", "kind": "notice"}', "sent to Telegram"),
    ('{"channel": "web", "kind": "notice"}', "sent here"),
    ('{"channel": null, "kind": "notice"}', "sent here"),
])
def test_turn_label(row, label, tmp_path):
    assert run_js(f"turnLabel({row})", tmp_path) == label


def test_take_token_strips_only_the_token(tmp_path):
    out = run_js('JSON.stringify(takeToken("https://w/?token=abc&x=1#top"))', tmp_path)
    assert out == '{"token":"abc","clean":"https://w/?x=1#top"}'
    out = run_js('JSON.stringify(takeToken("https://w/"))', tmp_path)
    assert out == '{"token":null,"clean":"https://w/"}'
```

The existing page-shell tests in `tests/test_webchat.py` that assert on strings in the page (`test_log_is_the_containing_block_for_the_speaker_labels`, `test_page_has_the_attachment_plumbing`, `test_a_drop_outside_the_composer_does_not_navigate_the_tab`, `test_api_helper_does_not_force_json_content_type_on_form_bodies`, `test_reply_images_render_inline_and_pdfs_as_links`, `test_notes_card_rows_show_a_thumbnail_for_attached_images`, `test_user_bubble_thumbnails_revoke_their_object_urls`, `test_every_render_card_call_passes_the_reply_files`) must keep passing; read each before editing the page so the strings they look for survive (rename `#chips` to `#attach` in the test too). Add to `tests/test_webchat.py`:

```python
def test_page_polls_the_timeline_and_has_no_conversation_list():
    _, _, html = asyncio.run(_page_request())
    assert "/api/timeline" in html
    assert "/api/conversations" not in html
    assert 'id="new"' not in html and 'id="convos"' not in html
    assert "prompt(" not in html and "confirm(" not in html
    for anchor in ('id="nav-today"', 'id="spaces"', 'id="today"', 'id="reads"',
                   'id="attach"', 'id="menu"', 'id="modeltext"'):
        assert anchor in html, anchor


def test_page_posts_to_the_shared_message_route():
    _, _, html = asyncio.run(_page_request())
    assert 'api("/message"' in html


def test_page_font_stack_is_system_first():
    _, _, html = asyncio.run(_page_request())
    assert "font: 15px/1.6 system-ui" in html


def test_sign_in_copy_names_no_env_var_or_command():
    _, _, html = asyncio.run(_page_request())
    login = html[html.index('id="login"'):html.index('id="app"')]
    assert "WREN_TOKENS" not in login and "manage.sh" not in login
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_chat_renderer.py tests/test_webchat.py -q`
Expected: the new tests FAIL (`parseIso is not defined`, missing ids).

- [ ] **Step 3: Rewrite the page**

Read the whole of `wren/communication/chat.html` first. Keep: the `<style>` token blocks, the `#wren-mark` symbol, the login card markup (new copy), the composer, the attachment plumbing, the card renderers (`shoppingCard`, `notesCard`, `ideasCard`, `remindersCard`, `locateCard`, `cardShell`, `cardStatus`, `renderCard`, `mountStoredCard`, `dispatch`), `md`/`esc`, `fileLink`, `api`, `signOut`, the plugins panel markup and all of `SKILL_INFO`/`SECRET_INFO`/`SETTING_META`/`settingRow`/`loadPlugins`/`showTab`. Remove: `#new`, `#convos`, `.convo*` CSS, `#rail`/`#edge` peek (Task 8 replaces the collapse with the drawer; keep `#rail` markup and `setRail` for now), `loadConvos`, `renameConvo`, `delConvo`, `openConvo`, `newConvo`, `STARTERS`, `PILL_READS`, `showList`, `#pills`, the `active` variable, `#modeltext` from the top bar.

Markup changes:

```html
<div id="login">
  ... brandmark unchanged ...
  <p>Paste the sign-in token you were given. Whoever set up this Wren can
     make one for you, and a link they send you signs you in on its own.</p>
  <input id="token" type="password" placeholder="sign-in token" autocomplete="current-password">
  <button id="signin">Connect</button>
  <div class="err" id="loginerr"></div>
</div>

<div id="app">
  <div id="scrim" hidden></div>
  <aside>
    <header> ...sidemark + <b>Wren</b> + #rail unchanged... </header>
    <nav id="sidenav">
      <button class="nav on" id="nav-today">Today</button>
      <div class="lab" id="spaces-label" hidden>Spaces</div>
      <div id="spaces" hidden></div>
    </nav>
    <footer id="side-foot"> ...unchanged... </footer>
  </aside>
  <main>
    <header id="topbar">
      <button id="menu" type="button" hidden aria-label="Menu">☰</button>
      <span id="ctitle">Today</span>
      <span id="cdate"></span>
    </header>
    <div id="today" hidden></div>
    <div id="log"><div id="inner"></div></div>
    <footer>
      <div id="reads"></div>
      <div id="attach" hidden></div>
      <div id="composer"> ...unchanged... </div>
    </footer>
  </main>
</div>
```

Panel header: `<header><b>Plugins &amp; Settings</b><span id="modeltext" class="pstate"></span><button id="pclose">✕</button></header>`.

CSS to add (replace the `.convo`/`#new`/`#pills`/`.pill` rules):

```css
#sidenav { flex: 1; overflow-y: auto; padding: 2px 8px; display: flex; flex-direction: column; gap: 2px; }
.nav { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border-radius: 8px;
       border: 0; text-align: left; font-size: 14px; width: 100%; }
.nav:hover { background: var(--wash); }
.nav.on { background: var(--wash); font-weight: 600; }
.lab { font-size: 11px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); padding: 12px 10px 4px; }
#cdate { color: var(--muted); font-size: 13px; margin-left: auto; white-space: nowrap; }
#menu { border: 0; padding: 4px 8px; border-radius: 6px; font-size: 18px; line-height: 1; }
/* day separators: a hairline each side of a small caps label */
.day { display: flex; align-items: center; gap: 10px; margin: 18px 0 14px; font-size: 11.5px;
       letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }
.day::before, .day::after { content: ""; flex: 1; border-top: 1px solid var(--line); }
/* where a turn came from, under its bubble */
.via { font-size: 11.5px; color: var(--muted); margin-top: 3px; }
.msg.user .via { text-align: right; }
/* Wren spoke first: quiet, dashed, with a bell */
.msg.notice .body { background: var(--panel); border: 1px dashed var(--line-2); border-radius: 10px;
                    padding: 7px 11px; color: var(--muted); max-width: 82%; }
.msg.notice .body b { color: var(--text); font-weight: 600; }
/* the read chips above the composer, one per skill that can answer */
#reads { max-width: 760px; margin: 0 auto 6px; display: flex; gap: 6px; overflow-x: auto; padding-bottom: 2px; }
#reads:empty { display: none; }
.read { flex: none; border: 1px solid var(--line); background: var(--panel); border-radius: 999px;
        padding: 5px 12px; font-size: 13px; color: var(--muted); }
.read:hover { color: var(--accent); border-color: var(--clay); }
#attach { max-width: 760px; margin: 0 auto 6px; display: flex; flex-wrap: wrap; gap: 6px; }
#attach[hidden] { display: none; }
/* tick boxes on list cards: the brochure's checkbox, not a delete cross */
.card .ctick { width: 16px; height: 16px; border: 1.5px solid var(--line-2); border-radius: 4px;
               background: transparent; padding: 0; flex: none; cursor: pointer; }
.card .ctick:hover { border-color: var(--ok); }
.card li.cgone .ctick { background: var(--ok); border-color: var(--ok); }
/* .env-locked settings read as information, not as a warning */
.pslabel .plock { color: var(--muted); }
```

Body font: `font: 15px/1.6 system-ui, -apple-system, "Segoe UI", Roboto, "Inter", "Source Sans 3", sans-serif;`. Delete the `@media (max-width: 640px)` block (Task 8 writes the real one).

JS. Place these pure helpers immediately after `md()` and before `async function api`:

```js
/* ── timeline helpers ──────────────────────────────────────────────────── */
function parseIso(iso) {
  // sqlite rows carry six fractional digits; some engines reject more than three
  return new Date(String(iso).replace(/(\.\d{3})\d+/, "$1"));
}
function sameDay(a, b) {
  const d = parseIso(a), e = parseIso(b);
  return d.getFullYear() === e.getFullYear() && d.getMonth() === e.getMonth() && d.getDate() === e.getDate();
}
function dayLabel(iso, now = new Date()) {
  const d = parseIso(iso);
  const day = x => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(now) - day(d)) / 86400000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
}
const DOOR = { telegram: "Telegram", discord: "Discord", voice: "voice" };
function turnLabel(row) {
  if (row.kind === "notice") return "sent to " + (row.channel === "web" || !row.channel ? "here" : DOOR[row.channel] || row.channel);
  if (row.channel === "voice") return "by voice";
  return DOOR[row.channel] ? "from " + DOOR[row.channel] : "";
}
function takeToken(href) {
  const url = new URL(href);
  const token = url.searchParams.get("token");
  url.searchParams.delete("token");
  return { token, clean: url.toString() };
}
```

Invite link, at the top of the script after `let token = ...`:

```js
// A link with ?token= signs this browser in and then forgets it was ever in
// the address bar, so it is not left in history or pasted on by accident.
{
  const t = takeToken(location.href);
  if (t.token) {
    token = t.token;
    try { localStorage.setItem("wren_token", token); } catch (e) {}
    history.replaceState(null, "", t.clean);
  }
}
```

The timeline state and rendering:

```js
/* ── the timeline ───────────────────────────────────────────────────────── */
let rows = [];             // every row on screen, oldest first
let lastId = 0;            // poll cursor: the newest id drawn
let oldestId = null;       // page-back cursor
let exhausted = false;     // no older page exists
const PAGE = 50, POLL_MS = 30000;

function rowNode(r, prev, live) {
  // `live` is the card data from this very response, matched in order; a
  // row drawn from the timeline later re-fetches through mountStoredCard
  const frag = document.createDocumentFragment();
  if (!prev || !sameDay(prev.created_at, r.created_at)) {
    const d = document.createElement("div"); d.className = "day"; d.textContent = dayLabel(r.created_at);
    frag.append(d);
  }
  const role = r.kind === "notice" ? "notice" : r.role;
  const html = r.role === "user" ? esc(r.content) : md(r.content);
  const b = bubble(role, html, { detach: true });
  const label = turnLabel(r);
  if (label) { const v = document.createElement("div"); v.className = "via"; v.textContent = label; b.querySelector(".body").after(v); }
  if (r.card) {
    const card = live && live.cards.length ? live.cards.shift() : null;
    if (card) renderCard(card, b.querySelector(".body"), live.files);
    else if (r.card.intent) mountStoredCard(r.card, b.querySelector(".body"));
  }
  frag.append(b);
  return frag;
}
```

`bubble(role, html, {detach})` is the existing `bubble` with one change: when `opts.detach` is true it returns the element without appending or scrolling. The `notice` role draws the Wren avatar and a `<b>` prefix is not needed; `md()` keeps the ⏰ text as it is.

```js
function redraw() {
  const inner = $("#inner");
  inner.innerHTML = "";
  if (!rows.length) { showEmpty(); return; }
  let prev = null;
  for (const r of rows) { inner.append(rowNode(r, prev)); prev = r; }
}
function append(newRows, live) {
  const inner = $("#inner");
  const gone = $("#landing"); if (gone) gone.remove();
  const atBottom = $("#log").scrollTop + $("#log").clientHeight >= $("#log").scrollHeight - 40;
  let prev = rows[rows.length - 1] || null;
  for (const r of newRows) { inner.append(rowNode(r, prev, live)); rows.push(r); prev = r; }
  if (newRows.length) lastId = Math.max(lastId, newRows[newRows.length - 1].id);
  if (oldestId === null && rows.length) oldestId = rows[0].id;
  if (atBottom || live) $("#log").scrollTop = $("#log").scrollHeight;
}
async function loadTimeline() {
  const page = await api("/api/timeline?limit=" + PAGE);
  rows = []; lastId = 0; oldestId = null; exhausted = page.length < PAGE;
  append(page);
  if (!rows.length) showEmpty();
}
async function loadOlder() {
  if (exhausted || oldestId === null || loadOlder.busy) return;
  loadOlder.busy = true;
  try {
    const page = await api("/api/timeline?before=" + oldestId + "&limit=" + PAGE);
    exhausted = page.length < PAGE;
    if (!page.length) return;
    const log = $("#log"), before = log.scrollHeight;
    rows = page.concat(rows); oldestId = rows[0].id;
    redraw();
    log.scrollTop = log.scrollHeight - before;     // keep what was on screen where it was
  } finally { loadOlder.busy = false; }
}
async function poll() {
  if (!token || document.hidden) return;
  try { append(await api("/api/timeline?after=" + lastId)); }
  catch (e) { /* a blip; the next tick tries again, and a 401 has already signed out */ }
}
$("#log").addEventListener("scroll", () => { if ($("#log").scrollTop < 40) loadOlder(); });
setInterval(poll, POLL_MS);
document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
```

`send()` changes: post to `/message` (JSON or multipart exactly as now); draw an optimistic user bubble and the thinking dots; on success remove both and `append(data.messages, { cards: data.cards.slice(), files: data.files })`; files from `data.files` that are not card thumbnails are still drawn with `fileLink` as now; `loadConvos()` call removed; on failure the optimistic bubble stays with the error bubble beneath as now. Empty-timeline landing: `showEmpty()` keeps the greeting and mark, drops the pills.

Read chips:

```js
// One chip per skill that can answer a read without the model. Click draws
// the card as an unstored bubble, same contract as a card button.
const READS = [
  ["briefing_skill", "Catch me up", "briefing"],
  ["shopping_skill", "Shopping", "recall_shopping"],
  ["reminder_skill", "Reminders", "recall_reminders"],
  ["notes_skill", "Notes", "recall_notes"],
  ["notes_skill", "Ideas", "recall_ideas"],
];
function drawReads(skills) {
  const box = $("#reads"); box.innerHTML = "";
  for (const [skill, label, intent] of READS) {
    if (!skills.includes(skill)) continue;
    const b = document.createElement("button"); b.className = "read"; b.textContent = label;
    b.onclick = () => showRead(intent);
    box.append(b);
  }
}
async function showRead(intent) {
  const gone = $("#landing"); if (gone) gone.remove();
  try {
    const r = await dispatch(intent, {});
    const card = (r.cards || [])[0];
    const b = bubble("assistant", md((card && card.text) || (r.replies || []).join("\n")));
    if (card) renderCard(card, b.querySelector(".body"), r.files);
  } catch (e) {
    bubble("assistant", "<em>" + esc(e.message || "Something went wrong.") + "</em>");
  }
}
```

Cards: in `shoppingCard` the row control becomes `<button class="ctick" data-item="…" title="got it" aria-label="got it"></button>` placed before `.citem`; the click handler is the existing remove-with-undo flow unchanged (`.cgone` + the `←` undo button replaces the tick as now). In `remindersCard` the control becomes `<button class="ctick" data-item="…" title="done" aria-label="done"></button>` before the time, dispatching `cancel_reminder` as now. `ideasCard` keeps `.cx`.

`start()`:

```js
async function start() {
  $("#login").style.display = "none";
  $("#app").classList.add("on");
  if (localStorage.getItem("wren_rail")) setRail(true);
  $("#cdate").textContent = new Date().toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
  $("#gear").hidden = true;
  api("/api/plugins").then(() => { $("#gear").hidden = false; }).catch(() => {});
  const me = api("/api/me");
  me.then(m => { $("#whoname").textContent = m.name || "Signed in"; drawReads(m.skills || []); showModel(m.model); }).catch(() => {});
  await loadTimeline();
  $("#text").focus();
}
```

`showModel` writes into the panel's `#modeltext`. `#nav-today` click: `redraw()` and scroll to the bottom (Task 10 uses it to leave a space).

The `plock` note text becomes `"Set in .env on the host. Change it there and restart Wren."` and keeps class `plock` (now muted by the CSS above).

- [ ] **Step 4: Run the node and page tests**

Run: `pytest tests/test_chat_renderer.py tests/test_webchat.py -q`
Expected: all pass.

- [ ] **Step 5: Look at it once**

Boot a scratch instance and open it in the browser or Playwright (the script in Task 8 does this; until then, by hand):

```bash
S=$(mktemp -d); export WREN_DB=$S/scratch.db COMMUNICATION_PLUGINS=http WREN_TOKENS=shot:1 \
  WREN_OWNER_ID=1 OWNER_NAME=Erin WREN_HTTP_PORT=18787 DISCORD_TOKEN= TELEGRAM_TOKEN=
venv/bin/python3 -m wren.run > $S/wren.log 2>&1 & WREN_PID=$!
# seed: curl -s -H "Authorization: Bearer shot" -H 'Content-Type: application/json' \
#   -X POST localhost:18787/api/dispatch -d '{"intent":"add_shopping_item","content":"milk"}'
# open http://127.0.0.1:18787/?token=shot
kill $WREN_PID
```

Check: the token link signs in and the address bar is clean; the timeline shows the seeded notice if you add one with `python3 -c` against the scratch DB (`conversations.notice(1, "⏰ Reminder: bins", "telegram")` with `WREN_DB` set); the chips draw cards; a tick on the shopping card strikes the row with an undo; the Plugins panel still opens and the model name is in its header. Fix what you see, once.

- [ ] **Step 6: Commit**

```bash
git add wren/communication/chat.html tests/test_chat_renderer.py tests/test_webchat.py
git commit -m "feat(web): the page is the timeline: one thread, day separators, notices, read chips, invite links

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: The briefing gathers, renders, and sends a card

**Files:**
- Modify: `wren/skills/briefing_skill.py`
- Test: `tests/test_briefing_skill.py`

**Interfaces:**
- Produces:
  - `gather(user_id: int) -> dict` with keys `greeting: str`, `weather: str | None`, `calendar: list[str] | str | None` (a `str` is an error line), `reminders: list[{"local": "HH:MM", "content": str}] | None`, `email: list[{"sender", "subject"}]`, `shopping: int | None`. `None` = off or unconfigured; `[]`/`0` = configured and empty.
  - `render(data: dict) -> str` — byte-identical to today's `compose` output for the same inputs.
  - `compose(user_id) -> str` = `render(gather(user_id))` (the morning push keeps calling it).
  - `handle` sends `send_card("briefing", data, render(data), intent="briefing", params={"content": ""})`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_briefing_skill.py`:

```python
# --- gather / render (2026-10-13): the briefing as data for the card ------

def test_gather_shape_with_everything_configured():
    reminders.save(1, "call the vet", "2026-08-26T17:00:00+00:00")
    shopping.add("milk", "owner")
    with patch.object(weather_skill, "summary", return_value="72°F and clear."), \
         patch.object(calendar_skill, "events_between", return_value=[_event("Standup", 9)]):
        data = briefing_skill.gather(1)
    assert data["greeting"].startswith("Good morning. Wed Aug 26.")
    assert data["weather"] == "72°F and clear."
    assert data["calendar"] == calendar_skill.day_lines([_event("Standup", 9)], NOW.date())
    assert data["reminders"] == [{"local": "17:00", "content": "call the vet"}]
    assert data["shopping"] == 1
    assert data["email"] == []


def test_gather_marks_unconfigured_sections_none(monkeypatch):
    monkeypatch.setattr(config, "CALENDAR_URLS", [])
    monkeypatch.setattr(config, "WEATHER_LAT", None)
    data = briefing_skill.gather(1)
    assert data["weather"] is None and data["calendar"] is None
    assert data["reminders"] == [] and data["shopping"] == 0


def test_render_matches_compose_for_the_same_inputs():
    reminders.save(1, "call the vet", "2026-08-26T17:00:00+00:00")
    shopping.add("milk", "owner")
    with patch.object(weather_skill, "summary", return_value="72°F and clear."), \
         patch.object(calendar_skill, "events_between", return_value=[_event("Standup", 9)]):
        assert briefing_skill.render(briefing_skill.gather(1)) == briefing_skill.compose(1)


def test_render_of_an_empty_day_reads_as_a_sentence():
    with patch.object(weather_skill, "summary", return_value="72°F and clear."), \
         patch.object(calendar_skill, "events_between", return_value=[]):
        text = briefing_skill.render(briefing_skill.gather(1))
    assert text.endswith("Nothing on the calendar, no reminders, list is empty.")


def test_calendar_error_is_a_string_and_renders_as_the_old_line():
    with patch.object(weather_skill, "summary", return_value="x"), \
         patch.object(calendar_skill, "events_between", side_effect=calendar_skill.FeedError("down")):
        data = briefing_skill.gather(1)
    assert data["calendar"] == "couldn't reach the feed."
    assert "Calendar: couldn't reach the feed." in briefing_skill.render(data)


def test_handle_sends_a_briefing_card_whose_text_is_the_prose():
    shopping.add("milk", "owner")
    ch = CollectingChannel()
    with patch.object(weather_skill, "summary", return_value="x"), \
         patch.object(calendar_skill, "events_between", return_value=[]):
        asyncio.run(briefing_skill.handle("briefing", Ctx(user_id=1, channel=ch)))
    (card,) = ch.cards
    assert card["kind"] == "briefing"
    assert card["intent"] == "briefing" and card["params"] == {"content": ""}
    assert card["data"]["shopping"] == 1
    assert card["text"] == ch.sent[0]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_briefing_skill.py -q -k "gather or render or briefing_card"`
Expected: FAIL (`no attribute 'gather'`).

- [ ] **Step 3: Implement**

Replace `compose` and `handle` in `wren/skills/briefing_skill.py` with:

```python
def gather(user_id: int) -> dict:
    """Everything the briefing knows, as data. Blocking (feeds, HTTP); call
    under to_thread. None means a section is off or unconfigured; an empty
    list or 0 means configured and empty. A calendar that failed is a string
    so the card can say so in the section's own place.

    Composed, never generated: nothing here touches brain. A briefing that
    invents a meeting is worse than no briefing, and a deterministic one is
    free to send every morning. Each source is wrapped on its own so one dead
    feed costs one section, not the whole message.
    """
    from .. import registry   # local: registry imports this module

    tz = ZoneInfo(config.TIMEZONE)
    now = _now().astimezone(tz)
    today = now.date()
    data: dict = {"greeting": f"{_greeting(now)} {today:%a %b} {today.day}.",
                  "weather": None, "calendar": None, "reminders": None,
                  "email": [], "shopping": None}

    if registry.is_enabled(weather_skill) and weather_skill.is_active():
        try:
            data["weather"] = weather_skill.summary(0)
        except Exception as e:
            logging.warning(f"briefing: weather failed: {type(e).__name__}: {e}")
            data["weather"] = "couldn't reach the weather service."

    if registry.is_enabled(calendar_skill) and calendar_skill.is_active():
        try:
            data["calendar"] = calendar_skill.day_lines(calendar_skill.events_between(today, 1), today)
        except calendar_skill.FeedError as e:
            logging.warning(f"briefing: calendar failed: {e}")
            data["calendar"] = "couldn't reach the feed."

    if registry.is_enabled(reminder_skill):
        due = []
        for r in reminders.pending(user_id):
            local = datetime.fromisoformat(r["fire_at"]).astimezone(tz)
            if local.date() == today:
                due.append({"local": f"{local:%H:%M}", "content": r["content"]})
        data["reminders"] = due

    if config.EMAIL_DIGEST:
        alias = config.id_to_name().get(user_id)
        mail = gmail_state.recent(alias) if alias else []
        data["email"] = [{"sender": m["sender"], "subject": m["subject"]} for m in mail]

    if registry.is_enabled(shopping_skill):
        data["shopping"] = len(shopping.active_items())
    return data


def render(data: dict) -> str:
    """The day in one message, from gather()'s data. The same lines the
    briefing has always sent; the card is the other rendering of this dict."""
    lines = [data["greeting"]]
    empties: list[str] = []
    if data["weather"] is not None:
        lines.append("Weather: " + data["weather"])
    cal = data["calendar"]
    if isinstance(cal, str):
        lines.append("Calendar: " + cal)
    elif cal:
        lines.append("Calendar:")
        lines.extend(cal)
    elif cal is not None:
        empties.append("nothing on the calendar")
    due = data["reminders"]
    if due:
        lines.append("Reminders today:")
        lines.extend(f"  {r['local']}  {r['content']}" for r in due)
    elif due is not None:
        empties.append("no reminders")
    mail = data["email"]
    if mail:
        lines.append("Email:")
        lines.extend(f"  {m['sender']} — {m['subject']}" for m in mail[:_DIGEST_CAP])
        if len(mail) > _DIGEST_CAP:
            lines.append(f"  …and {len(mail) - _DIGEST_CAP} more.")
    n = data["shopping"]
    if n:
        lines.append(f"Shopping list: {n} item{'' if n == 1 else 's'}.")
    elif n is not None:
        empties.append("list is empty")
    # Only when every data section came up empty: a briefing that lists what
    # it does not have reads as an apology; one that says nothing reads as broken.
    if empties and not any(l.startswith(("Calendar", "Reminders", "Shopping")) for l in lines):
        sentence = ", ".join(empties)
        lines.append(sentence[:1].upper() + sentence[1:] + ".")
    return "\n".join(lines)


def compose(user_id: int) -> str:
    return render(gather(user_id))


async def handle(intent: str, ctx: Ctx) -> None:
    data = await asyncio.to_thread(gather, ctx.user_id)
    # one send: the card is the message, the prose its fallback on Discord
    # and Telegram, and what the timeline stores
    await ctx.channel.send_card("briefing", data, render(data),
                                intent="briefing", params={"content": ""})
```

- [ ] **Step 4: Run the suite**

Run: `pytest tests/test_briefing_skill.py -q && pytest -q`
Expected: all pass, including every pre-existing briefing test unchanged.

- [ ] **Step 5: Commit**

```bash
git add wren/skills/briefing_skill.py tests/test_briefing_skill.py
git commit -m "feat(briefing): gather the day as data and send it as a card; prose unchanged

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: The Today panel

**Files:**
- Modify: `wren/communication/chat.html`
- Test: `tests/test_chat_renderer.py`, `tests/test_webchat.py`

**Interfaces:**
- Consumes: the `briefing` card (`data` shape from Task 6) via `/api/dispatch`.
- Produces: pure `briefingSections(data) -> [{key, title, lines: [string], count?: number}]` placed with the other helpers (node-testable); `briefingCard(data, mount)` in `CARDS`; `#today` filled on start and on each poll tick; `localStorage("wren_today")` remembers collapsed.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_chat_renderer.py`:

```python
def test_briefing_sections_skip_unconfigured_and_keep_empty(tmp_path):
    data = ('{"greeting":"Good morning. Tue Oct 13.","weather":null,'
            '"calendar":[],"reminders":[{"local":"18:00","content":"bins"}],"email":[],"shopping":0}')
    out = run_js(f"JSON.stringify(briefingSections({data}).map(s => [s.key, s.lines.length]))", tmp_path)
    assert out == '[["calendar",0],["reminders",1],["shopping",0]]'


def test_briefing_sections_render_a_calendar_error_as_its_line(tmp_path):
    data = '{"greeting":"g","weather":"x","calendar":"couldn\'t reach the feed.","reminders":null,"email":[],"shopping":null}'
    out = run_js(f"JSON.stringify(briefingSections({data}))", tmp_path)
    assert '"key":"calendar"' in out and "reach the feed" in out
    assert '"key":"reminders"' not in out and '"key":"shopping"' not in out
```

Add to `tests/test_webchat.py`:

```python
def test_page_mounts_the_today_panel_from_the_briefing():
    _, _, html = asyncio.run(_page_request())
    assert "briefing: briefingCard" in html
    assert 'dispatch("briefing"' in html
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_chat_renderer.py tests/test_webchat.py -q -k "briefing or today_panel"`
Expected: FAIL.

- [ ] **Step 3: Implement**

Helper, next to the others:

```js
// The briefing's sections, in the order the panel draws them. null means
// the skill is off or unconfigured and the section is simply absent; an
// empty list is a configured section with nothing in it, which still shows.
function briefingSections(d) {
  const out = [];
  if (d.weather !== null && d.weather !== undefined) out.push({ key: "weather", title: "Weather", lines: [d.weather] });
  if (d.calendar !== null && d.calendar !== undefined)
    out.push({ key: "calendar", title: "Calendar", lines: typeof d.calendar === "string" ? [d.calendar] : d.calendar });
  if (d.reminders !== null && d.reminders !== undefined)
    out.push({ key: "reminders", title: "Reminders", lines: d.reminders.map(r => r.local + "  " + r.content), count: d.reminders.length });
  if ((d.email || []).length) out.push({ key: "email", title: "Email", lines: d.email.map(m => m.sender + " — " + m.subject) });
  if (d.shopping !== null && d.shopping !== undefined) out.push({ key: "shopping", title: "Shopping", lines: [], count: d.shopping });
  return out;
}
```

Renderer, registered as `briefing: briefingCard` in `CARDS`:

```js
function briefingCard(data, mount) {
  const secs = briefingSections(data);
  const empty = { weather: "", calendar: "Nothing on the calendar.", reminders: "No reminders today.", shopping: "The list is empty." };
  mount.innerHTML =
    '<div class="brief">' +
      '<p class="bgreet">' + esc(data.greeting || "") + "</p>" +
      '<div class="bgrid">' +
      secs.map(s =>
        '<div class="card bsec"><header>' + esc(s.title) +
          (s.count !== undefined ? ' · ' + s.count : "") + "</header>" +
          (s.key === "shopping"
            ? '<p class="csugg">' + (s.count ? s.count + " item" + (s.count === 1 ? "" : "s") + " on the list." : empty.shopping) +
              ' <button class="cundo bopen" data-intent="recall_shopping">open</button></p>'
            : s.lines.length
              ? "<ul>" + s.lines.map(l => "<li><span class=\"citem\">" + esc(l) + "</span></li>").join("") + "</ul>"
              : '<p class="cempty">' + esc(empty[s.key] || "") + "</p>") +
        "</div>").join("") +
      "</div></div>";
  for (const b of mount.querySelectorAll(".bopen")) b.onclick = () => showRead(b.dataset.intent);
}
```

CSS:

```css
#today { max-width: 760px; margin: 18px auto 0; padding: 0 20px; width: 100%; }
#today[hidden] { display: none; }
#today .thead { display: flex; align-items: center; gap: 8px; font-size: 12px; letter-spacing: .08em;
                text-transform: uppercase; color: var(--muted); margin-bottom: 8px; }
#today .thead button { margin-left: auto; border: 0; color: var(--muted); font-size: 12px; }
#today.closed .brief { display: none; }
.bgreet { margin: 0 0 10px; font-size: 17px; font-weight: 500; }
.bgrid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.bsec li { padding: 6px 12px; }
```

Panel wiring, called from `start()` after `loadTimeline()` and at the end of `poll()` when not hidden:

```js
async function loadToday() {
  const box = $("#today");
  try {
    const r = await dispatch("briefing", {});
    const card = (r.cards || [])[0];
    if (!card || card.kind !== "briefing") { box.hidden = true; return; }
    box.innerHTML = '<div class="thead">Today<button type="button" id="tfold"></button></div><div class="tmount"></div>';
    briefingCard(card.data || {}, box.querySelector(".tmount"));
    let closed = false; try { closed = localStorage.getItem("wren_today") === "closed"; } catch (e) {}
    box.classList.toggle("closed", closed);
    const fold = box.querySelector("#tfold");
    fold.textContent = closed ? "show" : "hide";
    fold.onclick = () => {
      closed = !closed; box.classList.toggle("closed", closed); fold.textContent = closed ? "show" : "hide";
      try { localStorage.setItem("wren_today", closed ? "closed" : ""); } catch (e) {}
    };
    box.hidden = false;
  } catch (e) { box.hidden = true; }   // briefing off (404) or a blip: no panel
}
```

The chip "Catch me up" draws the same card into the log through `showRead("briefing")`, so `CARDS.briefing` serves both.

- [ ] **Step 4: Run tests, look once**

Run: `pytest tests/test_chat_renderer.py tests/test_webchat.py -q`. Boot the scratch instance as in Task 5 with `BRIEFING_TIME=` unset and `WEATHER_LAT`/`CALENDAR_URLS` unset: the panel should show Reminders and Shopping sections only, fold and remember it.

- [ ] **Step 5: Commit**

```bash
git add wren/communication/chat.html tests/test_chat_renderer.py tests/test_webchat.py
git commit -m "feat(web): the landing is the briefing, pinned above the timeline

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Phone layout, and a screenshot smoke script

**Files:**
- Modify: `wren/communication/chat.html`
- Create: `tests/screens.py` (not collected by pytest: no `test_` prefix)
- Test: `tests/test_webchat.py`

**Interfaces:**
- Produces: under 700px the sidebar is a drawer (`#app.narrow`, `#app.narrow.open`), `#menu` visible, `#scrim` closes it; `#reads` scrolls; `.bgrid` is one column. `tests/screens.py <outdir>` boots a scratch Wren on a temp DB and port, seeds it through `/api/dispatch` and `conversations.notice`, screenshots 1280 and 390 in light and dark, asserts no horizontal overflow at 390, and stops the process by PID.

- [ ] **Step 1: Write the failing page test**

Add to `tests/test_webchat.py`:

```python
def test_page_has_a_phone_layout():
    _, _, html = asyncio.run(_page_request())
    assert "@media (max-width: 700px)" in html
    assert "matchMedia(" in html
    assert 'id="scrim"' in html
    assert "@media (max-width: 640px)" not in html
```

Run: `pytest tests/test_webchat.py -q -k phone_layout` → FAIL.

- [ ] **Step 2: CSS and JS**

CSS, replacing the deleted 640px block:

```css
/* Under 700px the sidebar is a drawer: closed by default, opened from the
   top bar, closed by the scrim, by choosing an entry, or by the button again.
   `left`, not transform, so the fixed #rail logic above keeps working. */
@media (max-width: 700px) {
  #menu { display: inline-block; }
  #app.narrow aside { position: fixed; top: 0; left: -260px; height: 100%; z-index: 6;
                      transition: left .18s ease; box-shadow: none; }
  #app.narrow.open aside { left: 0; box-shadow: var(--shadow-lg); }
  #app.narrow.open #scrim { display: block; position: fixed; inset: 0; background: rgba(36,31,26,.35); z-index: 5; }
  #app.narrow #rail, #app.narrow #edge { display: none; }
  #topbar { padding: 8px 12px; }
  #log { padding: 16px 12px; }
  main footer { padding: 8px 12px 12px; }
  .msg .body { max-width: 92%; }
  .bgrid { grid-template-columns: 1fr; }
  #today { padding: 0 12px; }
  #plugins .sheet { width: 100%; }
  .pset input, .pset select { width: 100%; }
}
#menu { display: none; }
#scrim { display: none; }
@media (prefers-reduced-motion: reduce) { #app aside { transition: none; } }
```

JS:

```js
// Narrow is a media query, not a saved choice: rotate a tablet and the
// drawer appears or goes away with the width.
const narrow = matchMedia("(max-width: 700px)");
function setNarrow() {
  $("#app").classList.toggle("narrow", narrow.matches);
  $("#app").classList.remove("open");
  $("#menu").hidden = !narrow.matches;
  $("#menu").setAttribute("aria-expanded", "false");
}
narrow.addEventListener("change", setNarrow);
function openDrawer(on) {
  $("#app").classList.toggle("open", on);
  $("#menu").setAttribute("aria-expanded", on ? "true" : "false");
}
$("#menu").onclick = () => openDrawer(!$("#app").classList.contains("open"));
$("#scrim").onclick = () => openDrawer(false);
$("#sidenav").addEventListener("click", e => { if (e.target.closest(".nav")) openDrawer(false); });
```

Call `setNarrow()` inside `start()` before `loadTimeline()`. `#scrim` loses its `hidden` attribute in the markup (CSS controls it).

- [ ] **Step 3: The smoke script**

Create `tests/screens.py`:

```python
#!/usr/bin/env python3
"""Screenshot the web chat on a scratch instance. Not a pytest test.

    python3 tests/screens.py /tmp/shots

Boots Wren on a temp database and port, seeds a few rows through the API,
renders the page at 1280 and 390 wide in both themes, and asserts the phone
width never scrolls sideways. Needs Playwright in the python it runs under
(`pip install playwright && playwright install chromium`), which is a dev
tool, not a Wren dependency. The scratch process is stopped by its PID: never
`pkill -f wren.run`, which also matches the live service.
"""
import asyncio, os, pathlib, subprocess, sys, tempfile, time, urllib.request, json

REPO = pathlib.Path(__file__).resolve().parent.parent
PORT = 18790
TOKEN = "shot"


def _api(path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
                                 method="POST" if body is not None else "GET")
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def boot(tmp: pathlib.Path) -> subprocess.Popen:
    env = {**os.environ, "WREN_DB": str(tmp / "scratch.db"), "COMMUNICATION_PLUGINS": "http",
           "WREN_TOKENS": f"{TOKEN}:1", "WREN_OWNER_ID": "1", "OWNER_NAME": "Erin",
           "WREN_HTTP_HOST": "127.0.0.1", "WREN_HTTP_PORT": str(PORT),
           "DISCORD_TOKEN": "", "TELEGRAM_TOKEN": "", "BRIEFING_TIME": ""}
    proc = subprocess.Popen([str(REPO / "venv" / "bin" / "python3"), "-m", "wren.run"],
                            cwd=REPO, env=env, stdout=open(tmp / "wren.log", "wb"), stderr=subprocess.STDOUT)
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1)
            return proc
        except Exception:
            time.sleep(0.2)
    proc.terminate()
    raise SystemExit("scratch Wren did not come up; see " + str(tmp / "wren.log"))


def seed(tmp: pathlib.Path):
    for item in ("milk", "eggs", "olive oil"):
        _api("/api/dispatch", {"intent": "add_shopping_item", "content": item})
    _api("/api/dispatch", {"intent": "save_note", "content": "call the plumber", "tags": ["house"]})
    # a notice and a Telegram turn, straight into the scratch DB
    env = {**os.environ, "WREN_DB": str(tmp / "scratch.db")}
    subprocess.run([str(REPO / "venv" / "bin" / "python3"), "-c",
                    "from wren import conversations as c; c.init_db(); h = c.home(1); "
                    "c.add_message(h, 'user', 'add a tent to the packing list', channel='telegram'); "
                    "c.add_message(h, 'assistant', 'Added a tent to packing.', channel='telegram'); "
                    "c.notice(1, '⏰ Reminder: take out the trash', 'telegram')"],
                   cwd=REPO, env=env, check=True)


async def shoot(out: pathlib.Path):
    from playwright.async_api import async_playwright
    url = f"http://127.0.0.1:{PORT}/?token={TOKEN}"
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for scheme in ("light", "dark"):
            for width, height, name in ((1280, 800, "desktop"), (390, 844, "phone")):
                ctx = await browser.new_context(viewport={"width": width, "height": height},
                                                color_scheme=scheme, is_mobile=width < 700)
                page = await ctx.new_page()
                await page.goto(url)
                await page.wait_for_selector("#app.on")
                await page.wait_for_timeout(800)
                assert "token=" not in page.url, "the invite token stayed in the address bar"
                if width < 700:
                    wide = await page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
                    assert not wide, f"horizontal overflow at {width}px ({scheme})"
                await page.screenshot(path=str(out / f"{name}-{scheme}.png"))
                if width < 700:
                    await page.click("#menu"); await page.wait_for_timeout(300)
                    await page.screenshot(path=str(out / f"{name}-{scheme}-drawer.png"))
                await ctx.close()
        await browser.close()


def main():
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()); out.mkdir(parents=True, exist_ok=True)
    tmp = pathlib.Path(tempfile.mkdtemp())
    proc = boot(tmp)
    try:
        seed(tmp)
        asyncio.run(shoot(out))
        print(f"screens in {out}")
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run it and look**

Run: `pytest tests/test_webchat.py -q && python3 tests/screens.py /tmp/claude-shots` (system `python3` has Playwright on this machine; if it does not, `pip install playwright && playwright install chromium` in a throwaway venv). Open the eight PNGs. At 390 the top bar shows ☰ Today and the date, the drawer opens over the log, the composer spans the width, the briefing stacks to one column, the chip row scrolls. Fix what you see, once.

- [ ] **Step 5: Commit**

```bash
git add wren/communication/chat.html tests/screens.py tests/test_webchat.py
git commit -m "feat(web): phone layout with a drawer; screenshot smoke script on a scratch instance

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Spaces — skills declare them, the registry lists them, the API serves them

**Files:**
- Modify: `wren/skills/shopping_store.py`, `wren/skills/shopping_skill.py`, `wren/skills/notes_skill.py`, `wren/skills/reminder_skill.py`
- Modify: `wren/registry.py`
- Modify: `wren/communication/webchat.py`
- Test: `tests/test_shopping.py`, `tests/test_registry_enabled.py`, `tests/test_webchat.py`

**Interfaces:**
- Produces:
  - `shopping_store.list_names() -> list[str]` — `"shopping"` first, then every other list with an active item, alphabetical.
  - A skill may declare `SPACES` as a list of `{"key", "label", "intent", "params"}` or a zero-argument function returning one.
  - `registry.spaces() -> list[dict]` from enabled skills, in `PLUGINS` order.
  - `GET /api/spaces` → that list, any whitelisted user.

- [ ] **Step 1: Write the failing tests**

`tests/test_shopping.py` (match its existing fixture style; it already inits the store):

```python
def test_list_names_is_shopping_first_then_named_lists_with_active_items():
    shopping.add("tent", "owner", list_name="packing")
    shopping.add("drill", "owner", list_name="hardware")
    shopping.add("gone", "owner", list_name="zzz")
    shopping.remove("gone", list_name="zzz")
    assert shopping.list_names() == ["shopping", "hardware", "packing"]


def test_list_names_with_nothing_is_just_shopping():
    assert shopping.list_names() == ["shopping"]
```

`tests/test_registry_enabled.py` (it already has a fixture that inits `settings`; add `shopping_store.init_db()` where needed):

```python
def test_spaces_come_from_enabled_skills_only():
    from wren.skills import shopping_store, notes_skill
    shopping_store.init_db()
    keys = [s["key"] for s in registry.spaces()]
    assert "list:shopping" in keys and "notes" in keys and "ideas" in keys and "reminders" in keys
    registry.set_enabled(notes_skill, False)
    keys = [s["key"] for s in registry.spaces()]
    assert "notes" not in keys and "ideas" not in keys and "list:shopping" in keys


def test_shopping_spaces_follow_the_lists_that_exist():
    from wren.skills import shopping_store
    shopping_store.init_db()
    shopping_store.add("tent", "owner", list_name="packing")
    packing = next(s for s in registry.spaces() if s["key"] == "list:packing")
    assert packing == {"key": "list:packing", "label": "Packing", "intent": "recall_shopping",
                       "params": {"content": "", "list": "packing"}}


def test_every_space_names_an_intent_its_skill_owns():
    from wren.skills import shopping_store
    shopping_store.init_db()
    for s in registry.spaces():
        assert s["intent"] in registry.INTENT_HANDLERS, s
        assert set(s) == {"key", "label", "intent", "params"}
```

`tests/test_webchat.py`:

```python
def test_spaces_route_lists_spaces_for_any_whitelisted_user():
    from wren.skills import shopping_store
    shopping_store.init_db()
    status, spaces = call("get", "/api/spaces", token=TOKEN_B)
    assert status == 200
    assert {"key": "list:shopping", "label": "Shopping", "intent": "recall_shopping",
            "params": {"content": "", "list": "shopping"}} in spaces


def test_spaces_route_requires_a_token():
    status, _ = call("get", "/api/spaces")
    assert status == 401
```

Run: `pytest tests/test_shopping.py tests/test_registry_enabled.py tests/test_webchat.py -q -k "spaces or list_names"` → FAIL.

- [ ] **Step 2: Implement**

`wren/skills/shopping_store.py`:

```python
def list_names() -> list[str]:
    """Every list with something on it, the shopping list first whether or
    not it is empty: it is the one that always exists."""
    with db.conn() as con:
        rows = con.execute(
            "SELECT DISTINCT list FROM shopping_items WHERE status='active' AND list<>'shopping' ORDER BY list"
        ).fetchall()
    return ["shopping", *(r[0] for r in rows)]
```

`wren/skills/shopping_skill.py`, after `PROMPT_GUIDELINES`:

```python
def SPACES() -> list[dict]:
    """One sidebar entry per list. A function, not a constant: a packing
    list shows up the day it is created."""
    return [{"key": f"list:{name}",
             "label": "Shopping" if name == "shopping" else name.capitalize(),
             "intent": "recall_shopping",
             "params": {"content": "", "list": name}}
            for name in shopping.list_names()]
```

`wren/skills/notes_skill.py`:

```python
SPACES = [
    {"key": "notes", "label": "Notes", "intent": "recall_notes", "params": {"content": "", "tags": []}},
    {"key": "ideas", "label": "Ideas", "intent": "recall_ideas", "params": {"content": ""}},
]
```

`wren/skills/reminder_skill.py`:

```python
SPACES = [{"key": "reminders", "label": "Reminders", "intent": "recall_reminders", "params": {"content": ""}}]
```

`wren/registry.py`, after `file_intents`:

```python
def spaces() -> list[dict]:
    """Sidebar entries declared by enabled skills: SPACES is a list of
    {key, label, intent, params} or a zero-arg function returning one (a
    skill whose spaces depend on data). Same renderer as the chat cards,
    second mount point (skill-cards design, slice 3)."""
    out: list[dict] = []
    for plugin in enabled_plugins():
        declared = getattr(plugin, "SPACES", None)
        if callable(declared):
            declared = declared()
        out.extend(declared or [])
    return out
```

`wren/communication/webchat.py`, inside `register_routes`:

```python
    async def get_spaces(request):
        _auth(request)
        from .. import registry
        return web.json_response(registry.spaces())
```

and `app.router.add_get("/api/spaces", get_spaces)`.

- [ ] **Step 3: Run the suite, commit**

Run: `pytest -q`. Then:

```bash
git add -A wren/ tests/
git commit -m "feat(spaces): skills declare SPACES, registry aggregates, GET /api/spaces

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Spaces in the sidebar

**Files:**
- Modify: `wren/communication/chat.html`
- Test: `tests/test_webchat.py`

**Interfaces:**
- Consumes: `GET /api/spaces`, `/api/dispatch`, `renderCard`.
- Produces: `#spaces` entries; `#space` pane (sibling of `#log`); `openSpace(space)` / `#nav-today` returns to the timeline.

- [ ] **Step 1: Failing page test**

```python
def test_page_lists_spaces_and_can_return_to_today():
    _, _, html = asyncio.run(_page_request())
    assert "/api/spaces" in html
    assert 'id="space"' in html
    assert "function openSpace" in html
```

Run: `pytest tests/test_webchat.py -q -k spaces_and_can_return` → FAIL.

- [ ] **Step 2: Implement**

Markup: add `<div id="space" hidden><div class="smount"></div></div>` between `#today` and `#log`. CSS:

```css
#space { flex: 1; overflow-y: auto; padding: 20px; }
#space[hidden] { display: none; }
#space .smount { max-width: 760px; margin: 0 auto; }
#app.in-space #today, #app.in-space #log, #app.in-space footer { display: none; }
```

JS:

```js
let spaces = [];
async function loadSpaces() {
  try { spaces = await api("/api/spaces"); } catch (e) { spaces = []; }
  const box = $("#spaces"); box.innerHTML = "";
  for (const s of spaces) {
    const b = document.createElement("button"); b.className = "nav"; b.dataset.key = s.key;
    b.textContent = s.label; b.onclick = () => openSpace(s);
    box.append(b);
  }
  box.hidden = $("#spaces-label").hidden = spaces.length === 0;
}
// A space is a card drawn full-pane instead of in a bubble. Same renderer,
// same dispatch; nothing is written to the timeline.
async function openSpace(s) {
  $("#app").classList.add("in-space");
  $("#space").hidden = false;
  $("#ctitle").textContent = s.label;
  for (const n of document.querySelectorAll("#sidenav .nav")) n.classList.toggle("on", n.dataset.key === s.key);
  const mount = $("#space .smount");
  mount.innerHTML = '<p class="phint">Loading…</p>';
  try {
    const r = await dispatch(s.intent, s.params || {});
    const card = (r.cards || [])[0];
    if (!card || !renderCard(card, mount, r.files)) mount.innerHTML = md((r.replies || []).join("\n"));
  } catch (e) { mount.innerHTML = "<em>" + esc(e.message || "Something went wrong.") + "</em>"; }
}
function showToday() {
  $("#app").classList.remove("in-space");
  $("#space").hidden = true;
  $("#ctitle").textContent = "Today";
  for (const n of document.querySelectorAll("#sidenav .nav")) n.classList.toggle("on", n.id === "nav-today");
  $("#log").scrollTop = $("#log").scrollHeight;
}
$("#nav-today").onclick = showToday;
```

`start()` calls `loadSpaces()` after `loadTimeline()`; `poll()` calls `loadSpaces()` only when the previous call's keys differ is unnecessary: call it every tick (one small GET). The card's own refresh (`cardShell`) already re-dispatches with the space's params, so adding an item inside the pane redraws it in place.

- [ ] **Step 3: Test, look, commit**

Run: `pytest tests/test_webchat.py tests/test_chat_renderer.py -q && python3 tests/screens.py /tmp/claude-shots`. In the desktop screenshot the sidebar lists Shopping, Packing, Notes, Ideas, Reminders.

```bash
git add wren/communication/chat.html tests/test_webchat.py
git commit -m "feat(web): spaces in the sidebar, drawn full-pane by the card renderers

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: Contacts over HTTP

**Files:**
- Modify: `wren/communication/webchat.py`
- Test: `tests/test_webchat_plugins.py`

**Interfaces:**
- Consumes: `contacts.add`, `set_id`, `get`, `remove`, `all`, `SURFACES`, `surfaces_of`; `config.whitelist`.
- Produces: `GET /api/contacts` → `[{alias, discord: int|null, telegram: int|null}]`; `POST /api/contacts` `{alias, surface, id}` → the row (201 new, 200 added a surface); `DELETE /api/contacts/{alias}` → `{ok: true}` or 404. All owner-only.

- [ ] **Step 1: Failing tests**

Append to `tests/test_webchat_plugins.py` (use its existing owner/non-owner token fixtures and `call` helper; add `contacts.init_db()` to its autouse fixture):

```python
# ── people ─────────────────────────────────────────────────────────────────

def test_contacts_list_is_owner_only():
    assert call("get", "/api/contacts", token=OTHER_TOKEN)[0] == 403
    status, rows = call("get", "/api/contacts", token=OWNER_TOKEN)
    assert (status, rows) == (200, [])


def test_add_a_contact_then_a_second_surface_then_remove():
    status, row = call("post", "/api/contacts", token=OWNER_TOKEN,
                       json={"alias": "Hubby", "surface": "discord", "id": "123"})
    assert (status, row) == (201, {"alias": "hubby", "discord": 123, "telegram": None})
    status, row = call("post", "/api/contacts", token=OWNER_TOKEN,
                       json={"alias": "hubby", "surface": "telegram", "id": 456})
    assert (status, row) == (200, {"alias": "hubby", "discord": 123, "telegram": 456})
    assert call("get", "/api/contacts", token=OWNER_TOKEN)[1] == [row]
    assert call("delete", "/api/contacts/hubby", token=OWNER_TOKEN) == (200, {"ok": True})
    assert call("delete", "/api/contacts/hubby", token=OWNER_TOKEN)[0] == 404


@pytest.mark.parametrize("body", [
    {"alias": "", "surface": "discord", "id": "1"},
    {"alias": "owner", "surface": "discord", "id": "1"},
    {"alias": "x", "surface": "sms", "id": "1"},
    {"alias": "x", "surface": "discord", "id": "abc"},
    {"alias": "x", "surface": "discord"},
    {"alias": 5, "surface": "discord", "id": "1"},
])
def test_add_contact_rejects_bad_bodies(body):
    status, out = call("post", "/api/contacts", token=OWNER_TOKEN, json=body)
    assert status == 400 and "error" in out


def test_add_contact_refuses_a_surface_the_person_already_has():
    call("post", "/api/contacts", token=OWNER_TOKEN, json={"alias": "kim", "surface": "discord", "id": "1"})
    status, out = call("post", "/api/contacts", token=OWNER_TOKEN, json={"alias": "kim", "surface": "discord", "id": "2"})
    assert status == 409


def test_add_contact_refuses_an_id_registered_under_another_name():
    call("post", "/api/contacts", token=OWNER_TOKEN, json={"alias": "kim", "surface": "discord", "id": "1"})
    status, out = call("post", "/api/contacts", token=OWNER_TOKEN, json={"alias": "sam", "surface": "discord", "id": "1"})
    assert status == 409


def test_contact_routes_are_not_for_non_owners():
    assert call("post", "/api/contacts", token=OTHER_TOKEN, json={"alias": "x", "surface": "discord", "id": "1"})[0] == 403
    assert call("delete", "/api/contacts/x", token=OTHER_TOKEN)[0] == 403
```

Run: `pytest tests/test_webchat_plugins.py -q -k contact` → FAIL.

- [ ] **Step 2: Implement**

In `webchat.py` add `import sqlite3` and `from .. import contacts`, and inside `register_routes`:

```python
    def _contact_row(alias: str) -> dict:
        return contacts.get(alias) or {"alias": alias, **{s: None for s in contacts.SURFACES}}

    async def list_contacts(request):
        _owner(request)
        return web.json_response([_contact_row(a) for a in sorted(contacts.all())])

    async def add_contact(request):
        """The same rules as contacts_skill.add_contact, over HTTP: the owner's
        name and the placeholder "owner" are taken, a surface a person already
        has an id on is taken, and an id already registered under another
        name is a conflict. Kept in step by hand; a shared helper would be
        the right move the third time a rule is added."""
        _owner(request)
        body = await _json_object(request)
        alias, surface, raw_id = body.get("alias"), body.get("surface"), body.get("id")
        if not isinstance(alias, str) or not alias.strip():
            return web.json_response({"error": "alias must be a name"}, status=400)
        alias = alias.strip().lower()
        if alias == "owner":
            return web.json_response({"error": '"owner" is the placeholder for you, not a contact'}, status=400)
        if surface not in contacts.SURFACES:
            return web.json_response({"error": f"surface must be one of {', '.join(contacts.SURFACES)}"}, status=400)
        if not isinstance(raw_id, (str, int)) or isinstance(raw_id, bool) or not str(raw_id).strip().isdigit():
            return web.json_response({"error": "id must be the number that app calls them"}, status=400)
        surface_id = int(str(raw_id).strip())
        existing = alias in contacts.all()
        if alias in config.whitelist() and not existing:
            return web.json_response({"error": "that name is taken"}, status=409)
        if existing and surface in contacts.surfaces_of(contacts.all()[alias]):
            return web.json_response({"error": f"{alias} already has a {surface} id"}, status=409)
        try:
            if existing:
                contacts.set_id(alias, surface, surface_id)
            else:
                contacts.add(alias, surface_id, surface=surface)
        except sqlite3.IntegrityError:
            return web.json_response({"error": "that id is already registered under another name"}, status=409)
        return web.json_response(_contact_row(alias), status=200 if existing else 201)

    async def remove_contact(request):
        _owner(request)
        alias = request.match_info["alias"].strip().lower()
        if not contacts.remove(alias):
            return web.json_response({"error": "no such contact"}, status=404)
        return web.json_response({"ok": True})
```

Routes: `add_get("/api/contacts", list_contacts)`, `add_post("/api/contacts", add_contact)`, `add_delete("/api/contacts/{alias}", remove_contact)`.

- [ ] **Step 3: Test, commit**

Run: `pytest -q`.

```bash
git add wren/communication/webchat.py tests/test_webchat_plugins.py
git commit -m "feat(web): owner-only contact routes for the People tab

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 12: The People tab

**Files:**
- Modify: `wren/communication/chat.html`
- Test: `tests/test_webchat.py`

**Interfaces:**
- Consumes: Task 11's routes.
- Produces: a `People` tab in the panel; the invite-link explanation lives here, not on the sign-in card.

- [ ] **Step 1: Failing page test**

```python
def test_panel_has_a_people_tab_with_the_invite_explanation():
    _, _, html = asyncio.run(_page_request())
    assert 'data-tab="people"' in html and 'id="tab-people"' in html
    assert "/api/contacts" in html
    assert "?token=" in html                      # the invite link is explained here
    assert "manage.sh token add" in html           # and only here (owner's panel)
```

Run → FAIL.

- [ ] **Step 2: Implement**

Tab button `<button class="tab" data-tab="people">People</button>` after Plugins; section:

```html
<section id="tab-people" hidden>
  <p class="phint">Who Wren will talk to. Each person has their own notes and
  reminders; the shopping list is the household's.</p>
  <div id="pcontacts"></div>
  <h4>Add someone</h4>
  <form id="padd" class="pset">
    <input id="palias" placeholder="name, e.g. hubby" aria-label="name" required>
    <select id="psurface" aria-label="app"></select>
    <input id="pid" placeholder="their id in that app" aria-label="id" inputmode="numeric" required>
    <button type="submit">Add</button>
    <span class="savestat" id="pstat"></span>
  </form>
  <h4>Sign-in links</h4>
  <p class="phint">To let someone use this page, make them a sign-in token on
  the computer Wren runs on, then send them a link with it:</p>
  <pre>./manage.sh token add THEIR_ID</pre>
  <pre>https://your-wren/?token=THE_TOKEN</pre>
  <p class="phint">Opening that link signs their browser in and drops the
  token from the address bar. Tokens are never shown here.</p>
</section>
```

JS, called from `loadPlugins()`:

```js
async function loadPeople() {
  const box = $("#pcontacts"); box.innerHTML = "";
  let people = [];
  try { people = await api("/api/contacts"); } catch (e) { $("#perr").textContent = String(e.message || e); return; }
  const sel = $("#psurface"); sel.innerHTML = "";
  for (const s of ["discord", "telegram"]) { const o = document.createElement("option"); o.value = o.textContent = s; sel.append(o); }
  if (!people.length) { box.innerHTML = '<p class="phint">Just you so far.</p>'; return; }
  for (const p of people) {
    const row = document.createElement("div"); row.className = "prow";
    row.innerHTML = '<span class="dot ok"></span><span class="pname"></span><span class="pstate"></span>' +
                    '<button class="premove" type="button">Remove</button>';
    row.querySelector(".pname").textContent = p.alias;
    row.querySelector(".pstate").textContent =
      ["discord", "telegram"].filter(s => p[s]).map(s => s + " " + p[s]).join(" · ") || "no app yet";
    const rm = row.querySelector(".premove");
    // inline confirm: the first click arms, the second removes, anything else disarms
    rm.onclick = async () => {
      if (!rm.dataset.armed) { rm.dataset.armed = "1"; rm.textContent = "Remove " + p.alias + "?"; return; }
      try { await api("/api/contacts/" + encodeURIComponent(p.alias), { method: "DELETE" }); loadPeople(); }
      catch (e) { $("#perr").textContent = String(e.message || e); }
    };
    rm.onblur = () => { delete rm.dataset.armed; rm.textContent = "Remove"; };
    box.append(row);
  }
}
$("#padd").onsubmit = async e => {
  e.preventDefault();
  const stat = $("#pstat"); stat.textContent = "…"; stat.className = "savestat";
  try {
    await api("/api/contacts", { method: "POST", body: JSON.stringify(
      { alias: $("#palias").value, surface: $("#psurface").value, id: $("#pid").value }) });
    $("#palias").value = ""; $("#pid").value = "";
    stat.textContent = "✓ added"; stat.classList.add("ok");
    loadPeople();
  } catch (err) { stat.textContent = String(err.message || err); stat.classList.add("err"); }
};
```

CSS: `.premove { border: 1px solid var(--line); border-radius: 8px; padding: 4px 10px; font-size: 12.5px; color: var(--muted); } .premove[data-armed] { color: var(--alert); border-color: var(--alert); }`.

- [ ] **Step 3: Test, look, commit**

Run: `pytest tests/test_webchat.py -q`; boot the scratch instance and add, add-a-surface, and remove a person from the tab.

```bash
git add wren/communication/chat.html tests/test_webchat.py
git commit -m "feat(web): People tab -- contacts and the invite link, for the owner

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 13: Docs, the invite link in manage.sh, and the plan's own status

**Files:**
- Modify: `README.md`, `.env.example`, `CLAUDE.md`, `PROJECT_PLAN.md`, `manage.sh`
- Test: `tests/test_manage_sh.py` (if it asserts on `token add`'s output)

- [ ] **Step 1: manage.sh**

In `cmd_token_add`, after the token line, print the link:

```bash
    local host port
    host=$(env_get WREN_HTTP_HOST); port=$(env_get WREN_HTTP_PORT)
    echo "  or send them this link, which signs their browser in on its own:"
    echo
    echo "  http://${host:-127.0.0.1}:${port:-8787}/?token=$tok"
    echo
    echo "  (use the https address of your reverse proxy if you have one)"
```

Run `pytest tests/test_manage_sh.py -q`; adjust any assertion on the exact output.

- [ ] **Step 2: README**

- "Web chat" section: replace the conversation-list description and the route table with the timeline: one thread per person, every door writes to it, reminders and the briefing appear in place marked with where they went, a 30-second poll, the Today panel, Spaces, the People tab, the `?token=` link. Route table: `GET /api/timeline` (three forms), `/api/dispatch`, `/api/me`, `/api/spaces`, `/api/plugins`, `/api/settings`, `/api/contacts` (three).
- Delete "This is the only channel besides Discord with conversation history" and the Telegram paragraph saying follow-ups won't resolve there; say instead that history is the timeline on every door.
- The machine API table: `/message` returns `{replies, files, cards, messages}`.
- `http` plugin: replace "send-only, logs and drops" with: passive — a reminder lands on the timeline and the page shows it; `NOTIFY_VIA=http` is allowed and means web-only; `any` still prefers a surface that can push.
- The landing screen subsection: the briefing panel, chips instead of pills.

- [ ] **Step 3: .env.example, CLAUDE.md, PROJECT_PLAN.md**

`.env.example`: the `NOTIFY_VIA` comment: "http: only the web chat sees it (no push to a phone)". `CLAUDE.md`: in rule 3 add that a chat plugin's `Channel` declares `NAME`; in rule 4 add `SPACES`; add a rule 8: "**Core records the transcript.** `core.handle_message` writes the user turn and wraps the channel so every reply is written; the router writes notices. A surface never writes to `conversations` itself." Update the test count line. `PROJECT_PLAN.md`: a dated paragraph "One timeline (2026-10-13)" at the end summarising what landed, pointing at the spec, and marking "Slice 3: spaces" and "Web chat as an installable app" in the backlog as landed / still parked respectively.

- [ ] **Step 4: Run everything, commit**

Run: `pytest -q && python3 tests/screens.py /tmp/claude-shots`.

```bash
git add README.md .env.example CLAUDE.md PROJECT_PLAN.md manage.sh tests/test_manage_sh.py
git commit -m "docs: one timeline, passive http, spaces, people, invite links

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Self-review

**Spec coverage.** §1 storage → Task 1; §1 who writes (core, channels, history, notices, passive http, dry run) → Tasks 2, 4; §2 routes → Task 3; §2 layout, poll, chips, cards, fonts, settings, sign-in → Task 5; §3 → Tasks 6, 7; §4 → Task 8; §5 → Tasks 9, 10; §6 → Tasks 11, 12; README/plan → Task 13; rollout (backup, one restart for 1+2) → the finishing step, not a task. No gap found.

**Type consistency.** `handle_message -> list[int]` (Task 2) is what `_dispatch` reads (Task 3). `conversations.by_ids` (Task 1) is what `_payload` calls (Task 3). `conversations.notice(owner_id, text, channel)` (Task 1) is what the router calls (Task 4). `CollectingChannel(name=)` (Task 2) is what `_dispatch(name=)` builds (Task 3). The card `data` shape of Task 6 is what `briefingSections` reads in Task 7. `registry.spaces()` rows (Task 9) are what `openSpace` dispatches (Task 10).

**Review Focus.** 1 → `test_two_home_rows_both_read` (Task 1). 2 → `test_timeline_cursor_naming_another_users_row_pages_nothing` (Task 3). 3 → `test_any_does_not_fall_back_to_passive_after_a_transient_failure` and `test_a_transient_failure_raises_and_records_nothing` (Task 4). 4 → `test_parse_iso_accepts_microseconds` (Task 5). 5 → `test_dry_run_returns_the_rows_but_persists_none` (Task 3).
