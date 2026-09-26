import os, sqlite3, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from wren import contacts

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("WREN_DB", str(tmp_path / "wren.db"))
    contacts.init_db()

def test_add_and_all():
    contacts.add("hubby", 222222222222222222)
    assert contacts.all() == {"hubby": 222222222222222222}

def test_add_lowercases_alias():
    contacts.add("Kevin", 333333333333333333)
    assert contacts.all() == {"kevin": 333333333333333333}

def test_add_duplicate_alias_raises():
    contacts.add("hubby", 222222222222222222)
    with pytest.raises(sqlite3.IntegrityError):
        contacts.add("hubby", 444444444444444444)

def test_add_duplicate_discord_id_raises():
    contacts.add("hubby", 222222222222222222)
    with pytest.raises(sqlite3.IntegrityError):
        contacts.add("kevin", 222222222222222222)

def test_remove_existing_returns_true():
    contacts.add("hubby", 222222222222222222)
    assert contacts.remove("hubby") is True
    assert contacts.all() == {}

def test_remove_is_case_insensitive():
    contacts.add("hubby", 222222222222222222)
    assert contacts.remove("HUBBY") is True

def test_remove_nonexistent_returns_false():
    assert contacts.remove("nobody") is False

def test_all_empty_by_default():
    assert contacts.all() == {}

def test_all_returns_multiple():
    contacts.add("hubby", 222222222222222222)
    contacts.add("kevin", 333333333333333333)
    assert contacts.all() == {"hubby": 222222222222222222, "kevin": 333333333333333333}


# --- one id per surface (2026-09-26) ------------------------------------------
# A contact's Wren id — what every note, reminder and pin is filed under — is
# their Discord id when they have one, otherwise their Telegram id: the rule the
# README already states for a Telegram-first install, applied per person.

def test_telegram_only_contact_is_keyed_by_their_telegram_id():
    contacts.add("hubby", 42, surface="telegram")
    assert contacts.all() == {"hubby": 42}
    assert contacts.wren_id("telegram", 42) == 42
    assert contacts.surface_id("telegram", 42) == 42
    assert contacts.surfaces_of(42) == ["telegram"]

def test_a_second_surface_id_maps_onto_the_same_wren_id():
    contacts.add("hubby", 222222222222222222)
    assert contacts.set_id("hubby", "telegram", 42) is True
    assert contacts.all() == {"hubby": 222222222222222222}   # the Discord id stays the key
    assert contacts.wren_id("telegram", 42) == 222222222222222222
    assert contacts.surface_id("telegram", 222222222222222222) == 42
    assert contacts.surface_id("discord", 222222222222222222) == 222222222222222222
    assert contacts.surfaces_of(222222222222222222) == ["discord", "telegram"]

def test_set_id_on_an_unknown_alias_is_false():
    assert contacts.set_id("nobody", "telegram", 42) is False

def test_lookups_for_an_unknown_id_are_none_and_empty():
    assert contacts.wren_id("telegram", 7) is None
    assert contacts.surface_id("telegram", 7) is None
    assert contacts.surfaces_of(7) == []

def test_duplicate_telegram_id_raises():
    contacts.add("hubby", 42, surface="telegram")
    with pytest.raises(sqlite3.IntegrityError):
        contacts.add("kevin", 42, surface="telegram")

def test_unknown_surface_is_rejected():
    with pytest.raises(ValueError):
        contacts.add("hubby", 42, surface="carrier-pigeon")

def test_a_pre_2026_09_26_table_is_migrated_with_its_rows(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setenv("WREN_DB", str(path))
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE contacts (alias TEXT PRIMARY KEY, discord_id TEXT NOT NULL UNIQUE, added_at TEXT NOT NULL)")
    con.execute("INSERT INTO contacts VALUES ('hubby', '222222222222222222', '2026-07-13T00:00:00')")
    con.commit(); con.close()
    contacts.init_db()
    assert contacts.all() == {"hubby": 222222222222222222}
    assert contacts.set_id("hubby", "telegram", 42) is True
    contacts.add("kevin", 43, surface="telegram")   # NOT NULL on discord_id is gone
    assert contacts.all() == {"hubby": 222222222222222222, "kevin": 43}
