import os, asyncio, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import AsyncMock, patch
from wren.skills import shopping_store as shopping
from wren.skills import shopping_skill as shopping_plugin
from wren import router
from wren import contacts
from wren.channel import Ctx, CollectingChannel
from wren.flourish import EMOTES

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("WREN_DB", str(tmp_path / "wren.db"))
    shopping.init_db()
    contacts.init_db()
    contacts.add("husband", 2)

def _assert_flourished(sent: str, prefix: str):
    assert sent.startswith(prefix + " ")
    assert sent.rsplit(" ", 1)[1] in EMOTES

def test_add_shopping_item_new():
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle("add_shopping_item", Ctx(user_id=1, channel=ch, content="potatoes")))
    _assert_flourished(ch.sent[-1], "Added potatoes.")

def test_add_shopping_item_dedup():
    shopping.add("potatoes", "owner")
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle("add_shopping_item", Ctx(user_id=1, channel=ch, content="potatoes")))
    assert ch.sent == ["Already on the list."]

def test_remove_shopping_item_found():
    shopping.add("milk", "owner")
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle("remove_shopping_item", Ctx(user_id=1, channel=ch, content="milk")))
    _assert_flourished(ch.sent[-1], "Got it, removed milk.")

def test_remove_shopping_item_not_found():
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle("remove_shopping_item", Ctx(user_id=1, channel=ch, content="milk")))
    assert ch.sent == ["milk wasn't on the list."]

def test_recall_shopping_empty():
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle("recall_shopping", Ctx(user_id=1, channel=ch, content="")))
    assert ch.sent == ["Shopping list is empty."]

def test_recall_shopping_with_items():
    shopping.add("potatoes", "owner")
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle("recall_shopping", Ctx(user_id=1, channel=ch, content="")))
    assert ch.sent == ["potatoes"]

def test_send_shopping_list_unknown_contact():
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle("send_shopping_list", Ctx(user_id=1, channel=ch, content="", person="stranger")))
    assert ch.sent == ["I don't know how to reach them."]

def test_send_shopping_list_empty_list():
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle("send_shopping_list", Ctx(user_id=1, channel=ch, content="", person="husband")))
    assert ch.sent == ["Nothing on the list to send."]

def test_send_shopping_list_success():
    shopping.add("potatoes", "owner")
    ch = CollectingChannel()
    with patch.object(router, "notify_name", new=AsyncMock(return_value=True)):
        asyncio.run(shopping_plugin.handle("send_shopping_list", Ctx(user_id=1, channel=ch, content="", person="husband")))
    _assert_flourished(ch.sent[-1], "Sent to husband.")

def test_send_shopping_list_delivery_failure():
    shopping.add("potatoes", "owner")
    ch = CollectingChannel()
    with patch.object(router, "notify_name", new=AsyncMock(return_value=False)):
        asyncio.run(shopping_plugin.handle("send_shopping_list", Ctx(user_id=1, channel=ch, content="", person="husband")))
    assert ch.sent == ["Couldn't reach husband — they may be unreachable right now."]

def test_recall_shopping_emits_a_card_alongside_the_prose():
    shopping.add("milk", "ann")
    shopping.add("eggs", "ann")
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle(
        "recall_shopping", Ctx(user_id=1, channel=ch, content="")))

    card = ch.cards[0]
    assert card["kind"] == "shopping"
    assert [i["text"] for i in card["data"]["items"]] == ["milk", "eggs"]
    assert all(i["added_by"] == "ann" for i in card["data"]["items"])
    # the refresh wiring: without these the stored card cannot re-read itself
    assert card["intent"] == "recall_shopping"
    assert card["params"] == {"content": "", "list": "shopping"}
    # the prose is untouched -- Discord and Telegram still get exactly this
    assert "milk, eggs" in ch.sent[0]

def test_an_empty_shopping_list_still_emits_a_card():
    # the card is how the page knows to draw an empty list with an add box,
    # rather than falling back to a prose bubble
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle(
        "recall_shopping", Ctx(user_id=1, channel=ch, content="")))

    card = ch.cards[0]
    assert card["kind"] == "shopping"
    assert card["data"]["items"] == []
    assert ch.sent == ["Shopping list is empty."]

def test_clear_shopping_empties_the_list():
    shopping.add("milk", "owner")
    shopping.add("eggs", "owner")
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle("clear_shopping", Ctx(user_id=1, channel=ch, content="shopping list")))
    _assert_flourished(ch.sent[-1], "Cleared the list — 2 items off.")
    assert shopping.active_items() == []

def test_clear_shopping_singular_wording():
    shopping.add("milk", "owner")
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle("clear_shopping", Ctx(user_id=1, channel=ch, content="shopping list")))
    _assert_flourished(ch.sent[-1], "Cleared the list — 1 item off.")

def test_clear_shopping_when_already_empty():
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle("clear_shopping", Ctx(user_id=1, channel=ch, content="shopping list")))
    assert ch.sent == ["Shopping list is already empty."]


def test_restore_shopping_item_puts_it_back():
    shopping.add("milk", "owner")
    shopping.remove("milk")
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle(
        "restore_shopping_item", Ctx(user_id=1, channel=ch, content="milk")))
    _assert_flourished(ch.sent[-1], "Put milk back.")
    assert [i["original_text"] for i in shopping.active_items()] == ["milk"]


def test_restore_shopping_item_when_there_is_nothing_to_restore():
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle(
        "restore_shopping_item", Ctx(user_id=1, channel=ch, content="milk")))
    assert ch.sent == ["milk wasn't there to put back."]


def test_restore_is_offered_to_the_model():
    # the card's undo button is not the only caller -- "put the milk back"
    # typed on any surface routes here too, which needs the intent registered
    assert "restore_shopping_item" in shopping_plugin.INTENTS
    assert "restore_shopping_item" in shopping_plugin.PROMPT_GUIDELINES


# --- named lists: "add a tent to my packing list" -----------------------------
# The name is parsed here from the user's words; the classifier keeps its six
# intents. No name, or grocery/groceries, means the shopping list.

@pytest.mark.parametrize("text,expected", [
    ("add a tent to my packing list", "packing"),
    ("put rope on the packing list", "packing"),
    ("take rope off the packing list", "packing"),
    ("what's on the hardware store list", "hardware store"),
    ("take the tent off the packing list", "packing"),
    ("clear the packing list", "packing"),
    ("add milk to the shopping list", "shopping"),
    ("add milk to the grocery list", "shopping"),
    ("add milk to my groceries list", "shopping"),
    ("add milk", "shopping"),
    ("what's on the list", "shopping"),
    ("add the christmas list of guests to notes", "shopping"),  # "list of" is not a list name
])
def test_list_name_from_text(text, expected):
    assert shopping_plugin._list_name(Ctx(user_id=1, channel=CollectingChannel(), text=text)) == expected

def test_list_name_from_a_card_dispatch_with_no_text():
    assert shopping_plugin._list_name(Ctx(user_id=1, channel=CollectingChannel(), list_name="packing")) == "packing"
    assert shopping_plugin._list_name(Ctx(user_id=1, channel=CollectingChannel())) == "shopping"

def test_add_to_a_named_list_says_which_and_keeps_shopping_clean():
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle("add_shopping_item", Ctx(
        user_id=1, channel=ch, content="tent", text="add a tent to my packing list")))
    _assert_flourished(ch.sent[-1], "Added tent to the packing list.")
    assert shopping.active_items() == []
    assert [i["item"] for i in shopping.active_items(list_name="packing")] == ["tent"]

def test_remove_and_clear_target_the_named_list():
    shopping.add("tent", "owner", list_name="packing")
    shopping.add("milk", "owner")
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle("remove_shopping_item", Ctx(
        user_id=1, channel=ch, content="tent", text="take the tent off the packing list")))
    _assert_flourished(ch.sent[-1], "Got it, removed tent from the packing list.")
    asyncio.run(shopping_plugin.handle("clear_shopping", Ctx(user_id=1, channel=ch, text="clear the packing list")))
    assert "packing list" in ch.sent[-1]
    assert [i["item"] for i in shopping.active_items()] == ["milk"]

def test_recall_a_named_list_carries_the_name_in_the_card_and_its_params():
    shopping.add("tent", "owner", list_name="packing")
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle("recall_shopping", Ctx(
        user_id=1, channel=ch, content="", text="what's on my packing list")))
    assert ch.sent == ["tent"]
    card = ch.cards[0]
    assert card["data"]["list"] == "packing"
    assert card["params"] == {"content": "", "list": "packing"}
    assert card["data"]["items"][0]["text"] == "tent"

def test_recall_an_empty_named_list():
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle("recall_shopping", Ctx(
        user_id=1, channel=ch, content="", text="what's on my packing list")))
    assert ch.sent == ["The packing list is empty."]

def test_the_shopping_card_params_are_unchanged_for_the_default_list():
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle("recall_shopping", Ctx(user_id=1, channel=ch, content="")))
    assert ch.cards[0]["params"] == {"content": "", "list": "shopping"}
    assert ch.cards[0]["data"]["list"] == "shopping"


# --- a photo of a receipt, a fridge or a handwritten list ---------------------
from wren.channel import Inbound
from wren import brain as _brain

def _photo():
    return Inbound(filename="receipt.jpg", mime="image/jpeg", data=b"\xff\xd8\xff\xe0" + b"\0" * 8)

def test_add_shopping_item_declares_that_it_takes_files():
    assert "add_shopping_item" in shopping_plugin.ACCEPTS_FILES

def test_add_from_a_photo_reads_the_items_off_the_picture(monkeypatch):
    seen = []
    def fake_items_in(files, hint):
        seen.append(([f.filename for f in files], hint)); return ["milk", "eggs", "bread"]
    monkeypatch.setattr(_brain, "items_in", fake_items_in)
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle("add_shopping_item", Ctx(
        user_id=1, channel=ch, content="these", text="add these to my packing list", files=[_photo()])))
    assert seen == [(["receipt.jpg"], "these")]
    assert [i["item"] for i in shopping.active_items(list_name="packing")] == ["milk", "eggs", "bread"]
    _assert_flourished(ch.sent[-1], "Added milk, eggs and bread to the packing list.")

def test_add_from_a_photo_with_nothing_readable_says_so(monkeypatch):
    monkeypatch.setattr(_brain, "items_in", lambda files, hint: [])
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle("add_shopping_item", Ctx(
        user_id=1, channel=ch, content="these", text="add these", files=[_photo()])))
    assert ch.sent == ["I couldn't read any items off that picture."]
    assert shopping.active_items() == []

def test_add_from_a_photo_skips_what_is_already_on_the_list(monkeypatch):
    shopping.add("milk", "owner")
    monkeypatch.setattr(_brain, "items_in", lambda files, hint: ["milk", "eggs"])
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle("add_shopping_item", Ctx(
        user_id=1, channel=ch, content="these", text="add these", files=[_photo()])))
    _assert_flourished(ch.sent[-1], "Added eggs.")
    assert [i["item"] for i in shopping.active_items()] == ["milk", "eggs"]
