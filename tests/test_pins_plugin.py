import os, asyncio, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from wren.skills import pins_store as pins
from wren.skills import pins_skill as pins_plugin
from wren.channel import Ctx, CollectingChannel
from wren.flourish import EMOTES

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("WREN_DB", str(tmp_path / "wren.db"))
    pins.init_db()

def _assert_flourished(sent: str, prefix: str):
    assert sent.startswith(prefix + " ")
    assert sent.rsplit(" ", 1)[1] in EMOTES

def test_pin_note_empty_content_guarded():
    ch = CollectingChannel()
    asyncio.run(pins_plugin.handle("pin_note", Ctx(user_id=1, channel=ch, content="  ")))
    assert ch.sent == ["What should I pin?"]
    assert pins.all(1) == []

def test_pin_note_success_stores_and_confirms():
    ch = CollectingChannel()
    asyncio.run(pins_plugin.handle("pin_note", Ctx(user_id=1, channel=ch, content="wifi password is 12345")))
    _assert_flourished(ch.sent[0], "Pinned: wifi password is 12345")
    assert pins.all(1)[0]["content"] == "wifi password is 12345"

def test_unpin_note_empty_content_guarded():
    ch = CollectingChannel()
    asyncio.run(pins_plugin.handle("unpin_note", Ctx(user_id=1, channel=ch, content="  ")))
    assert ch.sent == ["Which pin do you want to remove?"]

def test_unpin_note_no_match():
    pins.save(1, "wifi password is 12345")
    ch = CollectingChannel()
    asyncio.run(pins_plugin.handle("unpin_note", Ctx(user_id=1, channel=ch, content="garage code")))
    assert ch.sent == ["No pin found matching that."]
    assert len(pins.all(1)) == 1

def test_unpin_note_single_match():
    pins.save(1, "wifi password is 12345")
    ch = CollectingChannel()
    asyncio.run(pins_plugin.handle("unpin_note", Ctx(user_id=1, channel=ch, content="wifi")))
    _assert_flourished(ch.sent[0], "Unpinned: wifi password is 12345")
    assert pins.all(1) == []

def test_unpin_note_multiple_matches():
    pins.save(1, "wifi password is 12345")
    pins.save(1, "wifi network name is HomeNet")
    ch = CollectingChannel()
    asyncio.run(pins_plugin.handle("unpin_note", Ctx(user_id=1, channel=ch, content="wifi")))
    assert "Found more than one match" in ch.sent[0]
    assert len(pins.all(1)) == 2

def test_list_pins_empty():
    ch = CollectingChannel()
    asyncio.run(pins_plugin.handle("list_pins", Ctx(user_id=1, channel=ch, content="")))
    assert ch.sent == ["Nothing pinned."]

def test_list_pins_multiple():
    pins.save(1, "wifi password is 12345")
    pins.save(1, "garage code is 6789")
    ch = CollectingChannel()
    asyncio.run(pins_plugin.handle("list_pins", Ctx(user_id=1, channel=ch, content="")))
    assert len(ch.sent) == 2
    assert "📌 wifi password is 12345" in ch.sent
    assert "📌 garage code is 6789" in ch.sent

def test_list_pins_owner_isolation():
    pins.save(1, "my pin")
    pins.save(2, "their pin")
    ch = CollectingChannel()
    asyncio.run(pins_plugin.handle("list_pins", Ctx(user_id=1, channel=ch, content="")))
    assert ch.sent == ["📌 my pin"]
