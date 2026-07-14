import os, asyncio
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import MagicMock, AsyncMock
from wren.progress import Progress

def _channel():
    sent_message = MagicMock()
    sent_message.edit = AsyncMock()
    channel = MagicMock()
    channel.send = AsyncMock(return_value=sent_message)
    return channel, sent_message

def test_start_sends_initial_text_and_returns_progress():
    channel, sent_message = _channel()
    progress = asyncio.run(Progress.start(channel, "Thinking…"))
    channel.send.assert_awaited_once_with("Thinking…")
    assert isinstance(progress, Progress)

def test_update_edits_the_same_message():
    channel, sent_message = _channel()
    progress = asyncio.run(Progress.start(channel, "Thinking…"))
    asyncio.run(progress.update("Almost done…"))
    sent_message.edit.assert_awaited_once_with(content="Almost done…")
    channel.send.assert_awaited_once()  # no second send from update()

def test_fail_edits_the_same_message():
    channel, sent_message = _channel()
    progress = asyncio.run(Progress.start(channel, "Thinking…"))
    asyncio.run(progress.fail("Something went wrong."))
    sent_message.edit.assert_awaited_once_with(content="Something went wrong.")
    channel.send.assert_awaited_once()  # no second send from fail()

def test_update_then_fail_both_target_same_message():
    channel, sent_message = _channel()
    progress = asyncio.run(Progress.start(channel, "Thinking…"))
    asyncio.run(progress.update("Almost done…"))
    asyncio.run(progress.fail("Something went wrong."))
    assert sent_message.edit.await_count == 2
    sent_message.edit.assert_awaited_with(content="Something went wrong.")
