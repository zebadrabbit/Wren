import os, asyncio, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

import itertools

from wren import conversations
from wren import db

ALICE = 1
BOB = 2


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("WREN_DB", str(tmp_path / "wren.db"))
    conversations.init_db()


@pytest.fixture
def clock(monkeypatch):
    """Monotonic fake timestamps.

    Every call returns a strictly larger string, so tests that interleave
    rows by created_at do not depend on the real clock's resolution.
    """
    ticks = itertools.count(1)
    monkeypatch.setattr(
        conversations, "_now",
        lambda: "2026-08-08T12:00:00.{:06d}+00:00".format(next(ticks)),
    )


# --------------------------------------------------------------------------
# add_message
# --------------------------------------------------------------------------

def test_add_message_returns_an_id():
    convo_id = conversations.home(ALICE)
    msg_id = conversations.add_message(convo_id, "user", "hello")
    assert isinstance(msg_id, int)
    assert msg_id > 0


@pytest.mark.parametrize("role", ["system", "tool", "USER", "Assistant", "", "bot"])
def test_add_message_rejects_an_unknown_role(role):
    convo_id = conversations.home(ALICE)
    with pytest.raises(ValueError, match="role must be"):
        conversations.add_message(convo_id, role, "hello")
    assert conversations.timeline(ALICE) == []


# --------------------------------------------------------------------------
# owner isolation — the security boundary
# --------------------------------------------------------------------------


def test_add_message_stores_and_returns_a_card():
    convo = conversations.home(ALICE)
    conversations.add_message(convo, "assistant", "milk, eggs",
                              card={"kind": "shopping", "intent": "recall_shopping",
                                    "params": {"content": ""}})
    row = conversations.timeline(ALICE)[0]
    assert row["content"] == "milk, eggs"
    assert row["card"] == {"kind": "shopping", "intent": "recall_shopping",
                           "params": {"content": ""}}


def test_a_message_without_a_card_reads_back_as_none():
    # every message written before this column existed takes this path
    convo = conversations.home(ALICE)
    conversations.add_message(convo, "assistant", "hello")
    assert conversations.timeline(ALICE)[0]["card"] is None


def test_init_db_adds_the_card_column_to_a_table_that_predates_it(tmp_path, monkeypatch):
    monkeypatch.setenv("WREN_DB", str(tmp_path / "old.db"))
    with db.conn() as con:
        con.execute("""
            CREATE TABLE messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role            TEXT NOT NULL,
                content         TEXT NOT NULL,
                created_at      TEXT NOT NULL
            )
        """)
        con.execute("INSERT INTO messages (conversation_id, role, content, created_at) "
                    "VALUES (1, 'user', 'existing row', '2026-01-01T00:00:00+00:00')")

    conversations.init_db()

    with db.conn() as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
        kept = con.execute("SELECT content FROM messages").fetchone()
    assert "card" in cols
    assert kept[0] == "existing row"      # the migration must not drop anything


def test_init_db_is_idempotent():
    conversations.init_db()
    conversations.init_db()               # must not raise "duplicate column name"


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
