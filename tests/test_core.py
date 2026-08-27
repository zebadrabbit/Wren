import os, asyncio, pytest
from unittest.mock import patch
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from wren import brain
from wren import config
from wren import core
from wren import registry as plugins
from wren import settings
from wren.channel import Ctx, CollectingChannel
from wren.skills import memory_store as memory
from wren.skills import shopping_store

OWNER = 1


@pytest.fixture(autouse=True)
def authorized(monkeypatch):
    """Authorization comes from config, not the real .env."""
    monkeypatch.setattr(config, "whitelist", lambda: {"owner": OWNER})
    monkeypatch.setattr(config, "id_to_name", lambda: {OWNER: "owner"})


@pytest.fixture
def detected(monkeypatch):
    """Patches brain.detect_intent and hands back the list of calls it saw,
    so tests can assert the security gate never reached the LLM."""
    calls = []

    def install(**result):
        def fake(user_id, text, history=None):
            calls.append((user_id, text, history))
            return result
        monkeypatch.setattr(brain, "detect_intent", fake)
        return calls

    install.calls = calls
    install(intent="chat", content="", tags=[])
    return install


def dispatch(text, user_id=OWNER):
    ch = CollectingChannel()
    asyncio.run(core.handle_message(user_id, text, ch))
    return ch


# --- the authorization gate ---------------------------------------------

def test_unauthorized_user_produces_no_output_and_never_detects_intent(detected):
    calls = detected(intent="help")
    ch = dispatch("show help", user_id=999)
    assert ch.sent == []
    assert ch.files == []
    assert ch.acks == []          # not even "seen" — the surface learns nothing
    assert calls == []            # the LLM was never asked


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_blank_text_is_ignored(detected, text):
    calls = detected(intent="help")
    ch = dispatch(text)
    assert ch.sent == []
    assert ch.acks == []
    assert calls == []


# --- acknowledgement ordering -------------------------------------------

def test_authorized_message_acks_seen_first_and_done_last(detected):
    detected(intent="help")
    ch = dispatch("show help")
    assert ch.acks[0] == "seen"
    assert ch.acks[-1] == "done"


# --- core-owned intents ---------------------------------------------------

def test_help_sends_help_text(detected):
    detected(intent="help")
    ch = dispatch("what can you do")
    assert ch.sent == [core.HELP_TEXT]


def test_status_reports_backend_and_uptime(detected):
    detected(intent="status")
    ch = dispatch("show status")
    assert len(ch.sent) == 1
    assert "Backend:" in ch.sent[0]
    assert "Uptime:" in ch.sent[0]


def test_list_plugins_sends_one_line_per_plugin(detected):
    detected(intent="list_plugins")
    ch = dispatch("what plugins do you have")
    assert len(ch.sent) == 1
    lines = ch.sent[0].split("\n")
    assert len(lines) == len(plugins.plugin_status())
    for name, _active in plugins.plugin_status():
        assert any(line.endswith(f" {name}") for line in lines)


# --- plugin dispatch ------------------------------------------------------

def test_plugin_intent_is_routed_to_that_plugin_with_a_ctx(detected, monkeypatch):
    seen = {}

    class FakePlugin:
        @staticmethod
        async def handle(intent, ctx):
            seen["intent"], seen["ctx"] = intent, ctx
            await ctx.channel.send("plugin handled it")

    monkeypatch.setattr(plugins, "INTENT_HANDLERS", {"fake_intent": FakePlugin})
    detected(intent="fake_intent", content="buy milk", tags=["grocery"],
             person="hubby", when="2026-08-07T18:00:00")

    ch = dispatch("remind me to buy milk")

    assert seen["intent"] == "fake_intent"
    ctx = seen["ctx"]
    assert isinstance(ctx, Ctx)
    assert ctx.user_id == OWNER
    assert ctx.channel is ch
    assert ctx.content == "buy milk"
    assert ctx.tags == ["grocery"]
    assert ctx.person == "hubby"
    assert ctx.when == "2026-08-07T18:00:00"
    assert ch.sent == ["plugin handled it"]
    assert ch.acks == ["seen", "done"]


def test_unknown_intent_falls_through_to_chat(detected, monkeypatch):
    detected(intent="something_nobody_owns", content="ignored")
    monkeypatch.setattr(brain, "chat", lambda text, history=None: f"chatted about {text}")
    ch = dispatch("how are you")
    # chat gets the raw text, not the detected content
    assert ch.sent == ["chatted about how are you"]


def test_send_to_person_with_unknown_person(detected):
    detected(intent="send_to_person", content="dinner's at 7", person="stranger")
    ch = dispatch("tell stranger dinner's at 7")
    assert ch.sent == ["I don't know how to reach them."]
    assert ch.acks == ["seen", "done"]


# --- failure handling -----------------------------------------------------

def test_detect_intent_blowing_up_is_reported_not_raised(monkeypatch):
    def boom(user_id, text, history=None):
        raise RuntimeError("the LLM caught fire")

    monkeypatch.setattr(brain, "detect_intent", boom)
    ch = dispatch("anything")          # must not raise
    assert ch.sent == ["Something went wrong, try again."]
    assert ch.acks == ["seen", "error"]


# --- uptime formatting ----------------------------------------------------

@pytest.mark.parametrize("seconds,expected", [
    (0, "0m"),
    (45, "0m"),                  # sub-minute rounds down
    (90, "1m"),
    (3600, "1h 0m"),
    (3600 * 5 + 60 * 7, "5h 7m"),
    (86400, "1d 0h 0m"),         # days force the hours field even at zero
    (86400 + 3661, "1d 1h 1m"),
])
def test_format_uptime(seconds, expected):
    assert core._format_uptime(seconds) == expected


# --- disabled-skill dispatch guard -----------------------------------------

def test_disabled_skill_intent_falls_through_to_chat(monkeypatch):
    # A model can still emit an intent it was never offered -- a stale prompt
    # cache, or plain hallucination. Dispatch must refuse it rather than run a
    # skill the owner switched off.
    from wren import registry, settings
    from wren.skills import notes_skill, notes_store

    settings.init_db()
    notes_store.init_db()
    monkeypatch.setattr(brain, "detect_intent", lambda *a, **k: {"intent": "save_note", "content": "milk"})
    monkeypatch.setattr(brain, "chat", lambda *a, **k: "chatty reply")
    registry.set_enabled(notes_skill, False)

    channel = CollectingChannel()
    try:
        asyncio.run(core.handle_message(1, "save a note", channel))
    finally:
        registry.set_enabled(notes_skill, True)

    assert channel.sent == ["chatty reply"]


# --- memory hook ------------------------------------------------------------

def test_chat_turn_is_observed_for_memory():
    settings.init_db()
    memory.init_db()
    ch = CollectingChannel()
    with patch.object(brain, "detect_intent", return_value={"intent": "chat", "content": "x"}), \
         patch.object(brain, "chat", return_value="sure"):
        asyncio.run(core.handle_message(1, "my sister Kate lives in Denver", ch))
    assert len(memory.drain()) == 1

def test_skill_turn_is_not_observed_for_memory():
    settings.init_db()
    memory.init_db()
    shopping_store.init_db()
    ch = CollectingChannel()
    with patch.object(brain, "detect_intent",
                      return_value={"intent": "add_shopping_item", "content": "milk"}):
        asyncio.run(core.handle_message(1, "add milk, my usual", ch))
    assert memory.drain() == []
