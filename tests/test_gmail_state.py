import os, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from datetime import datetime, timedelta, timezone
from wren.communication import gmail_state


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("WREN_DB", str(tmp_path / "wren.db"))
    gmail_state.init_db()


def test_recent_is_per_contact_newest_first():
    gmail_state.add("owner", "alice@example.com", "Invoice")
    gmail_state.add("hubby", "bob@example.com", "Fishing")
    gmail_state.add("owner", "carol@example.com", "Party")
    assert [(r["sender"], r["subject"]) for r in gmail_state.recent("owner")] == [
        ("carol@example.com", "Party"), ("alice@example.com", "Invoice")]


def test_only_the_last_day_counts_and_older_rows_are_pruned(monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(timespec="seconds")
    real_now = gmail_state._now
    monkeypatch.setattr(gmail_state, "_now", lambda: old)
    gmail_state.add("owner", "alice@example.com", "Yesterday's")
    # only the clock goes back: monkeypatch.undo() would also drop the scratch WREN_DB
    monkeypatch.setattr(gmail_state, "_now", real_now)
    gmail_state.add("owner", "bob@example.com", "Today's")
    assert [r["subject"] for r in gmail_state.recent("owner")] == ["Today's"]
