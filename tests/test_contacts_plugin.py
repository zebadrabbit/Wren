import os, asyncio, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from wren import contacts
from wren.skills import contacts_skill as contacts_plugin
from wren.channel import Ctx, CollectingChannel
from wren.flourish import EMOTES

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("WREN_DB", str(tmp_path / "wren.db"))
    contacts.init_db()

def _assert_flourished(sent: str, prefix: str):
    assert sent.startswith(prefix + " ")
    assert sent.rsplit(" ", 1)[1] in EMOTES

# user_id=1 is WREN_OWNER_ID; user_id=2 is a non-owner whitelisted contact
def test_non_owner_cannot_add_contact():
    ch = CollectingChannel()
    asyncio.run(contacts_plugin.handle("add_contact", Ctx(user_id=2, channel=ch, content="222222222222222222", person="hubby")))
    assert ch.sent == ["Only the owner can manage contacts."]
    assert contacts.all() == {}

def test_owner_adds_contact():
    ch = CollectingChannel()
    asyncio.run(contacts_plugin.handle("add_contact", Ctx(user_id=1, channel=ch, content="222222222222222222", person="hubby")))
    _assert_flourished(ch.sent[-1], "Added hubby.")
    assert contacts.all() == {"hubby": 222222222222222222}

def test_owner_add_contact_non_numeric_id():
    ch = CollectingChannel()
    asyncio.run(contacts_plugin.handle("add_contact", Ctx(user_id=1, channel=ch, content="not-a-number", person="hubby")))
    assert ch.sent == ["That doesn't look like a user ID."]
    assert contacts.all() == {}

def test_owner_add_contact_empty_alias():
    ch = CollectingChannel()
    asyncio.run(contacts_plugin.handle("add_contact", Ctx(user_id=1, channel=ch, content="222222222222222222", person="")))
    assert ch.sent == ["Who should I add?"]
    assert contacts.all() == {}

def test_owner_add_contact_duplicate_discord_id():
    contacts.add("hubby", 222222222222222222)
    ch = CollectingChannel()
    asyncio.run(contacts_plugin.handle("add_contact", Ctx(user_id=1, channel=ch, content="222222222222222222", person="kevin")))
    assert ch.sent == ["That user ID is already registered under another name."]
    assert contacts.all() == {"hubby": 222222222222222222}

def test_owner_add_contact_taken_alias():
    contacts.add("hubby", 222222222222222222)
    ch = CollectingChannel()
    asyncio.run(contacts_plugin.handle("add_contact", Ctx(user_id=1, channel=ch, content="333333333333333333", person="hubby")))
    assert ch.sent == ["That name's already taken."]

def test_owner_add_contact_alias_matches_owner():
    ch = CollectingChannel()
    asyncio.run(contacts_plugin.handle("add_contact", Ctx(user_id=1, channel=ch, content="222222222222222222", person="owner")))
    assert ch.sent == ["That name's already taken."]

def test_owner_removes_contact():
    contacts.add("hubby", 222222222222222222)
    ch = CollectingChannel()
    asyncio.run(contacts_plugin.handle("remove_contact", Ctx(user_id=1, channel=ch, person="hubby")))
    _assert_flourished(ch.sent[-1], "Removed hubby.")
    assert contacts.all() == {}

def test_owner_removes_unknown_contact():
    ch = CollectingChannel()
    asyncio.run(contacts_plugin.handle("remove_contact", Ctx(user_id=1, channel=ch, person="nobody")))
    assert ch.sent == ["No contact named nobody."]

def test_non_owner_cannot_remove_contact():
    contacts.add("hubby", 222222222222222222)
    ch = CollectingChannel()
    asyncio.run(contacts_plugin.handle("remove_contact", Ctx(user_id=2, channel=ch, person="hubby")))
    assert ch.sent == ["Only the owner can manage contacts."]
    assert contacts.all() == {"hubby": 222222222222222222}

def test_owner_lists_contacts_empty():
    ch = CollectingChannel()
    asyncio.run(contacts_plugin.handle("list_contacts", Ctx(user_id=1, channel=ch)))
    assert ch.sent == ["No contacts yet."]

def test_owner_lists_contacts():
    contacts.add("hubby", 222222222222222222)
    contacts.add("kevin", 333333333333333333)
    ch = CollectingChannel()
    asyncio.run(contacts_plugin.handle("list_contacts", Ctx(user_id=1, channel=ch)))
    assert ch.sent == ["hubby (discord), kevin (discord)"]

def test_non_owner_cannot_list_contacts():
    ch = CollectingChannel()
    asyncio.run(contacts_plugin.handle("list_contacts", Ctx(user_id=2, channel=ch)))
    assert ch.sent == ["Only the owner can manage contacts."]


# --- "add 555 as hubby on telegram" ------------------------------------------
from wren import config

def _add(ch, content, person, text=""):
    asyncio.run(contacts_plugin.handle("add_contact", Ctx(user_id=1, channel=ch, content=content, person=person, text=text)))

def test_owner_adds_a_contact_on_a_named_surface(monkeypatch):
    monkeypatch.setattr(config, "COMMUNICATION_PLUGINS", ["discord", "telegram"])
    ch = CollectingChannel()
    _add(ch, "555", "hubby", text="add 555 as hubby on telegram")
    _assert_flourished(ch.sent[-1], "Added hubby on telegram.")
    assert contacts.wren_id("telegram", 555) == 555
    assert contacts.surfaces_of(555) == ["telegram"]

def test_owner_adds_a_second_surface_to_an_existing_contact(monkeypatch):
    monkeypatch.setattr(config, "COMMUNICATION_PLUGINS", ["discord", "telegram"])
    contacts.add("hubby", 222222222222222222)
    ch = CollectingChannel()
    _add(ch, "555", "hubby", text="add 555 as hubby on telegram")
    _assert_flourished(ch.sent[-1], "Added hubby on telegram.")
    assert contacts.all() == {"hubby": 222222222222222222}
    assert contacts.surface_id("telegram", 222222222222222222) == 555

def test_a_surface_the_contact_already_has_is_taken(monkeypatch):
    monkeypatch.setattr(config, "COMMUNICATION_PLUGINS", ["discord", "telegram"])
    contacts.add("hubby", 42, surface="telegram")
    ch = CollectingChannel()
    _add(ch, "43", "hubby", text="add 43 as hubby on telegram")
    assert ch.sent == ["That name's already taken."]

def test_an_unknown_surface_word_is_not_a_surface(monkeypatch):
    # same rule as reminders' "via": a word this install does not run is part
    # of the sentence, so the default surface applies
    monkeypatch.setattr(config, "COMMUNICATION_PLUGINS", ["discord", "telegram"])
    ch = CollectingChannel()
    _add(ch, "555", "hubby", text="add 555 as hubby on whatsapp")
    assert contacts.surfaces_of(555) == ["discord"]

def test_default_surface_is_the_first_configured_one_with_ids(monkeypatch):
    monkeypatch.setattr(config, "COMMUNICATION_PLUGINS", ["telegram", "http"])
    ch = CollectingChannel()
    _add(ch, "555", "hubby")
    assert contacts.surfaces_of(555) == ["telegram"]

def test_list_contacts_shows_each_ones_surfaces():
    contacts.add("hubby", 222222222222222222)
    contacts.set_id("hubby", "telegram", 42)
    contacts.add("kevin", 333333333333333333)
    ch = CollectingChannel()
    asyncio.run(contacts_plugin.handle("list_contacts", Ctx(user_id=1, channel=ch)))
    assert ch.sent == ["hubby (discord, telegram), kevin (discord)"]


def test_undo_remove_contact_brings_them_back_on_every_surface():
    contacts.add("hubby", 222); contacts.set_id("hubby", "telegram", 42)
    ctx = Ctx(user_id=1, channel=CollectingChannel(), person="hubby")
    asyncio.run(contacts_plugin.handle("remove_contact", ctx))
    assert contacts.all() == {}
    asyncio.run(contacts_plugin.UNDO["remove_contact"](ctx))
    assert contacts.all() == {"hubby": 222} and contacts.surface_id("telegram", 222) == 42
    _assert_flourished(ctx.channel.sent[-1], "Added hubby back.")
