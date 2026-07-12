import os, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("HUSBAND_ID", "2")

import notes

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(notes, "DB_PATH", str(tmp_path / "test.db"))
    notes.init_db()

def test_save_and_list_recent():
    note_id = notes.save(1, "buy milk", ["grocery"])
    assert isinstance(note_id, int)
    results = notes.list_recent(1)
    assert len(results) == 1
    assert results[0]["content"] == "buy milk"
    assert results[0]["tags"] == "grocery"

def test_search_by_tag():
    notes.save(1, "buy eggs", ["grocery"])
    notes.save(1, "fix the fence", ["plans", "home"])
    results = notes.search(1, tags=["grocery"])
    assert len(results) == 1
    assert "eggs" in results[0]["content"]

def test_search_no_tag_returns_all():
    notes.save(1, "note one", ["a"])
    notes.save(1, "note two", ["b"])
    results = notes.search(1)
    assert len(results) == 2

def test_owner_isolation():
    notes.save(1, "my note", ["personal"])
    notes.save(2, "their note", ["personal"])
    assert len(notes.list_recent(1)) == 1
    assert len(notes.list_recent(2)) == 1

def test_search_tag_is_exact_not_substring():
    notes.save(1, "note about home", ["home"])
    notes.save(1, "note about homework", ["homework"])
    results = notes.search(1, tags=["home"])
    assert len(results) == 1
    assert results[0]["content"] == "note about home"
