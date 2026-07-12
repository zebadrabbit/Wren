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

def test_delete_existing_note():
    note_id = notes.save(1, "buy milk", ["grocery"])
    assert notes.delete(note_id) is True
    assert notes.list_recent(1) == []

def test_delete_nonexistent_returns_false():
    assert notes.delete(9999) is False

def test_find_matches_substring_case_insensitive():
    notes.save(1, "Build a treehouse for the kids", ["idea"])
    notes.save(1, "Learn to bake bread", ["idea"])
    results = notes.find(1, "TREEHOUSE", tags=["idea"])
    assert len(results) == 1
    assert "treehouse" in results[0]["content"].lower()

def test_find_no_match_returns_empty():
    notes.save(1, "Build a treehouse", ["idea"])
    results = notes.find(1, "spaceship", tags=["idea"])
    assert results == []

def test_find_multiple_matches():
    notes.save(1, "treehouse idea one", ["idea"])
    notes.save(1, "treehouse idea two", ["idea"])
    results = notes.find(1, "treehouse", tags=["idea"])
    assert len(results) == 2

def test_find_respects_owner_scope():
    notes.save(1, "treehouse for me", ["idea"])
    notes.save(2, "treehouse for them", ["idea"])
    results = notes.find(1, "treehouse", tags=["idea"])
    assert len(results) == 1
