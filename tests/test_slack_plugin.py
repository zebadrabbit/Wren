import os, asyncio, logging
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import AsyncMock

import aiohttp
import pytest

from wren import config, contacts, core, router
from wren.communication import slack_plugin

OWNER_SLACK = "U0OWNER0001"


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    router.reset()
    monkeypatch.setenv("WREN_DB", str(tmp_path / "wren.db"))
    contacts.init_db()
    monkeypatch.setattr(config, "SLACK_APP_TOKEN", "xapp-app-token")
    monkeypatch.setattr(config, "SLACK_BOT_TOKEN", "xoxb-bot-token")
    monkeypatch.setattr(config, "SLACK_OWNER_ID", OWNER_SLACK)
    yield
    router.reset()


def _event(**over) -> dict:
    e = {"type": "message", "channel_type": "im", "channel": "D0123CHAN",
         "user": "U0KIM000001", "text": "hello", "ts": "1760000000.000100"}
    e.update(over)
    return e


# ── module shape ───────────────────────────────────────────────────────────────

def test_module_declares_a_chat_door():
    assert slack_plugin.ROLE == "chat" and slack_plugin.CAN_NOTIFY is True
    assert slack_plugin.PLUGIN_NAME == "Slack"
    assert slack_plugin.SlackChannel.NAME == "slack"

def test_is_active_needs_both_tokens(monkeypatch):
    assert slack_plugin.is_active()
    monkeypatch.setattr(config, "SLACK_BOT_TOKEN", "")
    assert not slack_plugin.is_active()
    assert "SLACK_BOT_TOKEN" in slack_plugin.inactive_reason()


# ── _incoming ──────────────────────────────────────────────────────────────────

def test_incoming_dm_text():
    assert slack_plugin._incoming(_event()) == ("U0KIM000001", "hello", [])

@pytest.mark.parametrize("over", [
    {"channel_type": "channel"},
    {"bot_id": "B0123"},
    {"subtype": "message_changed"},
    {"subtype": "message_deleted"},
    {"type": "reaction_added"},
    {"user": None},
])
def test_incoming_ignores_edits_and_bots(over):
    assert slack_plugin._incoming(_event(**over)) is None

def test_incoming_file_share_wants_accepted_files_only():
    e = _event(subtype="file_share", text="look", files=[
        {"id": "F1", "name": "cat.jpg", "mimetype": "image/jpeg", "size": 1000,
         "url_private_download": "https://files.slack.com/cat.jpg"},
        {"id": "F2", "name": "clip.mp4", "mimetype": "video/mp4", "size": 1000,
         "url_private_download": "https://files.slack.com/clip.mp4"},
    ])
    user, text, wanted = slack_plugin._incoming(e)
    assert text == "look"
    assert [w["filename"] for w in wanted] == ["cat.jpg"]
    assert wanted[0]["url"] == "https://files.slack.com/cat.jpg" and wanted[0]["mime"] == "image/jpeg"

def test_incoming_with_nothing_to_say_is_none():
    assert slack_plugin._incoming(_event(text="")) is None


# ── id translation ─────────────────────────────────────────────────────────────

def test_the_owners_slack_id_becomes_their_wren_id():
    assert slack_plugin._wren_user_id(OWNER_SLACK) == 1
    assert slack_plugin._slack_user_id(1) == OWNER_SLACK

def test_a_contact_is_looked_up_and_a_stranger_is_none():
    contacts.register("kim", "slack", "U0KIM000001")
    wid = contacts.all()["kim"]
    assert slack_plugin._wren_user_id("U0KIM000001") == wid
    assert slack_plugin._slack_user_id(wid) == "U0KIM000001"
    assert slack_plugin._wren_user_id("U0STRANGER1") is None
    assert slack_plugin._slack_user_id(424242) is None


# ── _api ───────────────────────────────────────────────────────────────────────

def test_api_raises_slack_error_with_the_error_code(monkeypatch):
    class Resp:
        async def json(self): return {"ok": False, "error": "channel_not_found"}
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    class Session:
        def __init__(self, *a, **k): pass
        def post(self, url, **k):
            assert "xoxb" not in url
            assert k["headers"]["Authorization"] == "Bearer xoxb-bot-token"
            return Resp()
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    with pytest.raises(slack_plugin.SlackError) as info:
        asyncio.run(slack_plugin._api("chat.postMessage", json={"channel": "D1", "text": "x"}))
    assert info.value.error == "channel_not_found"
    assert "xoxb" not in str(info.value)

def test_transport_error_is_scrubbed_of_both_tokens(monkeypatch):
    class Session:
        def __init__(self, *a, **k): pass
        def post(self, url, **k):
            raise aiohttp.ClientError("boom xoxb-bot-token and xapp-app-token")
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    with pytest.raises(aiohttp.ClientError) as info:
        asyncio.run(slack_plugin._api("auth.test"))
    assert "xoxb" not in str(info.value) and "xapp" not in str(info.value)
    assert "<token>" in str(info.value)


# ── channel ────────────────────────────────────────────────────────────────────

def test_channel_send_posts_chunks_to_the_dm(monkeypatch):
    api = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(slack_plugin, "_api", api)
    ch = slack_plugin.SlackChannel("D0123CHAN", "1760000000.000100")
    asyncio.run(ch.send("a" * 4000 + "\n" + "b" * 10))
    assert [c.args[0] for c in api.await_args_list] == ["chat.postMessage", "chat.postMessage"]
    assert api.await_args_list[0].kwargs["json"] == {"channel": "D0123CHAN", "text": "a" * 4000}
    assert api.await_args_list[1].kwargs["json"]["text"] == "b" * 10

def test_channel_ack_adds_a_reaction_and_swallows_failure(monkeypatch, caplog):
    api = AsyncMock(side_effect=[{"ok": True}, slack_plugin.SlackError("reactions.add", "already_reacted")])
    monkeypatch.setattr(slack_plugin, "_api", api)
    ch = slack_plugin.SlackChannel("D0123CHAN", "1760000000.000100")
    asyncio.run(ch.ack("seen"))
    assert api.await_args_list[0].kwargs["json"] == {"channel": "D0123CHAN", "timestamp": "1760000000.000100", "name": "eyes"}
    with caplog.at_level(logging.WARNING):
        asyncio.run(ch.ack("done"))          # the error one
    assert "reactions.add" in caplog.text

def test_channel_send_card_falls_back_to_the_prose(monkeypatch):
    api = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(slack_plugin, "_api", api)
    ch = slack_plugin.SlackChannel("D0123CHAN", "1")
    asyncio.run(ch.send_card("shopping", {"items": []}, "The list is empty.", intent="recall_shopping"))
    assert api.await_args.kwargs["json"]["text"] == "The list is empty."

def test_channel_send_file_uses_the_three_call_upload(monkeypatch):
    calls = []
    async def fake_api(method, *, json=None):
        calls.append((method, json))
        if method == "files.getUploadURLExternal":
            return {"ok": True, "upload_url": "https://files.slack.com/upload/abc", "file_id": "F9"}
        return {"ok": True}
    put = AsyncMock()
    monkeypatch.setattr(slack_plugin, "_api", fake_api)
    monkeypatch.setattr(slack_plugin, "_upload_bytes", put)
    ch = slack_plugin.SlackChannel("D0123CHAN", "1")
    asyncio.run(ch.send_file(b"\x89PNG....", "cat.png"))
    assert calls[0] == ("files.getUploadURLExternal", {"filename": "cat.png", "length": 8})
    put.assert_awaited_once_with("https://files.slack.com/upload/abc", b"\x89PNG....")
    assert calls[1] == ("files.completeUploadExternal",
                        {"files": [{"id": "F9", "title": "cat.png"}], "channel_id": "D0123CHAN"})


# ── notify ─────────────────────────────────────────────────────────────────────

def test_notify_dms_the_owner_by_slack_id(monkeypatch):
    api = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(slack_plugin, "_api", api)
    assert asyncio.run(slack_plugin.notify(1, "dinner")) is True
    assert api.await_args.kwargs["json"] == {"channel": OWNER_SLACK, "text": "dinner"}

def test_notify_to_someone_not_on_slack_is_permanent(monkeypatch):
    api = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(slack_plugin, "_api", api)
    assert asyncio.run(slack_plugin.notify(424242, "dinner")) is False
    api.assert_not_awaited()

@pytest.mark.parametrize("error", ["channel_not_found", "user_not_found", "cannot_dm_bot",
                                   "user_disabled", "account_inactive"])
def test_notify_returns_false_on_permanent_failure(monkeypatch, error):
    monkeypatch.setattr(slack_plugin, "_api", AsyncMock(side_effect=slack_plugin.SlackError("chat.postMessage", error)))
    assert asyncio.run(slack_plugin.notify(1, "dinner")) is False

@pytest.mark.parametrize("exc", [slack_plugin.SlackError("chat.postMessage", "ratelimited"),
                                 slack_plugin.SlackError("chat.postMessage", "invalid_auth"),
                                 aiohttp.ClientError("reset")])
def test_notify_raises_on_transient_failure(monkeypatch, exc):
    monkeypatch.setattr(slack_plugin, "_api", AsyncMock(side_effect=exc))
    with pytest.raises(type(exc)):
        asyncio.run(slack_plugin.notify(1, "dinner"))

def test_notify_splits_too(monkeypatch):
    api = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(slack_plugin, "_api", api)
    asyncio.run(slack_plugin.notify(1, "x" * 4001))
    assert api.await_count == 2
