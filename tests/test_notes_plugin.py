import os, asyncio, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("HUSBAND_ID", "2")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import MagicMock, AsyncMock, patch
import notes
import brain
import notes_plugin

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(notes, "DB_PATH", str(tmp_path / "test.db"))
    notes.init_db()

def _message():
    message = MagicMock()
    message.channel.send = AsyncMock()
    return message

def test_save_note():
    message = _message()
    asyncio.run(notes_plugin.handle("save_note", message, None, 1, "buy milk", ["grocery"], None))
    message.channel.send.assert_awaited_once_with("Saved.")
    assert notes.list_recent(1)[0]["content"] == "buy milk"

def test_recall_notes_no_matches():
    message = _message()
    asyncio.run(notes_plugin.handle("recall_notes", message, None, 1, "milk", [], None))
    message.channel.send.assert_awaited_once_with("No notes found.")

def test_recall_notes_with_matches():
    notes.save(1, "buy milk", ["grocery"])
    message = _message()
    with patch.object(brain, "recall", return_value="You need milk."):
        asyncio.run(notes_plugin.handle("recall_notes", message, None, 1, "what groceries", [], None))
    message.channel.send.assert_awaited_once_with("You need milk.")

def test_save_idea_empty_content_guarded():
    message = _message()
    asyncio.run(notes_plugin.handle("save_idea", message, None, 1, "  ", [], None))
    message.channel.send.assert_awaited_once_with("What idea should I save?")
    assert notes.search(1, tags=["idea"]) == []

def test_save_idea():
    message = _message()
    asyncio.run(notes_plugin.handle("save_idea", message, None, 1, "build a treehouse", [], None))
    message.channel.send.assert_awaited_once_with("Saved that idea.")
    assert notes.search(1, tags=["idea"])[0]["content"] == "build a treehouse"

def test_recall_ideas_empty():
    message = _message()
    asyncio.run(notes_plugin.handle("recall_ideas", message, None, 1, "", [], None))
    message.channel.send.assert_awaited_once_with("No ideas saved.")

def test_recall_ideas_with_items():
    notes.save(1, "build a treehouse", ["idea"])
    message = _message()
    asyncio.run(notes_plugin.handle("recall_ideas", message, None, 1, "", [], None))
    message.channel.send.assert_awaited_once_with("- build a treehouse")

def test_discard_idea_empty_content_guarded():
    message = _message()
    asyncio.run(notes_plugin.handle("discard_idea", message, None, 1, "", [], None))
    message.channel.send.assert_awaited_once_with("Which idea do you want to discard?")

def test_discard_idea_no_match():
    message = _message()
    asyncio.run(notes_plugin.handle("discard_idea", message, None, 1, "treehouse", [], None))
    message.channel.send.assert_awaited_once_with("No idea found matching that.")

def test_discard_idea_single_match():
    notes.save(1, "build a treehouse", ["idea"])
    message = _message()
    asyncio.run(notes_plugin.handle("discard_idea", message, None, 1, "treehouse", [], None))
    message.channel.send.assert_awaited_once_with("Discarded: build a treehouse.")
    assert notes.search(1, tags=["idea"]) == []

def test_discard_idea_multiple_matches():
    notes.save(1, "treehouse plan one", ["idea"])
    notes.save(1, "treehouse plan two", ["idea"])
    message = _message()
    asyncio.run(notes_plugin.handle("discard_idea", message, None, 1, "treehouse", [], None))
    sent_text = message.channel.send.call_args[0][0]
    assert "Found more than one match" in sent_text

def test_expand_idea_empty_content_guarded():
    message = _message()
    asyncio.run(notes_plugin.handle("expand_idea", message, None, 1, "", [], None))
    message.channel.send.assert_awaited_once_with("Which idea do you want to expand on?")

def test_expand_idea_single_match():
    notes.save(1, "build a treehouse", ["idea"])
    message = _message()
    with patch.object(brain, "expand", return_value="Here's how..."):
        asyncio.run(notes_plugin.handle("expand_idea", message, None, 1, "treehouse", [], None))
    message.channel.send.assert_awaited_once_with("Here's how...")
