import os, asyncio, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import patch
from wren.skills import notes_store as notes
from wren import brain
from wren.skills import notes_skill as notes_plugin
from wren.channel import Ctx, CollectingChannel
from wren.flourish import EMOTES

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("WREN_DB", str(tmp_path / "wren.db"))
    notes.init_db()

def _assert_flourished(sent: str, prefix: str):
    assert sent.startswith(prefix + " ")
    assert sent.rsplit(" ", 1)[1] in EMOTES

def test_save_note():
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("save_note", Ctx(user_id=1, channel=ch, content="buy milk", tags=["grocery"])))
    _assert_flourished(ch.sent[0], "Saved.")
    assert notes.list_recent(1)[0]["content"] == "buy milk"

def test_recall_notes_no_matches():
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("recall_notes", Ctx(user_id=1, channel=ch, content="milk")))
    assert ch.sent == ["No notes found."]

def test_recall_notes_with_matches():
    notes.save(1, "buy milk", ["grocery"])
    ch = CollectingChannel()
    with patch.object(brain, "recall", return_value="You need milk."):
        asyncio.run(notes_plugin.handle("recall_notes", Ctx(user_id=1, channel=ch, content="what groceries")))
    assert ch.sent == ["You need milk."]

def test_recall_notes_falls_back_when_guessed_tag_does_not_match():
    # Regression test: the LLM may guess a tag for the recall query itself
    # (e.g. "notes") that doesn't match the tags actually used when the note
    # was saved (e.g. "self,motivation") — recall_notes must not report "No
    # notes found." just because that guessed tag doesn't overlap.
    notes.save(1, "how awesome you are", ["self", "motivation"])
    ch = CollectingChannel()
    with patch.object(brain, "recall", return_value="You're awesome."):
        asyncio.run(notes_plugin.handle("recall_notes", Ctx(user_id=1, channel=ch, content="tell me something nice", tags=["notes"])))
    assert ch.sent == ["You're awesome."]

def test_recall_notes_plain_listing_sends_one_combined_message():
    # A bare "show my notes" (empty content) lists notes directly, as one
    # combined message (and a card — see the card tests below) instead of
    # routing through the LLM or sending one message per note.
    notes.save(1, "buy milk", ["grocery"])
    notes.save(1, "how awesome you are", ["self", "motivation"])
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("recall_notes", Ctx(user_id=1, channel=ch, content="")))
    assert len(ch.sent) == 1
    assert "buy milk" in ch.sent[0] and "(tags: grocery)" in ch.sent[0]
    assert "how awesome you are" in ch.sent[0] and "(tags: self,motivation)" in ch.sent[0]

def test_recall_notes_respects_real_tag_match():
    notes.save(1, "buy milk", ["grocery"])
    notes.save(1, "fix the fence", ["home"])
    ch = CollectingChannel()
    with patch.object(brain, "recall", return_value="You need milk.") as mock_recall:
        asyncio.run(notes_plugin.handle("recall_notes", Ctx(user_id=1, channel=ch, content="groceries", tags=["grocery"])))
    assert ch.sent == ["You need milk."]
    # only the grocery-tagged note should have been passed to brain.recall
    passed_notes = mock_recall.call_args[0][0]
    assert len(passed_notes) == 1
    assert passed_notes[0]["content"] == "buy milk"

def test_save_idea_empty_content_guarded():
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("save_idea", Ctx(user_id=1, channel=ch, content="  ")))
    assert ch.sent == ["What idea should I save?"]
    assert notes.search(1, tags=["idea"]) == []

def test_save_idea():
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("save_idea", Ctx(user_id=1, channel=ch, content="build a treehouse")))
    _assert_flourished(ch.sent[0], "Saved that idea.")
    assert notes.search(1, tags=["idea"])[0]["content"] == "build a treehouse"

def test_recall_ideas_empty():
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("recall_ideas", Ctx(user_id=1, channel=ch, content="")))
    assert ch.sent == ["No ideas saved."]

def test_recall_ideas_with_items():
    notes.save(1, "build a treehouse", ["idea"])
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("recall_ideas", Ctx(user_id=1, channel=ch, content="")))
    assert ch.sent == ["- build a treehouse"]

def test_recall_ideas_multiple_items_sends_one_combined_message():
    notes.save(1, "build a treehouse", ["idea"])
    notes.save(1, "learn to bake bread", ["idea"])
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("recall_ideas", Ctx(user_id=1, channel=ch, content="")))
    assert len(ch.sent) == 1
    assert "- build a treehouse" in ch.sent[0]
    assert "- learn to bake bread" in ch.sent[0]

def test_discard_idea_empty_content_guarded():
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("discard_idea", Ctx(user_id=1, channel=ch, content="")))
    assert ch.sent == ["Which idea do you want to discard?"]

def test_discard_idea_no_match():
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("discard_idea", Ctx(user_id=1, channel=ch, content="treehouse")))
    assert ch.sent == ["No idea found matching that."]

def test_discard_idea_single_match():
    notes.save(1, "build a treehouse", ["idea"])
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("discard_idea", Ctx(user_id=1, channel=ch, content="treehouse")))
    _assert_flourished(ch.sent[0], "Discarded: build a treehouse.")
    assert notes.search(1, tags=["idea"]) == []

def test_discard_idea_multiple_matches():
    notes.save(1, "treehouse plan one", ["idea"])
    notes.save(1, "treehouse plan two", ["idea"])
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("discard_idea", Ctx(user_id=1, channel=ch, content="treehouse")))
    assert "Found more than one match" in ch.sent[0]

def test_expand_idea_empty_content_guarded():
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("expand_idea", Ctx(user_id=1, channel=ch, content="")))
    assert ch.sent == ["Which idea do you want to expand on?"]

def test_expand_idea_single_match():
    notes.save(1, "build a treehouse", ["idea"])
    ch = CollectingChannel()
    with patch.object(brain, "expand", return_value="Here's how..."):
        asyncio.run(notes_plugin.handle("expand_idea", Ctx(user_id=1, channel=ch, content="treehouse")))
    assert ch.sent == ["Here's how..."]

def test_export_notes_empty():
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("export_notes", Ctx(user_id=1, channel=ch, content="")))
    assert ch.sent == ["Nothing to export yet."]
    assert ch.files == []

def _sent_file_text(ch):
    data, filename = ch.files[0]
    return data.decode("utf-8"), filename

def test_export_notes_notes_only():
    notes.save(1, "buy milk", ["grocery"])
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("export_notes", Ctx(user_id=1, channel=ch, content="")))
    assert len(ch.files) == 1
    assert ch.sent == []
    text, filename = _sent_file_text(ch)
    assert filename.startswith("notes-export-") and filename.endswith(".md")
    assert "## Notes" in text
    assert "buy milk" in text
    assert "(tags: grocery)" in text
    assert "## Ideas\n_None._" in text

def test_export_notes_ideas_only():
    notes.save(1, "build a treehouse", ["idea"])
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("export_notes", Ctx(user_id=1, channel=ch, content="")))
    text, _ = _sent_file_text(ch)
    assert "## Notes\n_None._" in text
    assert "## Ideas" in text
    assert "- build a treehouse" in text

def test_export_notes_both():
    notes.save(1, "buy milk", ["grocery"])
    notes.save(1, "build a treehouse", ["idea"])
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("export_notes", Ctx(user_id=1, channel=ch, content="")))
    text, _ = _sent_file_text(ch)
    assert "buy milk" in text
    assert "- build a treehouse" in text
    assert "_None._" not in text

def test_export_notes_owner_isolation():
    notes.save(1, "my note", ["personal"])
    notes.save(2, "their note", ["personal"])
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("export_notes", Ctx(user_id=1, channel=ch, content="")))
    text, _ = _sent_file_text(ch)
    assert "my note" in text
    assert "their note" not in text

def test_recall_notes_guideline_distinguishes_from_conversation():
    # Regression: "do you retain context from the previous message?" was
    # misclassified as recall_notes (lexical overlap: "previous"/"past",
    # "context"/"notes") — the guideline now explicitly excludes questions
    # about the live conversation itself, not saved notes.
    assert "conversation" in notes_plugin.PROMPT_GUIDELINES

def test_recall_notes_without_a_question_emits_one_card_and_one_message():
    notes.save(1, "call the plumber", ["house"])
    notes.save(1, "school run is 8:15", ["kids"])
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle(
        "recall_notes", Ctx(user_id=1, channel=ch, content="", tags=[])))

    card = ch.cards[0]
    assert card["kind"] == "notes"
    # notes_store.search orders created_at DESC (pre-existing, out of scope
    # here) so the most-recently-saved note ("school run") leads
    assert [n["content"] for n in card["data"]["notes"]] == [
        "school run is 8:15", "call the plumber"]
    assert card["data"]["notes"][1]["tags"] == ["house"]
    assert card["intent"] == "recall_notes"
    # content MUST stay empty: with a question this intent calls the LLM, and a
    # card re-dispatches its own intent on every render
    assert card["params"]["content"] == ""
    # one combined message, not one per note
    assert len(ch.sent) == 1
    assert "call the plumber" in ch.sent[0] and "school run is 8:15" in ch.sent[0]


def test_a_filtered_notes_card_remembers_its_filter():
    # params is what the card replays an hour later; drop the tags and a
    # filtered card silently comes back unfiltered
    notes.save(1, "call the plumber", ["house"])
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle(
        "recall_notes", Ctx(user_id=1, channel=ch, content="", tags=["house"])))

    assert ch.cards[0]["params"] == {"content": "", "tags": ["house"]}


def test_recall_notes_with_a_question_still_answers_in_prose_and_emits_no_card():
    # the LLM-answering branch must never become a card: a card re-dispatches
    # its intent to refresh, which would mean an LLM call per render
    notes.save(1, "call the plumber", ["house"])
    ch = CollectingChannel()
    with patch.object(brain, "recall", return_value="You need to call the plumber."):
        asyncio.run(notes_plugin.handle(
            "recall_notes", Ctx(user_id=1, channel=ch, content="what do I need to do?", tags=[])))

    assert ch.cards == []
    assert ch.sent == ["You need to call the plumber."]


def test_recall_ideas_emits_a_card():
    notes.save(1, "build a treehouse", ["idea"])
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("recall_ideas", Ctx(user_id=1, channel=ch)))

    card = ch.cards[0]
    assert card["kind"] == "ideas"
    assert [i["content"] for i in card["data"]["ideas"]] == ["build a treehouse"]
    assert card["intent"] == "recall_ideas"
    assert len(ch.sent) == 1


def test_no_notes_and_no_ideas_still_emit_cards():
    # the card is how the page knows to draw an empty state rather than falling
    # back to a prose bubble
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle(
        "recall_notes", Ctx(user_id=1, channel=ch, content="", tags=[])))
    assert ch.cards[0]["data"]["notes"] == []
    assert ch.sent == ["No notes found."]

    ch2 = CollectingChannel()
    asyncio.run(notes_plugin.handle("recall_ideas", Ctx(user_id=1, channel=ch2)))
    assert ch2.cards[0]["data"]["ideas"] == []
    assert ch2.sent == ["No ideas saved."]
