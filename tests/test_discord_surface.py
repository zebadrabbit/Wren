import os, asyncio
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import MagicMock, AsyncMock
import pytest
import discord
from wren.communication import discord_utils
from wren import contacts

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("WREN_DB", str(tmp_path / "wren.db"))
    contacts.init_db()
    contacts.add("husband", 2)

def test_notify_success():
    client = MagicMock()
    target_user = MagicMock()
    target_user.send = AsyncMock()
    client.fetch_user = AsyncMock(return_value=target_user)

    result = asyncio.run(discord_utils.notify(client, "owner", "hello"))

    assert result is True
    target_user.send.assert_awaited_once_with("hello")

def test_notify_unknown_contact_returns_false():
    client = MagicMock()
    client.fetch_user = AsyncMock()

    result = asyncio.run(discord_utils.notify(client, "stranger", "hello"))

    assert result is False
    client.fetch_user.assert_not_called()

def test_notify_dm_forbidden_returns_false():
    client = MagicMock()
    fake_response = MagicMock(status=403, reason="Forbidden")
    client.fetch_user = AsyncMock(side_effect=discord.Forbidden(fake_response, "Cannot send messages to this user"))

    result = asyncio.run(discord_utils.notify(client, "owner", "hello"))

    assert result is False

def test_notify_dm_not_found_returns_false():
    client = MagicMock()
    fake_response = MagicMock(status=404, reason="Not Found")
    client.fetch_user = AsyncMock(side_effect=discord.NotFound(fake_response, "Unknown user"))

    result = asyncio.run(discord_utils.notify(client, "husband", "hello"))

    assert result is False

def test_notify_id_success():
    client = MagicMock()
    target_user = MagicMock()
    target_user.send = AsyncMock()
    client.fetch_user = AsyncMock(return_value=target_user)

    result = asyncio.run(discord_utils.notify_id(client, 42, "hello"))

    assert result is True
    target_user.send.assert_awaited_once_with("hello")
    client.fetch_user.assert_awaited_once_with(42)

def test_notify_id_forbidden_returns_false():
    client = MagicMock()
    fake_response = MagicMock(status=403, reason="Forbidden")
    client.fetch_user = AsyncMock(side_effect=discord.Forbidden(fake_response, "Cannot send messages to this user"))

    result = asyncio.run(discord_utils.notify_id(client, 42, "hello"))

    assert result is False

def test_notify_id_not_found_returns_false():
    client = MagicMock()
    fake_response = MagicMock(status=404, reason="Not Found")
    client.fetch_user = AsyncMock(side_effect=discord.NotFound(fake_response, "Unknown user"))

    result = asyncio.run(discord_utils.notify_id(client, 42, "hello"))

    assert result is False

def _fake_message(author_id: int, content: str):
    msg = MagicMock()
    msg.author.id = author_id
    msg.content = content
    return msg

def test_history_to_messages_empty_list():
    assert discord_utils.history_to_messages([], bot_user_id=99) == []

def test_history_to_messages_reverses_to_chronological_order():
    # channel.history() yields newest-first; input here is [newest, ..., oldest]
    newest = _fake_message(1, "second thing I said")
    oldest = _fake_message(1, "first thing I said")
    result = discord_utils.history_to_messages([newest, oldest], bot_user_id=99)
    assert result == [
        {"role": "user", "content": "first thing I said"},
        {"role": "user", "content": "second thing I said"},
    ]

def test_history_to_messages_assigns_assistant_role_to_bot_author():
    bot_msg = _fake_message(99, "Wren's reply")
    user_msg = _fake_message(1, "user's message")
    result = discord_utils.history_to_messages([user_msg, bot_msg], bot_user_id=99)
    assert result == [
        {"role": "assistant", "content": "Wren's reply"},
        {"role": "user", "content": "user's message"},
    ]

def test_history_to_messages_skips_empty_content():
    blank = _fake_message(1, "   ")
    real = _fake_message(1, "hello")
    result = discord_utils.history_to_messages([real, blank], bot_user_id=99)
    assert result == [{"role": "user", "content": "hello"}]
