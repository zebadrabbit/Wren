import os, asyncio, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import MagicMock, AsyncMock
from wren import contacts
from wren import contacts_plugin

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(contacts, "DB_PATH", str(tmp_path / "test.db"))
    contacts.init_db()

def _message():
    message = MagicMock()
    message.channel.send = AsyncMock()
    return message

# user_id=1 is OWNER_ID; user_id=2 is a non-owner whitelisted contact
def test_non_owner_cannot_add_contact():
    message = _message()
    asyncio.run(contacts_plugin.handle("add_contact", message, None, 2, "222222222222222222", [], "hubby", None))
    message.channel.send.assert_awaited_once_with("Only the owner can manage contacts.")
    assert contacts.all() == {}

def test_owner_adds_contact():
    message = _message()
    asyncio.run(contacts_plugin.handle("add_contact", message, None, 1, "222222222222222222", [], "hubby", None))
    message.channel.send.assert_awaited_once_with("Added hubby.")
    assert contacts.all() == {"hubby": 222222222222222222}

def test_owner_add_contact_non_numeric_id():
    message = _message()
    asyncio.run(contacts_plugin.handle("add_contact", message, None, 1, "not-a-number", [], "hubby", None))
    message.channel.send.assert_awaited_once_with("That doesn't look like a Discord ID.")
    assert contacts.all() == {}

def test_owner_add_contact_taken_alias():
    contacts.add("hubby", 222222222222222222)
    message = _message()
    asyncio.run(contacts_plugin.handle("add_contact", message, None, 1, "333333333333333333", [], "hubby", None))
    message.channel.send.assert_awaited_once_with("That name's already taken.")

def test_owner_add_contact_alias_matches_owner():
    message = _message()
    asyncio.run(contacts_plugin.handle("add_contact", message, None, 1, "222222222222222222", [], "owner", None))
    message.channel.send.assert_awaited_once_with("That name's already taken.")

def test_owner_removes_contact():
    contacts.add("hubby", 222222222222222222)
    message = _message()
    asyncio.run(contacts_plugin.handle("remove_contact", message, None, 1, "", [], "hubby", None))
    message.channel.send.assert_awaited_once_with("Removed hubby.")
    assert contacts.all() == {}

def test_owner_removes_unknown_contact():
    message = _message()
    asyncio.run(contacts_plugin.handle("remove_contact", message, None, 1, "", [], "nobody", None))
    message.channel.send.assert_awaited_once_with("No contact named nobody.")

def test_non_owner_cannot_remove_contact():
    contacts.add("hubby", 222222222222222222)
    message = _message()
    asyncio.run(contacts_plugin.handle("remove_contact", message, None, 2, "", [], "hubby", None))
    message.channel.send.assert_awaited_once_with("Only the owner can manage contacts.")
    assert contacts.all() == {"hubby": 222222222222222222}

def test_owner_lists_contacts_empty():
    message = _message()
    asyncio.run(contacts_plugin.handle("list_contacts", message, None, 1, "", [], None, None))
    message.channel.send.assert_awaited_once_with("No contacts yet.")

def test_owner_lists_contacts():
    contacts.add("hubby", 222222222222222222)
    contacts.add("kevin", 333333333333333333)
    message = _message()
    asyncio.run(contacts_plugin.handle("list_contacts", message, None, 1, "", [], None, None))
    message.channel.send.assert_awaited_once_with("hubby, kevin")

def test_non_owner_cannot_list_contacts():
    message = _message()
    asyncio.run(contacts_plugin.handle("list_contacts", message, None, 2, "", [], None, None))
    message.channel.send.assert_awaited_once_with("Only the owner can manage contacts.")
