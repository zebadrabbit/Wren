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

    The real _now() has one-second resolution, so a test that creates and then
    touches a conversation inside the same second cannot tell "updated_at was
    refreshed" from "updated_at was ignored". Every call here returns a strictly
    larger string, which is what the ORDER BY compares.
    """
    ticks = itertools.count(1)
    monkeypatch.setattr(
        conversations, "_now",
        lambda: "2026-08-08T12:00:00.{:06d}+00:00".format(next(ticks)),
    )


def _all_message_rows():
    """Read the messages table directly — delete() must really empty it, not
    merely make the rows unreachable through the module's own API."""
    with db.conn() as con:
        return con.execute(
            "SELECT id, conversation_id, role, content FROM messages ORDER BY id"
        ).fetchall()


# --------------------------------------------------------------------------
# create / list_for / get
# --------------------------------------------------------------------------

def test_create_returns_an_id():
    convo_id = conversations.create(ALICE)
    assert isinstance(convo_id, int)
    assert convo_id > 0


def test_create_defaults_to_new_chat_title():
    convo_id = conversations.create(ALICE)
    assert conversations.get(convo_id, ALICE)["title"] == "New chat"


def test_create_returns_distinct_ids():
    first = conversations.create(ALICE)
    second = conversations.create(ALICE)
    assert first != second


def test_list_for_is_empty_for_a_new_owner():
    assert conversations.list_for(ALICE) == []


def test_list_for_is_newest_first_by_updated_at(clock):
    a = conversations.create(ALICE, "A")
    b = conversations.create(ALICE, "B")
    c = conversations.create(ALICE, "C")

    assert [r["id"] for r in conversations.list_for(ALICE)] == [c, b, a]

    # touching the oldest must float it to the top: ordering is by updated_at,
    # not by id or created_at
    conversations.touch(a, ALICE)
    assert [r["id"] for r in conversations.list_for(ALICE)] == [a, c, b]


def test_list_for_returns_the_full_row():
    convo_id = conversations.create(ALICE, "Groceries")
    (row,) = conversations.list_for(ALICE)
    assert row["id"] == convo_id
    assert row["title"] == "Groceries"
    assert row["created_at"]
    assert row["updated_at"]


def test_get_returns_the_conversation_for_its_owner():
    convo_id = conversations.create(ALICE, "Groceries")
    convo = conversations.get(convo_id, ALICE)
    assert convo["id"] == convo_id
    assert convo["title"] == "Groceries"
    assert convo["created_at"] == convo["updated_at"]


def test_get_unknown_id_is_none():
    assert conversations.get(4242, ALICE) is None


# --------------------------------------------------------------------------
# rename
# --------------------------------------------------------------------------

def test_rename_changes_the_title():
    convo_id = conversations.create(ALICE)
    assert conversations.rename(convo_id, ALICE, "Dinner plans") is True
    assert conversations.get(convo_id, ALICE)["title"] == "Dinner plans"


def test_rename_bumps_updated_at(clock):
    convo_id = conversations.create(ALICE)
    before = conversations.get(convo_id, ALICE)["updated_at"]
    conversations.rename(convo_id, ALICE, "Dinner plans")
    assert conversations.get(convo_id, ALICE)["updated_at"] > before


def test_rename_rejects_an_empty_title():
    convo_id = conversations.create(ALICE, "Keep me")
    assert conversations.rename(convo_id, ALICE, "") is False
    assert conversations.get(convo_id, ALICE)["title"] == "Keep me"


def test_rename_rejects_a_whitespace_only_title():
    convo_id = conversations.create(ALICE, "Keep me")
    assert conversations.rename(convo_id, ALICE, "   \t \n  ") is False
    assert conversations.get(convo_id, ALICE)["title"] == "Keep me"


def test_rename_collapses_whitespace():
    convo_id = conversations.create(ALICE)
    assert conversations.rename(convo_id, ALICE, "  dinner \n  plans  ") is True
    assert conversations.get(convo_id, ALICE)["title"] == "dinner plans"


def test_rename_truncates_to_title_max():
    convo_id = conversations.create(ALICE)
    assert conversations.rename(convo_id, ALICE, "x" * 500) is True
    title = conversations.get(convo_id, ALICE)["title"]
    assert len(title) == conversations.TITLE_MAX
    assert title == "x" * conversations.TITLE_MAX


def test_rename_keeps_a_title_of_exactly_title_max():
    convo_id = conversations.create(ALICE)
    exact = "y" * conversations.TITLE_MAX
    assert conversations.rename(convo_id, ALICE, exact) is True
    assert conversations.get(convo_id, ALICE)["title"] == exact


def test_rename_unknown_id_returns_false():
    assert conversations.rename(4242, ALICE, "nope") is False


# --------------------------------------------------------------------------
# delete
# --------------------------------------------------------------------------

def test_delete_removes_the_conversation_and_its_messages():
    convo_id = conversations.create(ALICE)
    conversations.add_message(convo_id, "user", "hello")
    conversations.add_message(convo_id, "assistant", "hi there")
    assert len(_all_message_rows()) == 2

    assert conversations.delete(convo_id, ALICE) is True

    assert conversations.get(convo_id, ALICE) is None
    assert conversations.list_for(ALICE) == []
    # the rows are gone from the table, not just unreachable
    assert _all_message_rows() == []
    assert conversations.messages(convo_id) == []


def test_delete_leaves_other_conversations_messages_alone():
    keep = conversations.create(ALICE)
    drop = conversations.create(ALICE)
    conversations.add_message(keep, "user", "keep me")
    conversations.add_message(drop, "user", "drop me")

    assert conversations.delete(drop, ALICE) is True

    assert [r[3] for r in _all_message_rows()] == ["keep me"]
    assert [m["content"] for m in conversations.messages(keep)] == ["keep me"]


def test_delete_unknown_id_returns_false():
    assert conversations.delete(4242, ALICE) is False


# --------------------------------------------------------------------------
# add_message / messages
# --------------------------------------------------------------------------

def test_add_message_returns_an_id():
    convo_id = conversations.create(ALICE)
    msg_id = conversations.add_message(convo_id, "user", "hello")
    assert isinstance(msg_id, int)
    assert msg_id > 0


@pytest.mark.parametrize("role", ["system", "tool", "USER", "Assistant", "", "bot"])
def test_add_message_rejects_an_unknown_role(role):
    convo_id = conversations.create(ALICE)
    with pytest.raises(ValueError, match="role must be"):
        conversations.add_message(convo_id, role, "hello")
    assert _all_message_rows() == []


def test_messages_is_empty_for_a_fresh_conversation():
    convo_id = conversations.create(ALICE)
    assert conversations.messages(convo_id) == []


def test_messages_returns_oldest_first():
    convo_id = conversations.create(ALICE)
    for text in ("one", "two", "three", "four"):
        conversations.add_message(convo_id, "user", text)
    assert [m["content"] for m in conversations.messages(convo_id)] == [
        "one", "two", "three", "four",
    ]


def test_messages_returns_the_full_row():
    convo_id = conversations.create(ALICE)
    msg_id = conversations.add_message(convo_id, "assistant", "hi there")
    (msg,) = conversations.messages(convo_id)
    assert msg["id"] == msg_id
    assert msg["role"] == "assistant"
    assert msg["content"] == "hi there"
    assert msg["created_at"]


def test_messages_only_returns_its_own_conversation():
    mine = conversations.create(ALICE)
    other = conversations.create(ALICE)
    conversations.add_message(mine, "user", "mine")
    conversations.add_message(other, "user", "other")
    assert [m["content"] for m in conversations.messages(mine)] == ["mine"]


def test_messages_before_id_excludes_that_message_and_everything_after():
    convo_id = conversations.create(ALICE)
    ids = [conversations.add_message(convo_id, "user", str(n)) for n in range(1, 6)]

    # this is what stops the model seeing the in-flight message twice
    got = conversations.messages(convo_id, before_id=ids[2])
    assert [m["id"] for m in got] == ids[:2]
    assert [m["content"] for m in got] == ["1", "2"]


def test_messages_before_id_of_the_first_message_is_empty():
    convo_id = conversations.create(ALICE)
    first = conversations.add_message(convo_id, "user", "only")
    assert conversations.messages(convo_id, before_id=first) == []


def test_messages_limit_returns_the_newest_n_in_oldest_first_order():
    # A plain "ORDER BY id ASC LIMIT 2" would return messages 1 and 2 — the
    # OLDEST, i.e. the least relevant context. It must return 4 and 5, and in
    # that order, not 5 then 4.
    convo_id = conversations.create(ALICE)
    ids = [conversations.add_message(convo_id, "user", str(n)) for n in range(1, 6)]

    got = conversations.messages(convo_id, limit=2)
    assert [m["content"] for m in got] == ["4", "5"]
    assert [m["id"] for m in got] == [ids[3], ids[4]]


def test_messages_limit_larger_than_history_returns_everything():
    convo_id = conversations.create(ALICE)
    for n in range(1, 4):
        conversations.add_message(convo_id, "user", str(n))
    assert [m["content"] for m in conversations.messages(convo_id, limit=99)] == [
        "1", "2", "3",
    ]


def test_messages_before_id_and_limit_combined():
    convo_id = conversations.create(ALICE)
    ids = [conversations.add_message(convo_id, "user", str(n)) for n in range(1, 6)]

    # drop 5 (in-flight), then keep the newest 2 of what remains: 3 and 4
    got = conversations.messages(convo_id, before_id=ids[4], limit=2)
    assert [m["content"] for m in got] == ["3", "4"]
    assert [m["id"] for m in got] == [ids[2], ids[3]]


# --------------------------------------------------------------------------
# title_from
# --------------------------------------------------------------------------

def test_title_from_collapses_whitespace():
    assert conversations.title_from("  remind   me \n to\tcall  ") == "remind me to call"


def test_title_from_keeps_short_text():
    assert conversations.title_from("dinner plans") == "dinner plans"


def test_title_from_truncates_long_text_with_an_ellipsis():
    title = conversations.title_from("z" * 400)
    assert len(title) == conversations.TITLE_MAX
    assert title.endswith("…")
    assert title == "z" * (conversations.TITLE_MAX - 1) + "…"


def test_title_from_keeps_text_of_exactly_title_max():
    exact = "z" * conversations.TITLE_MAX
    assert conversations.title_from(exact) == exact


def test_title_from_empty_falls_back_to_new_chat():
    assert conversations.title_from("") == "New chat"


def test_title_from_whitespace_only_falls_back_to_new_chat():
    assert conversations.title_from("  \n \t ") == "New chat"


# --------------------------------------------------------------------------
# owner isolation — the security boundary
# --------------------------------------------------------------------------

def test_list_for_never_shows_another_owners_conversation():
    alice_convo = conversations.create(ALICE, "Alice private")
    bob_convo = conversations.create(BOB, "Bob private")

    assert [r["id"] for r in conversations.list_for(BOB)] == [bob_convo]
    assert [r["id"] for r in conversations.list_for(ALICE)] == [alice_convo]


def test_get_by_another_owner_is_none():
    alice_convo = conversations.create(ALICE, "Alice private")
    assert conversations.get(alice_convo, BOB) is None
    # ...and it is still there for its real owner
    assert conversations.get(alice_convo, ALICE)["title"] == "Alice private"


def test_rename_by_another_owner_is_refused_and_changes_nothing():
    alice_convo = conversations.create(ALICE, "Alice private")

    assert conversations.rename(alice_convo, BOB, "pwned") is False
    assert conversations.get(alice_convo, ALICE)["title"] == "Alice private"


def test_delete_by_another_owner_is_refused_and_keeps_the_messages():
    alice_convo = conversations.create(ALICE, "Alice private")
    conversations.add_message(alice_convo, "user", "secret")
    conversations.add_message(alice_convo, "assistant", "acknowledged")

    assert conversations.delete(alice_convo, BOB) is False

    assert conversations.get(alice_convo, ALICE)["title"] == "Alice private"
    assert [m["content"] for m in conversations.messages(alice_convo)] == [
        "secret", "acknowledged",
    ]
    assert len(_all_message_rows()) == 2


def test_touch_updates_updated_at_for_its_owner(clock):
    convo_id = conversations.create(ALICE)
    before = conversations.get(convo_id, ALICE)["updated_at"]
    conversations.touch(convo_id, ALICE)
    assert conversations.get(convo_id, ALICE)["updated_at"] > before


def test_touch_by_another_owner_does_not_modify_updated_at(clock):
    alice_convo = conversations.create(ALICE, "Alice private")
    before = conversations.get(alice_convo, ALICE)["updated_at"]

    conversations.touch(alice_convo, BOB)

    assert conversations.get(alice_convo, ALICE)["updated_at"] == before


def test_touch_by_another_owner_does_not_reorder_the_owners_list(clock):
    alice_old = conversations.create(ALICE, "old")
    alice_new = conversations.create(ALICE, "new")

    conversations.touch(alice_old, BOB)

    assert [r["id"] for r in conversations.list_for(ALICE)] == [alice_new, alice_old]


def test_add_message_stores_and_returns_a_card():
    convo = conversations.create(1)
    conversations.add_message(convo, "assistant", "milk, eggs",
                              card={"kind": "shopping", "intent": "recall_shopping",
                                    "params": {"content": ""}})
    row = conversations.messages(convo)[0]
    assert row["content"] == "milk, eggs"
    assert row["card"] == {"kind": "shopping", "intent": "recall_shopping",
                           "params": {"content": ""}}


def test_a_message_without_a_card_reads_back_as_none():
    # every message written before this column existed takes this path
    convo = conversations.create(1)
    conversations.add_message(convo, "assistant", "hello")
    assert conversations.messages(convo)[0]["card"] is None


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
