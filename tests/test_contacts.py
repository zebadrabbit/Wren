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
