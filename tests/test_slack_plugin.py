import os, asyncio, json, logging, time
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
    slack_plugin._seen.clear()
    yield
    router.reset()
    slack_plugin._seen.clear()


def _event(**over) -> dict:
    e = {"type": "message", "channel_type": "im", "channel": "D0123CHAN",
         "user": "U0KIM000001", "text": "hello", "ts": f"{time.time():.6f}"}
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

@pytest.mark.parametrize("sent, typed", [
    ("add salt &amp; pepper", "add salt & pepper"),
    ("is 3 &lt; 4 &gt; 2", "is 3 < 4 > 2"),
    ("save <https://example.com|this>", "save https://example.com"),
    ("save <https://example.com>", "save https://example.com"),
    ("save <https://example.com/?a=1&amp;b=2>", "save https://example.com/?a=1&b=2"),
    ("mail <mailto:kim@example.com|kim@example.com>", "mail kim@example.com"),
    ("ask <@U0KIM000001> and <#C0123|general>", "ask @U0KIM000001 and #general"),
    ("tell <!here>", "tell @here"),
])
def test_incoming_text_is_what_the_person_typed(sent, typed):
    assert slack_plugin._incoming(_event(text=sent))[1] == typed


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
            assert url == f"{slack_plugin.API_ROOT}/chat.postMessage"
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

def test_form_only_methods_are_sent_form_encoded(monkeypatch):
    seen = {}
    class Resp:
        async def json(self): return {"ok": True, "upload_url": "https://u", "file_id": "F1"}
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    class Session:
        def __init__(self, *a, **k): pass
        def post(self, url, **k):
            seen.update(k)
            return Resp()
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    asyncio.run(slack_plugin._api("files.getUploadURLExternal", json={"filename": "a.png", "length": 3}))
    assert seen["data"] == {"filename": "a.png", "length": 3}
    assert "json" not in seen

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

def test_outgoing_text_is_escaped_for_mrkdwn(monkeypatch):
    api = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(slack_plugin, "_api", api)
    asyncio.run(slack_plugin.SlackChannel("D0123CHAN", "1").send("a < b & c"))
    assert api.await_args.kwargs["json"]["text"] == "a &lt; b &amp; c"

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
    method, body = calls[1]
    assert method == "files.completeUploadExternal" and method in slack_plugin._FORM_ONLY
    # form-encoded, so the file list travels as a JSON string, as Slack's SDKs send it
    assert isinstance(body["files"], str)
    assert json.loads(body["files"]) == [{"id": "F9", "title": "cat.png"}]
    assert body["channel_id"] == "D0123CHAN"


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
                                   "user_disabled"])
def test_notify_returns_false_on_permanent_failure(monkeypatch, error):
    monkeypatch.setattr(slack_plugin, "_api", AsyncMock(side_effect=slack_plugin.SlackError("chat.postMessage", error)))
    assert asyncio.run(slack_plugin.notify(1, "dinner")) is False

@pytest.mark.parametrize("exc", [slack_plugin.SlackError("chat.postMessage", "ratelimited"),
                                 slack_plugin.SlackError("chat.postMessage", "invalid_auth"),
                                 # the bot's own account or workspace: an admin can bring it back
                                 slack_plugin.SlackError("chat.postMessage", "account_inactive"),
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


# ── the loop ───────────────────────────────────────────────────────────────────

class FakeWs:
    """An async iterator of frames plus a send_json recorder, standing in for
    aiohttp's ClientWebSocketResponse. A frame is a dict (sent as TEXT) or a
    ready-made WSMessage. Each read yields to the loop first, as a real
    socket does, so the worker gets its turn between frames."""
    def __init__(self, frames):
        self._frames = list(frames)
        self.sent = []
        self.closed = False
    def __aiter__(self): return self
    async def __anext__(self):
        await asyncio.sleep(0)
        if not self._frames:
            raise StopAsyncIteration
        frame = self._frames.pop(0)
        if frame == "hang":
            await asyncio.sleep(3600)
        if isinstance(frame, aiohttp.WSMessage):
            return frame
        return aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps(frame), None)
    async def send_json(self, data): self.sent.append(data)
    async def close(self): self.closed = True
    async def __aenter__(self): return self
    async def __aexit__(self, *a): await self.close(); return False


def _socket(monkeypatch, frames):
    ws = FakeWs(frames)
    opened = []
    async def fake_api(method, *, json=None, token=None):
        opened.append((method, token))
        return {"ok": True, "url": "wss://wss.slack.com/link/abc"}
    class Session:
        def __init__(self, *a, **k): pass
        def ws_connect(self, url, **k):
            assert url == "wss://wss.slack.com/link/abc"
            return ws
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(slack_plugin, "_api", fake_api)
    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    return ws, opened


def _envelope(event, envelope_id="env-1", event_id=None):
    payload = {"event": event}
    if event_id:
        payload["event_id"] = event_id
    return {"type": "events_api", "envelope_id": envelope_id, "payload": payload}


def test_connection_is_opened_with_the_app_token(monkeypatch):
    ws, opened = _socket(monkeypatch, [{"type": "hello"}, {"type": "disconnect", "reason": "refresh_requested"}])
    asyncio.run(slack_plugin._run_once())
    assert opened == [("apps.connections.open", "xapp-app-token")]
    assert ws.closed


def test_envelope_is_acked_before_the_message_is_handled(monkeypatch):
    order = []
    ws, _ = _socket(monkeypatch, [_envelope(_event(user=OWNER_SLACK)), {"type": "disconnect"}])
    original_send = ws.send_json
    async def send_json(data):
        order.append(("ack", data["envelope_id"])); await original_send(data)
    ws.send_json = send_json
    async def handle(user_id, text, channel, **kw):
        order.append(("handle", user_id, text, channel.NAME))
    monkeypatch.setattr(core, "handle_message", handle)
    asyncio.run(slack_plugin._run_once())
    assert order == [("ack", "env-1"), ("handle", 1, "hello", "slack")]


def test_a_second_envelope_is_acked_while_the_first_is_still_being_handled(monkeypatch):
    # The brain can take longer than Slack's ~3 s ack window. Were handling
    # inline, env-2 would wait unacked behind it and Slack would resend it.
    ws, _ = _socket(monkeypatch, [_envelope(_event(user=OWNER_SLACK, text="first"), "env-1"),
                                  _envelope(_event(user=OWNER_SLACK, text="second"), "env-2"),
                                  {"type": "disconnect"}])
    both_acked = asyncio.Event()
    original_send = ws.send_json
    async def send_json(data):
        await original_send(data)
        if len(ws.sent) == 2:
            both_acked.set()
    ws.send_json = send_json
    handled = []
    async def handle(user_id, text, channel, **kw):
        if text == "first":
            await asyncio.wait_for(both_acked.wait(), 1)   # times out if the reader waits on us
        handled.append(text)
    monkeypatch.setattr(core, "handle_message", handle)
    asyncio.run(slack_plugin._run_once())
    assert ws.sent == [{"envelope_id": "env-1"}, {"envelope_id": "env-2"}]
    assert handled == ["first", "second"]                   # one worker: still in order


def test_a_redelivered_event_is_acked_and_handled_once(monkeypatch):
    # Socket Mode is at-least-once: the same event_id can come back under a
    # new envelope, on this connection or the next one.
    handle = AsyncMock()
    monkeypatch.setattr(core, "handle_message", handle)
    event = _event(user=OWNER_SLACK)
    ws, _ = _socket(monkeypatch, [_envelope(event, "env-1", event_id="Ev1"),
                                  _envelope(event, "env-2", event_id="Ev1"),
                                  {"type": "disconnect"}])
    asyncio.run(slack_plugin._run_once())
    ws2, _ = _socket(monkeypatch, [_envelope(event, "env-3", event_id="Ev1"),
                                   _envelope(_event(user=OWNER_SLACK, text="new"), "env-4", event_id="Ev2"),
                                   {"type": "disconnect"}])
    asyncio.run(slack_plugin._run_once())
    assert ws.sent == [{"envelope_id": "env-1"}, {"envelope_id": "env-2"}]
    assert ws2.sent == [{"envelope_id": "env-3"}, {"envelope_id": "env-4"}]
    assert [c.args[1] for c in handle.await_args_list] == ["hello", "new"]


def test_an_error_frame_is_logged_and_ends_the_connection(monkeypatch, caplog):
    error = aiohttp.WSMessage(aiohttp.WSMsgType.ERROR, RuntimeError("reset xoxb-bot-token"), None)
    ws, _ = _socket(monkeypatch, [error, _envelope(_event(user=OWNER_SLACK))])
    handle = AsyncMock()
    monkeypatch.setattr(core, "handle_message", handle)
    with caplog.at_level(logging.WARNING):
        asyncio.run(slack_plugin._run_once())
    assert "slack websocket error" in caplog.text and "reset" in caplog.text
    assert "xoxb" not in caplog.text
    assert ws.sent == [] and ws.closed
    handle.assert_not_awaited()


def test_cancelling_the_connection_cancels_its_worker(monkeypatch):
    ws, _ = _socket(monkeypatch, [_envelope(_event(user=OWNER_SLACK)), "hang"])
    started, finished = asyncio.Event(), []
    async def handle(user_id, text, channel, **kw):
        started.set()
        await asyncio.sleep(3600)
        finished.append(text)
    monkeypatch.setattr(core, "handle_message", handle)
    async def _run():
        task = asyncio.create_task(slack_plugin._run_once())
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert asyncio.run(_run()) == []          # no worker left behind
    assert finished == []


def test_a_stranger_is_acked_and_dropped(monkeypatch):
    ws, _ = _socket(monkeypatch, [_envelope(_event(user="U0STRANGER1")), {"type": "disconnect"}])
    handle = AsyncMock()
    monkeypatch.setattr(core, "handle_message", handle)
    asyncio.run(slack_plugin._run_once())
    assert ws.sent == [{"envelope_id": "env-1"}]
    handle.assert_not_awaited()


def test_a_failed_message_does_not_end_the_connection(monkeypatch, caplog):
    ws, _ = _socket(monkeypatch, [_envelope(_event(user=OWNER_SLACK), "env-1"),
                                  _envelope(_event(user=OWNER_SLACK, text="again"), "env-2"),
                                  {"type": "disconnect"}])
    handle = AsyncMock(side_effect=[RuntimeError("brain down"), None])
    monkeypatch.setattr(core, "handle_message", handle)
    with caplog.at_level(logging.ERROR):
        asyncio.run(slack_plugin._run_once())
    assert handle.await_count == 2
    assert "brain down" in caplog.text and "hello" not in caplog.text   # never the body


def test_stale_event_gets_the_offline_notice(monkeypatch):
    old = _event(user=OWNER_SLACK, ts=str(time.time() - 3600))
    ws, _ = _socket(monkeypatch, [_envelope(old), {"type": "disconnect"}])
    handle = AsyncMock()
    sent = AsyncMock()
    monkeypatch.setattr(core, "handle_message", handle)
    monkeypatch.setattr(slack_plugin, "_send_text", sent)
    asyncio.run(slack_plugin._run_once())
    handle.assert_not_awaited()
    assert sent.await_args.args[0] == "D0123CHAN" and "offline" in sent.await_args.args[1]


def test_files_are_downloaded_and_handed_to_core(monkeypatch):
    e = _event(user=OWNER_SLACK, subtype="file_share", text="keep this", files=[
        {"id": "F1", "name": "cat.jpg", "mimetype": "image/jpeg", "size": 10,
         "url_private_download": "https://files.slack.com/cat.jpg"}])
    ws, _ = _socket(monkeypatch, [_envelope(e), {"type": "disconnect"}])
    monkeypatch.setattr(slack_plugin, "_download", AsyncMock(return_value=b"jpegbytes"))
    handle = AsyncMock()
    monkeypatch.setattr(core, "handle_message", handle)
    asyncio.run(slack_plugin._run_once())
    files = handle.await_args.kwargs["files"]
    assert [(f.filename, f.mime, f.data) for f in files] == [("cat.jpg", "image/jpeg", b"jpegbytes")]


def test_an_oversize_file_is_refused_in_the_dm(monkeypatch):
    e = _event(user=OWNER_SLACK, subtype="file_share", text="", files=[
        {"id": "F1", "name": "big.pdf", "mimetype": "application/pdf", "size": 11 * 1024 * 1024,
         "url_private_download": "https://files.slack.com/big.pdf"}])
    ws, _ = _socket(monkeypatch, [_envelope(e), {"type": "disconnect"}])
    sent = AsyncMock()
    handle = AsyncMock()
    monkeypatch.setattr(slack_plugin, "_send_text", sent)
    monkeypatch.setattr(core, "handle_message", handle)
    asyncio.run(slack_plugin._run_once())
    assert "too big" in sent.await_args.args[1]
    handle.assert_not_awaited()


def test_start_raises_when_a_token_is_unset(monkeypatch):
    monkeypatch.setattr(config, "SLACK_APP_TOKEN", "")
    with pytest.raises(RuntimeError, match="SLACK_APP_TOKEN"):
        asyncio.run(slack_plugin.start())


def test_start_registers_itself_and_reconnects(monkeypatch):
    runs = []
    async def run_once():
        runs.append(1)
        await asyncio.sleep(0.01)
    monkeypatch.setattr(slack_plugin, "_run_once", run_once)
    async def _run():
        task = asyncio.create_task(slack_plugin.start())
        await asyncio.sleep(0.05)
        registered = router.registered()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return registered
    assert "slack" in asyncio.run(_run())
    assert len(runs) >= 2                    # a clean disconnect reconnects at once


def test_transient_error_does_not_escape_start(monkeypatch):
    monkeypatch.setattr(slack_plugin, "_RETRY_SECONDS", 0.001)
    run_once = AsyncMock(side_effect=aiohttp.ClientError("reset"))
    monkeypatch.setattr(slack_plugin, "_run_once", run_once)
    async def _run():
        task = asyncio.create_task(slack_plugin.start())
        await asyncio.sleep(0.05)
        alive = not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return alive, run_once.await_count
    alive, attempts = asyncio.run(_run())
    assert alive and attempts >= 2
