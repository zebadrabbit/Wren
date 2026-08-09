import os, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")

from wren.skills import pins_store as pins

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("WREN_DB", str(tmp_path / "wren.db"))
    pins.init_db()

def test_save_and_all():
    pin_id = pins.save(1, "wifi password is 12345")
    assert isinstance(pin_id, int)
    results = pins.all(1)
    assert len(results) == 1
    assert results[0]["id"] == pin_id
    assert results[0]["content"] == "wifi password is 12345"
    assert results[0]["created_at"]

def test_all_empty_for_new_owner():
    assert pins.all(1) == []

def test_owner_isolation():
    pins.save(1, "my pin")
    pins.save(2, "their pin")
    assert [p["content"] for p in pins.all(1)] == ["my pin"]
    assert [p["content"] for p in pins.all(2)] == ["their pin"]

def test_find_matches_substring_case_insensitive():
    pins.save(1, "WiFi password is 12345")
    pins.save(1, "garage code is 6789")
    results = pins.find(1, "wifi")
    assert len(results) == 1
    assert results[0]["content"] == "WiFi password is 12345"

def test_find_no_match_returns_empty():
    pins.save(1, "wifi password is 12345")
    assert pins.find(1, "spaceship") == []

def test_find_multiple_matches():
    pins.save(1, "wifi password is 12345")
    pins.save(1, "wifi network name is HomeNet")
    assert len(pins.find(1, "wifi")) == 2

def test_find_respects_owner_scope():
    pins.save(1, "wifi for me")
    pins.save(2, "wifi for them")
    results = pins.find(1, "wifi")
    assert len(results) == 1
    assert results[0]["content"] == "wifi for me"

def test_delete_existing_pin():
    pin_id = pins.save(1, "wifi password is 12345")
    assert pins.delete(pin_id) is True
    assert pins.all(1) == []

def test_delete_nonexistent_returns_false():
    assert pins.delete(9999) is False

def test_delete_leaves_other_pins():
    keep = pins.save(1, "garage code is 6789")
    drop = pins.save(1, "wifi password is 12345")
    assert pins.delete(drop) is True
    assert [p["id"] for p in pins.all(1)] == [keep]
