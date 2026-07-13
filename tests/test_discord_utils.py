import os, asyncio
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("HUSBAND_ID", "2")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import MagicMock, AsyncMock
import discord
from wren import discord_utils

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
