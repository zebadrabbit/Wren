import os, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")

from wren.skills import shopping_store as shopping
from wren import db

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("WREN_DB", str(tmp_path / "wren.db"))
    shopping.init_db()

def test_add_and_active_items():
    _, was_new = shopping.add("Potatoes", "owner")
    assert was_new is True
    items = shopping.active_items()
    assert len(items) == 1
    assert items[0]["item"] == "potatoes"
    assert items[0]["original_text"] == "Potatoes"
    assert items[0]["added_by"] == "owner"

def test_add_dedupes_active_item():
    id1, new1 = shopping.add("potatoes", "owner")
    id2, new2 = shopping.add("Potatoes ", "husband")
    assert new1 is True
    assert new2 is False
    assert id1 == id2
    assert len(shopping.active_items()) == 1

def test_remove_active_item():
    shopping.add("milk", "owner")
    removed = shopping.remove("Milk")
    assert removed is True
    assert shopping.active_items() == []

def test_remove_not_active_returns_false():
    removed = shopping.remove("nonexistent")
    assert removed is False

def test_removed_item_can_be_readded():
    shopping.add("eggs", "owner")
    shopping.remove("eggs")
    _, was_new = shopping.add("eggs", "owner")
    assert was_new is True
    assert len(shopping.active_items()) == 1

def test_common_items_threshold():
    shopping.add("bread", "owner")
    shopping.remove("bread")
    shopping.add("bread", "owner")
    shopping.remove("bread")
    shopping.add("bread", "owner")
    common = shopping.common_items(threshold=3)
    assert len(common) == 1
    assert common[0]["item"] == "bread"
    assert common[0]["count"] == 3

def test_common_items_below_threshold_excluded():
    shopping.add("rare item", "owner")
    common = shopping.common_items(threshold=3)
    assert common == []

def test_common_items_counts_removed_items():
    for _ in range(3):
        shopping.add("butter", "owner")
        shopping.remove("butter")
    common = shopping.common_items(threshold=3)
    assert any(c["item"] == "butter" and c["count"] == 3 for c in common)


def test_clear_removes_every_active_item():
    shopping.add("milk", "owner")
    shopping.add("eggs", "owner")
    assert shopping.clear() == 2
    assert shopping.active_items() == []

def test_clear_on_empty_list_returns_zero():
    assert shopping.clear() == 0

def test_clear_keeps_rows_for_common_items():
    # The cleared items must still count toward "you often get" -- clear() is a
    # status flip precisely so the suggestion history survives it.
    for _ in range(3):
        shopping.add("milk", "owner")
        shopping.clear()
    assert [c["item"] for c in shopping.common_items()] == ["milk"]


def test_restore_puts_the_most_recently_removed_item_back():
    shopping.add("milk", "owner")
    shopping.remove("milk")
    assert shopping.restore("milk") is True
    assert [i["original_text"] for i in shopping.active_items()] == ["milk"]


def test_restore_reports_false_when_there_is_nothing_to_put_back():
    assert shopping.restore("milk") is False
    shopping.add("milk", "owner")
    assert shopping.restore("milk") is False      # it is active, not removed


def test_restore_reuses_the_row_instead_of_inserting_a_second_one():
    # The reason restore() exists rather than calling add() again: add() only
    # matches active rows, so on a removed item it INSERTs. That extra row
    # would count toward common_items and quietly make a removed-then-restored
    # item look more frequently bought than it is.
    shopping.add("milk", "owner")
    shopping.remove("milk")
    shopping.restore("milk")

    with db.conn() as con:
        rows = con.execute("SELECT count(*) FROM shopping_items WHERE item='milk'").fetchone()[0]
    assert rows == 1


def test_restore_preserves_who_added_it():
    shopping.add("milk", "ann")
    shopping.remove("milk")
    shopping.restore("milk")
    assert shopping.active_items()[0]["added_by"] == "ann"


def test_a_remove_then_restore_does_not_inflate_the_suggestions():
    # common_items() drives "You often get: ...". Undo must not be a purchase.
    for _ in range(3):
        shopping.add("milk", "owner")
        shopping.remove("milk")
    before = [c["count"] for c in shopping.common_items(threshold=1) if c["item"] == "milk"][0]
    shopping.restore("milk")
    shopping.remove("milk")
    after = [c["count"] for c in shopping.common_items(threshold=1) if c["item"] == "milk"][0]
    assert after == before
