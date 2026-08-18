import os, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")

from wren.skills import shopping_store as shopping

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
