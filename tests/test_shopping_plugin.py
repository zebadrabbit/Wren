import os, asyncio, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import AsyncMock, patch
from wren.skills import shopping_store as shopping
from wren.skills import shopping_skill as shopping_plugin
from wren import router
from wren import contacts
from wren.channel import Ctx, CollectingChannel
from wren.flourish import EMOTES

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("WREN_DB", str(tmp_path / "wren.db"))
    shopping.init_db()
    contacts.init_db()
    contacts.add("husband", 2)

def _assert_flourished(sent: str, prefix: str):
    assert sent.startswith(prefix + " ")
    assert sent.rsplit(" ", 1)[1] in EMOTES

def test_add_shopping_item_new():
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle("add_shopping_item", Ctx(user_id=1, channel=ch, content="potatoes")))
    _assert_flourished(ch.sent[-1], "Added potatoes.")

def test_add_shopping_item_dedup():
    shopping.add("potatoes", "owner")
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle("add_shopping_item", Ctx(user_id=1, channel=ch, content="potatoes")))
    assert ch.sent == ["Already on the list."]

def test_remove_shopping_item_found():
    shopping.add("milk", "owner")
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle("remove_shopping_item", Ctx(user_id=1, channel=ch, content="milk")))
    _assert_flourished(ch.sent[-1], "Got it, removed milk.")

def test_remove_shopping_item_not_found():
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle("remove_shopping_item", Ctx(user_id=1, channel=ch, content="milk")))
    assert ch.sent == ["milk wasn't on the list."]

def test_recall_shopping_empty():
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle("recall_shopping", Ctx(user_id=1, channel=ch, content="")))
    assert ch.sent == ["Shopping list is empty."]

def test_recall_shopping_with_items():
    shopping.add("potatoes", "owner")
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle("recall_shopping", Ctx(user_id=1, channel=ch, content="")))
    assert ch.sent == ["potatoes"]

def test_send_shopping_list_unknown_contact():
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle("send_shopping_list", Ctx(user_id=1, channel=ch, content="", person="stranger")))
    assert ch.sent == ["I don't know how to reach them."]

def test_send_shopping_list_empty_list():
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle("send_shopping_list", Ctx(user_id=1, channel=ch, content="", person="husband")))
    assert ch.sent == ["Nothing on the list to send."]

def test_send_shopping_list_success():
    shopping.add("potatoes", "owner")
    ch = CollectingChannel()
    with patch.object(router, "notify_name", new=AsyncMock(return_value=True)):
        asyncio.run(shopping_plugin.handle("send_shopping_list", Ctx(user_id=1, channel=ch, content="", person="husband")))
    _assert_flourished(ch.sent[-1], "Sent to husband.")

def test_send_shopping_list_delivery_failure():
    shopping.add("potatoes", "owner")
    ch = CollectingChannel()
    with patch.object(router, "notify_name", new=AsyncMock(return_value=False)):
        asyncio.run(shopping_plugin.handle("send_shopping_list", Ctx(user_id=1, channel=ch, content="", person="husband")))
    assert ch.sent == ["Couldn't reach husband — they may be unreachable right now."]
