import os, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")
os.environ.setdefault("TIMEZONE", "UTC")

from wren.skills import memory_store as memory

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("WREN_DB", str(tmp_path / "wren.db"))
    memory.init_db()

def test_save_and_read_back():
    memory.save(1, "preference", "dislikes cilantro")
    rows = memory.all_for(1)
    assert len(rows) == 1
    assert rows[0]["fact"] == "dislikes cilantro"
    assert rows[0]["category"] == "preference"
    assert rows[0]["mention_count"] == 1

def test_memories_are_per_owner():
    memory.save(1, "fact", "mine")
    memory.save(2, "fact", "theirs")
    assert [r["fact"] for r in memory.all_for(1)] == ["mine"]

def test_bump_increments_and_touches_last_seen():
    mid = memory.save(1, "fact", "runs a home server")
    before = memory.all_for(1)[0]["last_seen_at"]
    memory.bump(mid)
    row = memory.all_for(1)[0]
    assert row["mention_count"] == 2
    assert row["last_seen_at"] >= before

def test_all_for_orders_by_mention_count():
    memory.save(1, "fact", "quiet one")
    loud = memory.save(1, "fact", "loud one")
    memory.bump(loud)
    assert memory.all_for(1)[0]["fact"] == "loud one"

def test_forget_removes_and_reports():
    mid = memory.save(1, "fact", "temporary")
    assert memory.forget(mid) is True
    assert memory.all_for(1) == []
    assert memory.forget(mid) is False

def test_queue_drains_once():
    memory.enqueue(1, "my sister Kate lives in Denver")
    drained = memory.drain()
    assert [r["text"] for r in drained] == ["my sister Kate lives in Denver"]
    assert int(drained[0]["owner_id"]) == 1
    assert memory.drain() == []          # delete-on-read: never served twice

def test_drain_respects_limit_and_keeps_the_rest():
    for i in range(3):
        memory.enqueue(1, f"turn {i}")
    assert len(memory.drain(limit=2)) == 2
    assert [r["text"] for r in memory.drain()] == ["turn 2"]
