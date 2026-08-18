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
    assert card["params"] == {"content": ""}
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
