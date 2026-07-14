import os, asyncio, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import MagicMock, AsyncMock
import discord
from wren import pins_plugin

def _pinned_message(content: str):
    m = MagicMock()
    m.content = content
    m.pin = AsyncMock()
    m.unpin = AsyncMock()
    return m

def _message(pins=None):
    message = MagicMock()
    message.channel.send = AsyncMock(return_value=_pinned_message(""))
    message.channel.pins = AsyncMock(return_value=pins or [])
    return message

def test_pin_note_empty_content_guarded():
    message = _message()
    asyncio.run(pins_plugin.handle("pin_note", message, None, 1, "  ", [], None, None))
    message.channel.send.assert_awaited_once_with("What should I pin?")

def test_pin_note_success_stays_silent():
    sent = _pinned_message("wifi password is 12345")
    message = _message()
    message.channel.send = AsyncMock(return_value=sent)
    asyncio.run(pins_plugin.handle("pin_note", message, None, 1, "wifi password is 12345", [], None, None))
    message.channel.send.assert_awaited_once_with("wifi password is 12345")
    sent.pin.assert_awaited_once()

def test_pin_note_failure_sends_error():
    sent = _pinned_message("wifi password is 12345")
    fake_response = MagicMock(status=400, reason="Bad Request")
    sent.pin = AsyncMock(side_effect=discord.HTTPException(fake_response, "too many pins"))
    message = _message()
    message.channel.send = AsyncMock(return_value=sent)
    asyncio.run(pins_plugin.handle("pin_note", message, None, 1, "wifi password is 12345", [], None, None))
    assert message.channel.send.await_count == 2
    last_call_text = message.channel.send.await_args_list[-1].args[0]
    assert "50-pin limit" in last_call_text

def test_unpin_note_empty_content_guarded():
    message = _message()
    asyncio.run(pins_plugin.handle("unpin_note", message, None, 1, "  ", [], None, None))
    message.channel.send.assert_awaited_once_with("Which pin do you want to remove?")

def test_unpin_note_no_match():
    message = _message(pins=[_pinned_message("wifi password is 12345")])
    asyncio.run(pins_plugin.handle("unpin_note", message, None, 1, "garage code", [], None, None))
    message.channel.send.assert_awaited_once_with("No pin found matching that.")

def test_unpin_note_single_match():
    target = _pinned_message("wifi password is 12345")
    message = _message(pins=[target])
    asyncio.run(pins_plugin.handle("unpin_note", message, None, 1, "wifi", [], None, None))
    target.unpin.assert_awaited_once()
    message.channel.send.assert_awaited_once_with("Unpinned: wifi password is 12345")

def test_unpin_note_multiple_matches():
    first = _pinned_message("wifi password is 12345")
    second = _pinned_message("wifi network name is HomeNet")
    message = _message(pins=[first, second])
    asyncio.run(pins_plugin.handle("unpin_note", message, None, 1, "wifi", [], None, None))
    first.unpin.assert_not_awaited()
    second.unpin.assert_not_awaited()
    sent_text = message.channel.send.call_args[0][0]
    assert "Found more than one match" in sent_text

def test_list_pins_empty():
    message = _message(pins=[])
    asyncio.run(pins_plugin.handle("list_pins", message, None, 1, "", [], None, None))
    message.channel.send.assert_awaited_once_with("Nothing pinned.")

def test_list_pins_multiple():
    first = _pinned_message("wifi password is 12345")
    second = _pinned_message("garage code is 6789")
    message = _message(pins=[first, second])
    asyncio.run(pins_plugin.handle("list_pins", message, None, 1, "", [], None, None))
    assert message.channel.send.await_count == 2
    sent_texts = [c.args[0] for c in message.channel.send.await_args_list]
    assert "📌 wifi password is 12345" in sent_texts
    assert "📌 garage code is 6789" in sent_texts
