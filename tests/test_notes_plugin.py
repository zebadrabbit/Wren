import os, asyncio, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("HUSBAND_ID", "2")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import MagicMock, AsyncMock, patch
from wren import notes
from wren import brain
from wren import notes_plugin

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
    asyncio.run(notes_plugin.handle("save_note", message, None, 1, "buy milk", ["grocery"], None, None))
    message.channel.send.assert_awaited_once_with("Saved.")
    assert notes.list_recent(1)[0]["content"] == "buy milk"

def test_recall_notes_no_matches():
    message = _message()
    asyncio.run(notes_plugin.handle("recall_notes", message, None, 1, "milk", [], None, None))
    message.channel.send.assert_awaited_once_with("No notes found.")

def test_recall_notes_with_matches():
    notes.save(1, "buy milk", ["grocery"])
    message = _message()
    with patch.object(brain, "recall", return_value="You need milk."):
        asyncio.run(notes_plugin.handle("recall_notes", message, None, 1, "what groceries", [], None, None))
    message.channel.send.assert_awaited_once_with("You need milk.")

def test_recall_notes_falls_back_when_guessed_tag_does_not_match():
    # Regression test: the LLM may guess a tag for the recall query itself
    # (e.g. "notes") that doesn't match the tags actually used when the note
    # was saved (e.g. "self,motivation") — recall_notes must not report "No
    # notes found." just because that guessed tag doesn't overlap.
    notes.save(1, "how awesome you are", ["self", "motivation"])
    message = _message()
    with patch.object(brain, "recall", return_value="You're awesome."):
        asyncio.run(notes_plugin.handle("recall_notes", message, None, 1, "tell me something nice", ["notes"], None, None))
    message.channel.send.assert_awaited_once_with("You're awesome.")

def test_recall_notes_plain_listing_sends_one_message_per_note():
    # A bare "show my notes" (empty content) lists notes directly, one
    # Discord message per note, instead of routing through the LLM.
    notes.save(1, "buy milk", ["grocery"])
    notes.save(1, "how awesome you are", ["self", "motivation"])
    message = _message()
    asyncio.run(notes_plugin.handle("recall_notes", message, None, 1, "", [], None, None))
    assert message.channel.send.await_count == 2
    sent_texts = [c.args[0] for c in message.channel.send.await_args_list]
    assert any("buy milk" in t and "(tags: grocery)" in t for t in sent_texts)
    assert any("how awesome you are" in t and "(tags: self,motivation)" in t for t in sent_texts)

def test_recall_notes_respects_real_tag_match():
    notes.save(1, "buy milk", ["grocery"])
    notes.save(1, "fix the fence", ["home"])
    message = _message()
    with patch.object(brain, "recall", return_value="You need milk.") as mock_recall:
        asyncio.run(notes_plugin.handle("recall_notes", message, None, 1, "groceries", ["grocery"], None, None))
    message.channel.send.assert_awaited_once_with("You need milk.")
    # only the grocery-tagged note should have been passed to brain.recall
    passed_notes = mock_recall.call_args[0][0]
    assert len(passed_notes) == 1
    assert passed_notes[0]["content"] == "buy milk"

def test_save_idea_empty_content_guarded():
    message = _message()
    asyncio.run(notes_plugin.handle("save_idea", message, None, 1, "  ", [], None, None))
    message.channel.send.assert_awaited_once_with("What idea should I save?")
    assert notes.search(1, tags=["idea"]) == []

def test_save_idea():
    message = _message()
    asyncio.run(notes_plugin.handle("save_idea", message, None, 1, "build a treehouse", [], None, None))
    message.channel.send.assert_awaited_once_with("Saved that idea.")
    assert notes.search(1, tags=["idea"])[0]["content"] == "build a treehouse"

def test_recall_ideas_empty():
    message = _message()
    asyncio.run(notes_plugin.handle("recall_ideas", message, None, 1, "", [], None, None))
    message.channel.send.assert_awaited_once_with("No ideas saved.")

def test_recall_ideas_with_items():
    notes.save(1, "build a treehouse", ["idea"])
    message = _message()
    asyncio.run(notes_plugin.handle("recall_ideas", message, None, 1, "", [], None, None))
    message.channel.send.assert_awaited_once_with("- build a treehouse")

def test_recall_ideas_multiple_items_sends_one_message_each():
    notes.save(1, "build a treehouse", ["idea"])
    notes.save(1, "learn to bake bread", ["idea"])
    message = _message()
    asyncio.run(notes_plugin.handle("recall_ideas", message, None, 1, "", [], None, None))
    assert message.channel.send.await_count == 2
    sent_texts = [c.args[0] for c in message.channel.send.await_args_list]
    assert "- build a treehouse" in sent_texts
    assert "- learn to bake bread" in sent_texts

def test_discard_idea_empty_content_guarded():
    message = _message()
    asyncio.run(notes_plugin.handle("discard_idea", message, None, 1, "", [], None, None))
    message.channel.send.assert_awaited_once_with("Which idea do you want to discard?")

def test_discard_idea_no_match():
    message = _message()
    asyncio.run(notes_plugin.handle("discard_idea", message, None, 1, "treehouse", [], None, None))
    message.channel.send.assert_awaited_once_with("No idea found matching that.")

def test_discard_idea_single_match():
    notes.save(1, "build a treehouse", ["idea"])
    message = _message()
    asyncio.run(notes_plugin.handle("discard_idea", message, None, 1, "treehouse", [], None, None))
    message.channel.send.assert_awaited_once_with("Discarded: build a treehouse.")
    assert notes.search(1, tags=["idea"]) == []

def test_discard_idea_multiple_matches():
    notes.save(1, "treehouse plan one", ["idea"])
    notes.save(1, "treehouse plan two", ["idea"])
    message = _message()
    asyncio.run(notes_plugin.handle("discard_idea", message, None, 1, "treehouse", [], None, None))
    sent_text = message.channel.send.call_args[0][0]
    assert "Found more than one match" in sent_text

def test_expand_idea_empty_content_guarded():
    message = _message()
    asyncio.run(notes_plugin.handle("expand_idea", message, None, 1, "", [], None, None))
    message.channel.send.assert_awaited_once_with("Which idea do you want to expand on?")

def test_expand_idea_single_match():
    notes.save(1, "build a treehouse", ["idea"])
    message = _message()
    with patch.object(brain, "expand", return_value="Here's how..."):
        asyncio.run(notes_plugin.handle("expand_idea", message, None, 1, "treehouse", [], None, None))
    message.channel.send.assert_awaited_once_with("Here's how...")
