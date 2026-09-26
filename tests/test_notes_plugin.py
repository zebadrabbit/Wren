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


# ── attachments ───────────────────────────────────────────────────────────

from wren.channel import Inbound

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 32
PDF = b"%PDF-1.7\n" + b"\0" * 32


def _img(name="a.jpg"):
    return Inbound(filename=name, mime="image/jpeg", data=JPEG)


def test_save_note_with_one_image_reports_it_and_stores_it():
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("save_note", Ctx(user_id=1, channel=ch, content="tyre receipt", files=[_img()])))
    assert ch.sent == ["Saved, 1 image."]
    note = notes.list_recent(1)[0]
    assert [a["filename"] for a in notes.attachments(note["id"])] == ["a.jpg"]


def test_save_note_counts_mixed_files_as_files():
    ch = CollectingChannel()
    files = [_img(), Inbound(filename="manual.pdf", mime="application/pdf", data=PDF)]
    asyncio.run(notes_plugin.handle("save_note", Ctx(user_id=1, channel=ch, content="dishwasher", files=files)))
    assert ch.sent == ["Saved, 2 files."]


def test_save_note_keeps_the_good_files_and_names_the_refused_one():
    ch = CollectingChannel()
    files = [_img(), Inbound(filename="evil.jpg", mime="image/jpeg", data=b"MZ\x90\x00" + b"\0" * 32)]
    asyncio.run(notes_plugin.handle("save_note", Ctx(user_id=1, channel=ch, content="hm", files=files)))
    assert ch.sent == ["Saved, 1 image. Skipped evil.jpg: I can keep images and PDFs, not that."]
    note = notes.list_recent(1)[0]
    assert len(notes.attachments(note["id"])) == 1


def test_save_note_with_only_refused_files_and_no_caption_saves_nothing():
    ch = CollectingChannel()
    files = [Inbound(filename="evil.jpg", mime="image/jpeg", data=b"MZ\x90\x00" + b"\0" * 32)]
    asyncio.run(notes_plugin.handle("save_note", Ctx(user_id=1, channel=ch, content="", files=files)))
    assert ch.sent == ["Skipped evil.jpg: I can keep images and PDFs, not that."]
    assert notes.list_recent(1) == []


def test_save_note_with_only_refused_files_and_a_caption_still_says_saved():
    ch = CollectingChannel()
    files = [Inbound(filename="evil.jpg", mime="image/jpeg", data=b"MZ\x90\x00" + b"\0" * 32)]
    asyncio.run(notes_plugin.handle("save_note", Ctx(user_id=1, channel=ch, content="keep this", files=files)))
    assert ch.sent == ["Saved. Skipped evil.jpg: I can keep images and PDFs, not that."]
    assert notes.list_recent(1)[0]["content"] == "keep this"


def test_recall_card_marks_attached_notes_and_sends_the_files():
    note_id = notes.save(1, "tyre receipt", [])
    notes.attach(note_id, "receipt.jpg", "image/jpeg", JPEG)
    notes.save(1, "plain note", [])
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("recall_notes", Ctx(user_id=1, channel=ch, content="")))
    card = ch.cards[0]
    assert card["kind"] == "notes"
    by_content = {r["content"]: r for r in card["data"]["notes"]}
    assert by_content["tyre receipt"]["files"] == [{"id": 1, "filename": "receipt.jpg", "mime": "image/jpeg"}]
    assert by_content["plain note"]["files"] == []
    assert "tyre receipt (1 image)" in ch.sent[0]
    assert "plain note" in ch.sent[0] and "plain note (" not in ch.sent[0]
    assert ch.files == [(JPEG, "receipt.jpg")]


def test_recall_sends_at_most_five_files_across_the_reply():
    for i in range(7):
        note_id = notes.save(1, f"n{i}", [])
        notes.attach(note_id, f"{i}.jpg", "image/jpeg", JPEG)
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("recall_notes", Ctx(user_id=1, channel=ch, content="")))
    assert len(ch.files) == 5


def test_recall_with_a_question_sends_the_matching_notes_files_too():
    note_id = notes.save(1, "tyre receipt", [])
    notes.attach(note_id, "receipt.jpg", "image/jpeg", JPEG)
    ch = CollectingChannel()
    with patch.object(brain, "recall", return_value="You paid the tyre place."):
        asyncio.run(notes_plugin.handle("recall_notes", Ctx(user_id=1, channel=ch, content="what did I pay")))
    assert ch.sent == ["You paid the tyre place."]
    assert ch.files == [(JPEG, "receipt.jpg")]


def test_export_marks_attached_notes():
    note_id = notes.save(1, "tyre receipt", [])
    notes.attach(note_id, "a.pdf", "application/pdf", PDF)
    notes.attach(note_id, "b.pdf", "application/pdf", PDF)
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("export_notes", Ctx(user_id=1, channel=ch)))
    text = ch.files[0][0].decode()
    assert "tyre receipt (2 files)" in text


def test_discard_idea_with_an_attachment_removes_it_too():
    note_id = notes.save(1, "kayak", ["idea"])
    att = notes.attach(note_id, "k.jpg", "image/jpeg", JPEG)
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("discard_idea", Ctx(user_id=1, channel=ch, content="kayak")))
    assert notes.attachment(att) is None


def test_save_reply_classifies_by_the_sniffed_type_not_the_declared_one():
    ch = CollectingChannel()
    lied = Inbound(filename="blob", mime="application/octet-stream", data=JPEG)
    asyncio.run(notes_plugin.handle("save_note", Ctx(user_id=1, channel=ch, content="x", files=[lied])))
    assert ch.sent == ["Saved, 1 image."]


# --- large collections: summary in prose, everything in the card ---------
# Spec: docs/superpowers/specs/2026-08-17-skill-cards-and-spaces-design.md,
# "Large collections". Past SUMMARY_AFTER rows a text surface gets a tag
# breakdown and an invitation to narrow; the card still carries every row.

def test_recall_notes_past_threshold_gets_tag_breakdown_not_listing():
    for i in range(10):
        notes.save(1, f"house note {i}", ["house"])
    for i in range(8):
        notes.save(1, f"work note {i}", ["work"])
    notes.save(1, "loose note", [])
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("recall_notes", Ctx(user_id=1, channel=ch, content="")))
    assert ch.sent == ["19 notes: house 10, work 8, untagged 1. Which?"]
    assert len(ch.cards[0]["data"]["notes"]) == 19


def test_recall_notes_at_threshold_still_lists_everything():
    for i in range(notes_plugin.SUMMARY_AFTER):
        notes.save(1, f"note {i}", ["house"])
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("recall_notes", Ctx(user_id=1, channel=ch, content="")))
    assert "Which?" not in ch.sent[0]
    assert all(f"note {i}" in ch.sent[0] for i in range(notes_plugin.SUMMARY_AFTER))


def test_recall_notes_one_tag_past_threshold_shows_newest_and_a_count():
    # Nothing to narrow by ("20 notes: work 20. Which?" would be useless), so
    # the newest SUMMARY_AFTER are listed and the rest counted.
    for i in range(20):
        notes.save(1, f"work note {i:02d}", ["work"])
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("recall_notes", Ctx(user_id=1, channel=ch, content="", tags=["work"])))
    text = ch.sent[0]
    assert "work note 19" in text and "work note 05" in text
    assert "work note 04" not in text
    assert text.endswith("…and 5 more. Ask about one to narrow it down.")
    assert len(ch.cards[0]["data"]["notes"]) == 20


def test_recall_ideas_past_threshold_shows_newest_and_a_count():
    for i in range(16):
        notes.save(1, f"idea {i:02d}", ["idea"])
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("recall_ideas", Ctx(user_id=1, channel=ch, content="")))
    text = ch.sent[0]
    assert text.count("\n- ") == notes_plugin.SUMMARY_AFTER - 1 and text.startswith("- idea 15")
    assert "- idea 00" not in text
    assert text.endswith("…and 1 more. Ask about one to narrow it down.")
    assert len(ch.cards[0]["data"]["ideas"]) == 16
