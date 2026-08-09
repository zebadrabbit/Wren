import os
import sqlite3

from wren import db


def test_path_returns_wren_db_env_var(monkeypatch, tmp_path):
    target = str(tmp_path / "somewhere.db")
    monkeypatch.setenv("WREN_DB", target)
    assert db.path() == target


def test_path_defaults_to_wren_db_when_unset(monkeypatch):
    # NB: assert on the string only — never open it, or the suite would
    # create a real wren.db in the repo root.
    monkeypatch.delenv("WREN_DB", raising=False)
    assert db.path() == "wren.db"


def test_path_defaults_to_wren_db_when_empty(monkeypatch):
    monkeypatch.setenv("WREN_DB", "")
    assert db.path() == "wren.db"


def test_default_path_constant():
    assert db.DEFAULT_PATH == "wren.db"


def test_path_is_read_at_call_time_not_import_time(monkeypatch, tmp_path):
    # wren.db was imported at the top of this module; an env var set now must
    # still be visible, otherwise conftest's per-test WREN_DB would be ignored
    # and tests would share (or clobber) the real database.
    later = str(tmp_path / "set-after-import.db")
    monkeypatch.setenv("WREN_DB", later)
    assert db.path() == later


def test_path_tracks_further_changes_to_the_env_var(monkeypatch, tmp_path):
    first, second = str(tmp_path / "one.db"), str(tmp_path / "two.db")
    monkeypatch.setenv("WREN_DB", first)
    assert db.path() == first
    monkeypatch.setenv("WREN_DB", second)
    assert db.path() == second


def test_conn_returns_a_sqlite_connection(monkeypatch, tmp_path):
    monkeypatch.setenv("WREN_DB", str(tmp_path / "wren.db"))
    with db.conn() as con:
        assert isinstance(con, sqlite3.Connection)


def test_conn_opens_the_file_wren_db_points_at(monkeypatch, tmp_path):
    target = tmp_path / "pointed-at.db"
    monkeypatch.setenv("WREN_DB", str(target))
    with db.conn() as con:
        con.execute("CREATE TABLE t (x INTEGER)")
        con.execute("INSERT INTO t VALUES (1)")
    assert target.exists()
    with sqlite3.connect(str(target)) as raw:
        assert raw.execute("SELECT x FROM t").fetchall() == [(1,)]


def test_conn_follows_a_changed_wren_db(monkeypatch, tmp_path):
    first, second = tmp_path / "first.db", tmp_path / "second.db"
    monkeypatch.setenv("WREN_DB", str(first))
    with db.conn() as con:
        con.execute("CREATE TABLE only_in_first (x INTEGER)")
    monkeypatch.setenv("WREN_DB", str(second))
    with db.conn() as con:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE name='only_in_first'"
        ).fetchall()
    assert rows == []
    assert second.exists()


def test_conn_attaches_the_path_not_the_default(monkeypatch, tmp_path):
    # ask SQLite itself which file it opened, so this holds regardless of
    # whether a wren.db happens to exist in the working directory.
    target = tmp_path / "chosen.db"
    monkeypatch.setenv("WREN_DB", str(target))
    with db.conn() as con:
        opened = con.execute("PRAGMA database_list").fetchone()[2]
    assert os.path.realpath(opened) == os.path.realpath(str(target))
