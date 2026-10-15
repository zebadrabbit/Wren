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


# --- one id per surface (2026-09-26), stored for good (2026-10-14) -----------
# A contact's Wren id — what every note, reminder and pin is filed under — is
# the id they were first added with. A second surface maps onto it; it never
# moves.

def test_adding_discord_later_keeps_the_id_they_were_added_with():
    contacts.add("kim", 55, surface="telegram")
    assert contacts.set_id("kim", "discord", 123456789012345678) is True
    assert contacts.all() == {"kim": 55}
    assert contacts.wren_id("discord", 123456789012345678) == 55
    assert contacts.surface_id("discord", 55) == 123456789012345678
    assert contacts.surfaces_of(55) == ["discord", "telegram"]

def test_init_db_backfills_wren_id_for_a_table_from_before_it_was_stored(tmp_path, monkeypatch):
    from wren import db
    monkeypatch.setenv("WREN_DB", str(tmp_path / "old.db"))
    with db.conn() as con:
        con.executescript("""
            CREATE TABLE contacts (alias TEXT PRIMARY KEY, discord_id TEXT UNIQUE,
                                   telegram_id TEXT UNIQUE, added_at TEXT NOT NULL);
            INSERT INTO contacts VALUES ('hubby', '222222222222222222', '42', 'x');
            INSERT INTO contacts VALUES ('kim', NULL, '55', 'x');
        """)
    contacts.init_db()
    # the id each person had under the old COALESCE rule, so nothing moves
    assert contacts.all() == {"hubby": 222222222222222222, "kim": 55}
    contacts.init_db()                                        # idempotent
    assert contacts.all() == {"hubby": 222222222222222222, "kim": 55}

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

# --- string ids per surface (2026-10-15) ---------------------------------------
# Slack and Matrix ids are not numbers. The per-surface id is a string in the
# store's interface; the Wren id stays an int, minted for string-id surfaces.

def test_surfaces_and_shapes():
    assert contacts.SURFACES == ("discord", "telegram", "slack", "matrix")
    assert contacts.NUMERIC == frozenset({"discord", "telegram"})
    assert contacts.ID_SHAPE["slack"].fullmatch("U0123ABCDEF")
    assert contacts.ID_SHAPE["matrix"].fullmatch("@kim:home.net")
    assert not contacts.ID_SHAPE["slack"].fullmatch("u0123abcdef")
    assert not contacts.ID_SHAPE["discord"].fullmatch("U0123ABCDEF")

def test_register_refuses_a_malformed_id():
    for surface, raw in [("slack", "u0123abcdef"), ("slack", "U01 23ABCDEF"),
                         ("matrix", "kim:home.net"), ("discord", "12ab"), ("telegram", "")]:
        with pytest.raises(contacts.Invalid, match=f"look like a {surface} id"):
            contacts.register("kim", surface, raw)
    assert contacts.all() == {}

def test_slack_first_contact_gets_a_minted_stable_wren_id():
    assert contacts.register("kim", "slack", "U0123ABCDEF") is True
    wid = contacts.all()["kim"]
    assert isinstance(wid, int) and 0 < wid < 2 ** 56
    assert wid == contacts.mint("slack", "U0123ABCDEF")
    assert contacts.wren_id("slack", "U0123ABCDEF") == wid
    assert contacts.surface_id("slack", wid) == "U0123ABCDEF"
    assert contacts.get("kim") == {"alias": "kim", "discord": None, "telegram": None,
                                   "slack": "U0123ABCDEF", "matrix": None}
    assert contacts.surfaces_of(wid) == ["slack"]
    # removed and re-added: same id, so their notes are still theirs
    assert contacts.remove("kim")
    contacts.register("kim", "slack", "U0123ABCDEF")
    assert contacts.all()["kim"] == wid

def test_mint_differs_by_surface_and_id():
    assert contacts.mint("slack", "U0123ABCDEF") != contacts.mint("matrix", "U0123ABCDEF")
    assert contacts.mint("slack", "U0123ABCDEF") != contacts.mint("slack", "U0123ABCDEG")

def test_numeric_surfaces_keep_native_ints():
    contacts.register("hubby", "discord", "222222222222222222")
    contacts.register("hubby", "matrix", "@hubby:home.net")
    assert contacts.all() == {"hubby": 222222222222222222}
    assert contacts.get("hubby")["discord"] == 222222222222222222
    assert contacts.get("hubby")["matrix"] == "@hubby:home.net"
    assert contacts.surface_id("matrix", 222222222222222222) == "@hubby:home.net"
    assert contacts.wren_id("matrix", "@hubby:home.net") == 222222222222222222
    assert contacts.surfaces_of(222222222222222222) == ["discord", "matrix"]

def test_register_strips_the_id_before_checking_it():
    contacts.register("kim", "telegram", " 55 ")
    assert contacts.all() == {"kim": 55}

def test_init_db_adds_the_new_columns_to_an_existing_table(tmp_path, monkeypatch):
    from wren import db
    monkeypatch.setenv("WREN_DB", str(tmp_path / "old.db"))
    with db.conn() as con:
        con.executescript("""
            CREATE TABLE contacts (alias TEXT PRIMARY KEY, discord_id TEXT UNIQUE,
                                   telegram_id TEXT UNIQUE, wren_id TEXT, added_at TEXT NOT NULL);
            INSERT INTO contacts VALUES ('kim', NULL, '55', '55', 'x');
        """)
    contacts.init_db()
    contacts.init_db()                                        # idempotent
    assert contacts.get("kim") == {"alias": "kim", "discord": None, "telegram": 55,
                                   "slack": None, "matrix": None}
    assert contacts.set_id("kim", "slack", "U0123ABCDEF") is True
    assert contacts.surface_id("slack", 55) == "U0123ABCDEF"

def test_a_surface_id_is_unique_across_people():
    contacts.register("kim", "slack", "U0123ABCDEF")
    with pytest.raises(contacts.Taken, match="another name"):
        contacts.register("sam", "slack", "U0123ABCDEF")


def test_non_ascii_digits_are_refused():
    with pytest.raises(contacts.Invalid):
        contacts.register("kim", "telegram", "١٢")
