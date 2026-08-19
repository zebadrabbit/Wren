import os, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from wren.skills import reminders_store as reminders

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("WREN_DB", str(tmp_path / "wren.db"))
    reminders.init_db()

def test_save_and_pending():
    reminders.save(1, "check the oven", "2026-07-12T21:00:00+00:00")
    items = reminders.pending(1)
    assert len(items) == 1
    assert items[0]["content"] == "check the oven"
    assert items[0]["status"] == "pending"

def test_pending_ordered_soonest_first():
    reminders.save(1, "later", "2026-07-12T22:00:00+00:00")
    reminders.save(1, "sooner", "2026-07-12T21:00:00+00:00")
    items = reminders.pending(1)
    assert [i["content"] for i in items] == ["sooner", "later"]

def test_pending_scoped_to_owner():
    reminders.save(1, "mine", "2026-07-12T21:00:00+00:00")
    reminders.save(2, "theirs", "2026-07-12T21:00:00+00:00")
    assert len(reminders.pending(1)) == 1
    assert len(reminders.pending(2)) == 1

def test_find_pending_case_insensitive_substring():
    reminders.save(1, "Check the Oven", "2026-07-12T21:00:00+00:00")
    results = reminders.find_pending(1, "oven")
    assert len(results) == 1

def test_find_pending_excludes_cancelled():
    reminder_id = reminders.save(1, "check the oven", "2026-07-12T21:00:00+00:00")
    reminders.cancel(reminder_id)
    assert reminders.find_pending(1, "oven") == []

def test_find_pending_prefers_an_exact_match():
    reminders.save(1, "call mum", "2030-01-01T09:00:00+00:00")
    reminders.save(1, "call mum about the car", "2030-01-01T10:00:00+00:00")

    found = reminders.find_pending(1, "call mum")
    assert [r["content"] for r in found] == ["call mum"]


def test_find_pending_still_substring_matches_when_no_exact_match():
    reminders.save(1, "call mum about the car", "2030-01-01T10:00:00+00:00")
    found = reminders.find_pending(1, "the car")
    assert [r["content"] for r in found] == ["call mum about the car"]

def test_cancel_returns_true_and_marks_cancelled():
    reminder_id = reminders.save(1, "check the oven", "2026-07-12T21:00:00+00:00")
    assert reminders.cancel(reminder_id) is True
    assert reminders.pending(1) == []

def test_cancel_nonexistent_returns_false():
    assert reminders.cancel(9999) is False

def test_cancel_already_cancelled_returns_false():
    reminder_id = reminders.save(1, "check the oven", "2026-07-12T21:00:00+00:00")
    reminders.cancel(reminder_id)
    assert reminders.cancel(reminder_id) is False

def test_due_returns_pending_at_or_before_now():
    reminders.save(1, "past", "2026-07-12T20:00:00+00:00")
    reminders.save(1, "future", "2026-07-12T23:00:00+00:00")
    due = reminders.due("2026-07-12T21:00:00+00:00")
    assert [d["content"] for d in due] == ["past"]

def test_due_excludes_fired_and_cancelled():
    fired_id = reminders.save(1, "already fired", "2026-07-12T20:00:00+00:00")
    reminders.mark_fired(fired_id)
    cancelled_id = reminders.save(1, "cancelled one", "2026-07-12T20:00:00+00:00")
    reminders.cancel(cancelled_id)
    due = reminders.due("2026-07-12T21:00:00+00:00")
    assert due == []

def test_mark_fired_removes_from_due():
    reminder_id = reminders.save(1, "check the oven", "2026-07-12T20:00:00+00:00")
    reminders.mark_fired(reminder_id)
    assert reminders.due("2026-07-12T21:00:00+00:00") == []


def test_save_records_the_route():
    reminders.save(1, "join mythics", "2026-07-12T21:00:00+00:00", via="discord")
    assert reminders.pending(1)[0]["via"] == "discord"


def test_save_without_a_route_defaults_to_none():
    reminders.save(1, "check the oven", "2026-07-12T21:00:00+00:00")
    assert reminders.pending(1)[0]["via"] is None


def test_init_db_adds_via_to_a_pre_existing_table(tmp_path, monkeypatch):
    # a database written before this column existed must keep its rows
    monkeypatch.setenv("WREN_DB", str(tmp_path / "old.db"))
    from wren import db
    with db.conn() as con:
        con.execute("""
            CREATE TABLE reminders (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id   TEXT NOT NULL,
                content    TEXT NOT NULL,
                fire_at    TEXT NOT NULL,
                status     TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        con.execute("INSERT INTO reminders (owner_id, content, fire_at, status, created_at)"
                    " VALUES ('1','old one','2026-07-12T21:00:00+00:00','pending','2026-07-12T20:00:00+00:00')")
    reminders.init_db()
    rows = reminders.pending(1)
    assert [r["content"] for r in rows] == ["old one"]
    assert rows[0]["via"] is None
