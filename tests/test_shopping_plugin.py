import os, asyncio, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("HUSBAND_ID", "2")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import MagicMock, AsyncMock, patch
from wren import shopping
from wren import shopping_plugin
from wren import discord_utils
from wren import contacts

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(shopping, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(contacts, "DB_PATH", str(tmp_path / "test_contacts.db"))
    shopping.init_db()
    contacts.init_db()
    contacts.add("husband", 2)

def _message():
    message = MagicMock()
    message.channel.send = AsyncMock()
    return message

def test_add_shopping_item_new():
    message = _message()
    asyncio.run(shopping_plugin.handle("add_shopping_item", message, None, 1, "potatoes", [], None, None))
    message.channel.send.assert_awaited_once_with("Added potatoes.")

def test_add_shopping_item_dedup():
    shopping.add("potatoes", "owner")
    message = _message()
    asyncio.run(shopping_plugin.handle("add_shopping_item", message, None, 1, "potatoes", [], None, None))
    message.channel.send.assert_awaited_once_with("Already on the list.")

def test_remove_shopping_item_found():
    shopping.add("milk", "owner")
    message = _message()
    asyncio.run(shopping_plugin.handle("remove_shopping_item", message, None, 1, "milk", [], None, None))
    message.channel.send.assert_awaited_once_with("Got it, removed milk.")

def test_remove_shopping_item_not_found():
    message = _message()
    asyncio.run(shopping_plugin.handle("remove_shopping_item", message, None, 1, "milk", [], None, None))
    message.channel.send.assert_awaited_once_with("milk wasn't on the list.")

def test_recall_shopping_empty():
    message = _message()
    asyncio.run(shopping_plugin.handle("recall_shopping", message, None, 1, "", [], None, None))
    message.channel.send.assert_awaited_once_with("Shopping list is empty.")

def test_recall_shopping_with_items():
    shopping.add("potatoes", "owner")
    message = _message()
    asyncio.run(shopping_plugin.handle("recall_shopping", message, None, 1, "", [], None, None))
    message.channel.send.assert_awaited_once_with("potatoes")

def test_send_shopping_list_unknown_contact():
    message = _message()
    asyncio.run(shopping_plugin.handle("send_shopping_list", message, None, 1, "", [], "stranger", None))
    message.channel.send.assert_awaited_once_with("I don't know how to reach them.")

def test_send_shopping_list_empty_list():
    message = _message()
    asyncio.run(shopping_plugin.handle("send_shopping_list", message, None, 1, "", [], "husband", None))
    message.channel.send.assert_awaited_once_with("Nothing on the list to send.")

def test_send_shopping_list_success():
    shopping.add("potatoes", "owner")
    message = _message()
    with patch.object(discord_utils, "notify", new=AsyncMock(return_value=True)):
        asyncio.run(shopping_plugin.handle("send_shopping_list", message, None, 1, "", [], "husband", None))
    message.channel.send.assert_awaited_once_with("Sent to husband.")

def test_send_shopping_list_dm_failure():
    shopping.add("potatoes", "owner")
    message = _message()
    with patch.object(discord_utils, "notify", new=AsyncMock(return_value=False)):
        asyncio.run(shopping_plugin.handle("send_shopping_list", message, None, 1, "", [], "husband", None))
    message.channel.send.assert_awaited_once_with("Couldn't reach husband — their DMs may be closed.")
