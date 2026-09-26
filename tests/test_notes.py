import os, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")

from wren.skills import notes_store as notes

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("WREN_DB", str(tmp_path / "wren.db"))
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

def test_find_prefers_an_exact_match_over_a_longer_substring_match():
    # A card's discard button sends the row's own text. Without this, clicking
    # "build a treehouse" while "build a treehouse with a rope ladder" also
    # exists matches both, and the skill refuses as ambiguous.
    notes.save(1, "build a treehouse", ["idea"])
    notes.save(1, "build a treehouse with a rope ladder", ["idea"])

    found = notes.find(1, "build a treehouse", tags=["idea"])
    assert [n["content"] for n in found] == ["build a treehouse"]


def test_find_is_still_a_substring_search_when_nothing_matches_exactly():
    notes.save(1, "build a treehouse with a rope ladder", ["idea"])
    found = notes.find(1, "treehouse", tags=["idea"])
    assert [n["content"] for n in found] == ["build a treehouse with a rope ladder"]


def test_find_exact_match_ignores_case():
    notes.save(1, "Build A Treehouse", ["idea"])
    notes.save(1, "build a treehouse with a rope ladder", ["idea"])
    found = notes.find(1, "build a treehouse", tags=["idea"])
    assert [n["content"] for n in found] == ["Build A Treehouse"]


# ── attachments ───────────────────────────────────────────────────────────

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 32
PDF = b"%PDF-1.7\n" + b"\0" * 32


def test_attach_stores_bytes_and_the_sniffed_mime_wins_over_the_declared_one():
    notes.init_db()
    note_id = notes.save(1, "tyre receipt", [])
    att_id = notes.attach(note_id, "receipt.png", "image/png", JPEG)   # lied: it is a JPEG
    rows = notes.attachments(note_id)
    assert [r["id"] for r in rows] == [att_id]
    assert rows[0]["mime"] == "image/jpeg"
    assert rows[0]["size"] == len(JPEG)
    assert "data" not in rows[0]                      # metadata only
    assert notes.attachment(att_id)["data"] == JPEG
    assert notes.attachment(att_id + 100) is None


def test_attach_refuses_oversize_and_unknown_types_without_writing():
    import pytest
    from wren import filetypes
    notes.init_db()
    note_id = notes.save(1, "n", [])
    with pytest.raises(ValueError, match="too big"):
        notes.attach(note_id, "big.jpg", "image/jpeg", JPEG + b"\0" * filetypes.MAX_BYTES)
    with pytest.raises(ValueError, match="unsupported type"):
        notes.attach(note_id, "evil.jpg", "image/jpeg", b"MZ\x90\x00" + b"\0" * 32)
    assert notes.attachments(note_id) == []


def test_delete_removes_the_notes_attachments_too():
    notes.init_db()
    keep = notes.save(1, "keep", [])
    gone = notes.save(1, "gone", [])
    keep_att = notes.attach(keep, "a.pdf", "application/pdf", PDF)
    notes.attach(gone, "b.pdf", "application/pdf", PDF)
    assert notes.delete(gone) is True
    assert notes.attachments(gone) == []
    assert notes.attachment(keep_att) is not None


def test_attachments_come_back_in_insertion_order():
    notes.init_db()
    note_id = notes.save(1, "n", [])
    first = notes.attach(note_id, "1.pdf", "application/pdf", PDF)
    second = notes.attach(note_id, "2.pdf", "application/pdf", PDF)
    assert [r["id"] for r in notes.attachments(note_id)] == [first, second]


def test_max_files_per_reply_is_five():
    assert notes.MAX_FILES_PER_REPLY == 5
