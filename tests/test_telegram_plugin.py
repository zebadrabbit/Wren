import os, asyncio, logging
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from wren import config, core, router
from wren.communication import telegram_plugin


@pytest.fixture(autouse=True)
def clean_router():
    # start() registers itself in router's module-level dict; wipe it either
    # side so a registration cannot leak into another test.
    router.reset()
    yield
    router.reset()


def _update(update_id: int = 1, *, text: str = "hello", user_id: int = 42,
            chat_type: str = "private", key: str = "message", **extra) -> dict:
    message = {
        "chat": {"id": user_id, "type": chat_type},
        "from": {"id": user_id},
        **extra,
    }
    if text is not None:
        message["text"] = text
    return {"update_id": update_id, key: message}


def _form_fields(form: aiohttp.FormData) -> dict:
    return {opts["name"]: value for opts, _headers, value in form._fields}


# ── the poll loop ────────────────────────────────────────────────────────────

def test_private_text_message_reaches_core():
    api = AsyncMock(return_value=[_update(text="add milk", user_id=42)])
    with patch.object(telegram_plugin, "_api", new=api), \
         patch.object(core, "handle_message", new=AsyncMock()) as handle:
        asyncio.run(telegram_plugin._poll_once(None))

    handle.assert_awaited_once()
    user_id, text, channel = handle.await_args.args
    assert user_id == 42
    assert text == "add milk"
    assert isinstance(channel, telegram_plugin.TelegramChannel)
    # which chat the reply goes to is the whole job of the channel: a stale
    # variable or the update_id here sends every answer into the void
    assert channel._chat_id == 42


def test_poll_once_advances_offset_past_processed_update():
    # the redelivery loop: without this, Telegram hands back the same update
    # on every poll and Wren answers it forever
    api = AsyncMock(return_value=[_update(update_id=7)])
    with patch.object(telegram_plugin, "_api", new=api), \
         patch.object(core, "handle_message", new=AsyncMock()):
        assert asyncio.run(telegram_plugin._poll_once(None)) == 8


def test_poll_once_advances_to_last_update_in_batch():
    api = AsyncMock(return_value=[_update(update_id=7), _update(update_id=9)])
    with patch.object(telegram_plugin, "_api", new=api), \
         patch.object(core, "handle_message", new=AsyncMock()) as handle:
        assert asyncio.run(telegram_plugin._poll_once(None)) == 10
    assert handle.await_count == 2


def test_poll_once_sends_the_offset_back_to_telegram():
    api = AsyncMock(return_value=[])
    with patch.object(telegram_plugin, "_api", new=api):
        asyncio.run(telegram_plugin._poll_once(8))

    assert api.await_args.args[0] == "getUpdates"
    assert api.await_args.kwargs["data"]["offset"] == 8


def test_first_poll_sends_no_offset():
    api = AsyncMock(return_value=[])
    with patch.object(telegram_plugin, "_api", new=api):
        assert asyncio.run(telegram_plugin._poll_once(None)) is None

    assert "offset" not in api.await_args.kwargs["data"]
    assert api.await_args.kwargs["data"]["timeout"] == telegram_plugin._LONG_POLL_SECONDS


def test_poll_once_keeps_prior_offset_when_nothing_arrives():
    with patch.object(telegram_plugin, "_api", new=AsyncMock(return_value=[])):
        assert asyncio.run(telegram_plugin._poll_once(8)) == 8


def test_group_chat_message_is_ignored_but_still_advances_offset():
    api = AsyncMock(return_value=[_update(update_id=3, chat_type="group")])
    with patch.object(telegram_plugin, "_api", new=api), \
         patch.object(core, "handle_message", new=AsyncMock()) as handle:
        assert asyncio.run(telegram_plugin._poll_once(None)) == 4

    handle.assert_not_called()


@pytest.mark.parametrize("update", [
    # a photo: a message with no "text" at all
    _update(update_id=3, text=None, photo=[{"file_id": "abc"}]),
    # a sticker
    _update(update_id=3, text=None, sticker={"file_id": "abc"}),
    # an edit arrives under a different key entirely
    _update(update_id=3, key="edited_message"),
    # so does a channel post
    _update(update_id=3, key="channel_post"),
    # an update type this plugin does not ask for and has never seen
    {"update_id": 3, "my_chat_member": {"chat": {"id": 42, "type": "private"}}},
    # an empty text message
    _update(update_id=3, text=""),
])
def test_non_text_update_is_skipped_without_raising(update):
    api = AsyncMock(return_value=[update])
    with patch.object(telegram_plugin, "_api", new=api), \
         patch.object(core, "handle_message", new=AsyncMock()) as handle:
        assert asyncio.run(telegram_plugin._poll_once(None)) == 4

    handle.assert_not_called()


def test_a_failed_message_does_not_stall_the_batch_or_the_offset():
    api = AsyncMock(return_value=[_update(update_id=7), _update(update_id=8)])
    handle = AsyncMock(side_effect=[RuntimeError("brain exploded"), None])
    with patch.object(telegram_plugin, "_api", new=api), \
         patch.object(core, "handle_message", new=handle):
        assert asyncio.run(telegram_plugin._poll_once(None)) == 9

    assert handle.await_count == 2


# ── start() ──────────────────────────────────────────────────────────────────

def test_start_raises_when_token_is_unset(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", "")
    with pytest.raises(RuntimeError, match="TELEGRAM_TOKEN"):
        asyncio.run(telegram_plugin.start())


def test_start_registers_itself_with_the_router(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", "token")

    async def _idle(offset):
        # must actually suspend: a mock that returns without awaiting anything
        # never yields to the loop, and start()'s while True would spin
        await asyncio.sleep(0.01)
        return offset

    monkeypatch.setattr(telegram_plugin, "_poll_once", _idle)

    async def _run():
        task = asyncio.create_task(telegram_plugin.start())
        await asyncio.sleep(0)          # let it get past register()
        registered = router.registered()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return registered

    assert "telegram" in asyncio.run(_run())


def test_start_feeds_each_poll_offset_into_the_next_poll(monkeypatch):
    # the two halves of the redelivery guard only work if start() hands
    # _poll_once's return value back in: drop the assignment and Telegram
    # replays the same batch forever, answered every time
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", "token")
    monkeypatch.setattr(core, "handle_message", AsyncMock())

    sent = []
    second_poll = asyncio.Event()

    async def fake_api(method, *, data=None):
        sent.append(data)
        if len(sent) == 1:
            return [_update(update_id=7)]
        second_poll.set()
        await asyncio.sleep(3600)   # park, so start()'s loop cannot spin

    monkeypatch.setattr(telegram_plugin, "_api", fake_api)

    async def _run():
        task = asyncio.create_task(telegram_plugin.start())
        await asyncio.wait_for(second_poll.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())

    assert "offset" not in sent[0]
    assert sent[1]["offset"] == 8


def test_transient_poll_error_does_not_escape_start(monkeypatch):
    # run.py gathers start(); an exception out of here kills Discord and the
    # web chat along with Telegram
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", "token")
    monkeypatch.setattr(telegram_plugin, "_RETRY_SECONDS", 0.001)
    poll = AsyncMock(side_effect=aiohttp.ClientError("connection reset"))
    monkeypatch.setattr(telegram_plugin, "_poll_once", poll)

    async def _run():
        task = asyncio.create_task(telegram_plugin.start())
        await asyncio.sleep(0.05)       # long enough for several failed polls
        still_running = not task.done()
        task.cancel()
        # cancellation must get through the loop's except clauses, or the task
        # could never be shut down
        with pytest.raises(asyncio.CancelledError):
            await task
        return still_running, poll.await_count

    still_running, attempts = asyncio.run(_run())
    assert still_running
    assert attempts >= 2                # it kept retrying rather than giving up


# ── notify() ─────────────────────────────────────────────────────────────────

def test_notify_returns_true_on_success():
    api = AsyncMock(return_value={"message_id": 1})
    with patch.object(telegram_plugin, "_api", new=api):
        assert asyncio.run(telegram_plugin.notify(42, "the kettle boiled")) is True

    assert api.await_args.args[0] == "sendMessage"
    assert api.await_args.kwargs["data"] == {"chat_id": 42, "text": "the kettle boiled"}


@pytest.mark.parametrize("code, description", [
    (403, "Forbidden: bot was blocked by the user"),
    (400, "Bad Request: chat not found"),
])
def test_notify_returns_false_on_permanent_failure(code, description):
    err = telegram_plugin.TelegramError("sendMessage", code, description)
    with patch.object(telegram_plugin, "_api", new=AsyncMock(side_effect=err)):
        assert asyncio.run(telegram_plugin.notify(42, "hi")) is False


@pytest.mark.parametrize("error", [
    telegram_plugin.TelegramError("sendMessage", 429, "Too Many Requests"),
    telegram_plugin.TelegramError("sendMessage", 502, "Bad Gateway"),
    aiohttp.ClientError("connection reset"),
])
def test_notify_raises_on_transient_failure(error):
    # router.notify()'s contract: False means permanent and lets the caller
    # consume the reminder. A blip must raise so it is retried instead.
    with patch.object(telegram_plugin, "_api", new=AsyncMock(side_effect=error)):
        with pytest.raises(type(error)):
            asyncio.run(telegram_plugin.notify(42, "hi"))


# ── TelegramChannel ──────────────────────────────────────────────────────────

def test_channel_send_posts_send_message():
    api = AsyncMock(return_value={})
    with patch.object(telegram_plugin, "_api", new=api):
        asyncio.run(telegram_plugin.TelegramChannel(42).send("on the list"))

    assert api.await_args.args[0] == "sendMessage"
    assert api.await_args.kwargs["data"] == {"chat_id": 42, "text": "on the list"}


def test_channel_send_file_posts_multipart_send_document():
    api = AsyncMock(return_value={})
    with patch.object(telegram_plugin, "_api", new=api):
        asyncio.run(telegram_plugin.TelegramChannel(42).send_file(b"# notes", "notes.md"))

    assert api.await_args.args[0] == "sendDocument"
    form = api.await_args.kwargs["data"]
    assert isinstance(form, aiohttp.FormData)
    assert _form_fields(form) == {"chat_id": "42", "document": b"# notes"}


def test_channel_history_is_none():
    assert asyncio.run(telegram_plugin.TelegramChannel(42).history()) is None


def test_channel_ack_seen_sends_typing():
    api = AsyncMock(return_value=True)
    with patch.object(telegram_plugin, "_api", new=api):
        asyncio.run(telegram_plugin.TelegramChannel(42).ack("seen"))

    assert api.await_args.args[0] == "sendChatAction"
    assert api.await_args.kwargs["data"] == {"chat_id": 42, "action": "typing"}


@pytest.mark.parametrize("state", ["done", "error"])
def test_channel_ack_done_and_error_are_no_ops(state):
    api = AsyncMock()
    with patch.object(telegram_plugin, "_api", new=api):
        asyncio.run(telegram_plugin.TelegramChannel(42).ack(state))

    api.assert_not_called()


def test_channel_ack_swallows_failures():
    # core awaits ack("seen") outside its try block, so a raise here would
    # drop the message before the brain ever saw it
    err = telegram_plugin.TelegramError("sendChatAction", 429, "Too Many Requests")
    with patch.object(telegram_plugin, "_api", new=AsyncMock(side_effect=err)):
        asyncio.run(telegram_plugin.TelegramChannel(42).ack("seen"))


# ── _api ─────────────────────────────────────────────────────────────────────

def test_api_raises_telegram_error_carrying_the_code():
    class _FakeResponse:
        async def json(self):
            return {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked"}

        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False

    class _FakeSession:
        def __init__(self, *a, **kw): pass
        def post(self, url, data=None): return _FakeResponse()
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False

    with patch.object(aiohttp, "ClientSession", _FakeSession):
        with pytest.raises(telegram_plugin.TelegramError) as excinfo:
            asyncio.run(telegram_plugin._api("sendMessage", data={"chat_id": 1}))

    assert excinfo.value.code == 403
    # the URL embeds the bot token and must never reach the journal
    assert "bot" + (config.TELEGRAM_TOKEN or "x") not in str(excinfo.value)


def test_transport_error_is_scrubbed_of_the_bot_token(monkeypatch, caplog):
    # The shape of a Telegram outage: the edge answers 502 with an HTML body,
    # so resp.json() raises ContentTypeError — and that class formats its
    # request URL into str(), which embeds the token. start() logs the
    # exception on every retry, so an unscrubbed one writes the token to the
    # journal once per backoff step for the whole outage.
    token = "8123456789:AAF-not-a-real-bot-token"
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", token)
    url = f"{telegram_plugin.API_ROOT}/bot{token}/getUpdates"
    boom = aiohttp.ContentTypeError(
        aiohttp.RequestInfo(url, "POST", {}, url), (), status=502,
        message="Attempt to decode JSON with unexpected mimetype: text/html")

    class _FakeResponse:
        async def json(self): raise boom
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False

    class _FakeSession:
        def __init__(self, *a, **kw): pass
        def post(self, url, data=None): return _FakeResponse()
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False

    with caplog.at_level(logging.WARNING):
        with patch.object(aiohttp, "ClientSession", _FakeSession):
            with pytest.raises(aiohttp.ClientError) as excinfo:
                asyncio.run(telegram_plugin._api("getUpdates"))
            # the ack() path swallows and logs its failure rather than raising
            asyncio.run(telegram_plugin.TelegramChannel(42).ack("seen"))

    assert token not in str(excinfo.value)
    assert "502" in str(excinfo.value)       # the useful detail survives
    assert token not in caplog.text
    assert caplog.text                       # the failure was still reported


def test_api_returns_the_result_field():
    class _FakeResponse:
        async def json(self):
            return {"ok": True, "result": [{"update_id": 1}]}

        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False

    class _FakeSession:
        def __init__(self, *a, **kw): pass
        def post(self, url, data=None): return _FakeResponse()
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False

    with patch.object(aiohttp, "ClientSession", _FakeSession):
        assert asyncio.run(telegram_plugin._api("getUpdates")) == [{"update_id": 1}]


def test_client_timeout_outlives_the_long_poll():
    # otherwise every quiet poll dies on our own deadline before Telegram answers
    assert telegram_plugin._CLIENT_TIMEOUT_SECONDS > telegram_plugin._LONG_POLL_SECONDS


# ── the 4096-char ceiling ────────────────────────────────────────────────────

def _sent_texts(api) -> list[str]:
    return [call.kwargs["data"]["text"] for call in api.await_args_list]


def test_send_splits_text_over_the_ceiling():
    long_text = "x" * 5000
    api = AsyncMock(return_value={})
    with patch.object(telegram_plugin, "_api", new=api):
        asyncio.run(telegram_plugin.TelegramChannel(42).send(long_text))

    parts = _sent_texts(api)
    assert len(parts) == 2
    assert all(len(p) <= 4096 for p in parts)
    # nothing may be silently dropped on the way through the splitter
    assert "".join(parts) == long_text


def test_send_splits_on_a_line_break_when_there_is_one():
    # a note dump is lines; cutting mid-line is uglier than cutting between two
    first_line = "a" * 4000
    long_text = f"{first_line}\n" + "b" * 500
    api = AsyncMock(return_value={})
    with patch.object(telegram_plugin, "_api", new=api):
        asyncio.run(telegram_plugin.TelegramChannel(42).send(long_text))

    parts = _sent_texts(api)
    assert parts == [first_line, "b" * 500]


def test_text_exactly_at_the_ceiling_is_one_message():
    api = AsyncMock(return_value={})
    with patch.object(telegram_plugin, "_api", new=api):
        asyncio.run(telegram_plugin.TelegramChannel(42).send("x" * 4096))

    assert len(api.await_args_list) == 1


def test_a_leading_newline_never_produces_an_empty_message():
    # an empty "text" is its own 400; splitting must not manufacture one
    api = AsyncMock(return_value={})
    with patch.object(telegram_plugin, "_api", new=api):
        asyncio.run(telegram_plugin.TelegramChannel(42).send("\n" + "x" * 5000))

    assert all(_sent_texts(api))


def test_notify_splits_too():
    # the whole point: a >4096 reminder used to come back as a 400, which
    # notify() reads as permanent, so the caller marked it delivered and
    # destroyed it. It must never reach Telegram over-long in the first place.
    api = AsyncMock(return_value={"message_id": 1})
    with patch.object(telegram_plugin, "_api", new=api):
        assert asyncio.run(telegram_plugin.notify(42, "x" * 9000)) is True

    assert len(api.await_args_list) == 3


# ── owner identity across surfaces ───────────────────────────────────────────

@pytest.fixture
def telegram_owner(monkeypatch):
    """The owner's Telegram id (999) is not their Wren id (1)."""
    monkeypatch.setattr(config, "TELEGRAM_OWNER_ID", 999)
    return 999


def test_the_owners_telegram_id_becomes_their_wren_id(telegram_owner):
    api = AsyncMock(return_value=[_update(text="add milk", user_id=999)])
    with patch.object(telegram_plugin, "_api", new=api), \
         patch.object(core, "handle_message", new=AsyncMock()) as handle:
        asyncio.run(telegram_plugin._poll_once(None))

    user_id, _text, channel = handle.await_args.args
    # core keys notes, reminders and pins off this, and its authz gate rejects
    # any id not in the whitelist — so the raw Telegram id would be a silent
    # drop, and whitelisting it separately would be a second, empty Wren
    assert user_id == config.WHITELIST["owner"]
    # ...but the reply still has to go back to the Telegram chat
    assert channel._chat_id == 999


def test_other_telegram_users_are_not_remapped(telegram_owner):
    api = AsyncMock(return_value=[_update(text="hi", user_id=42)])
    with patch.object(telegram_plugin, "_api", new=api), \
         patch.object(core, "handle_message", new=AsyncMock()) as handle:
        asyncio.run(telegram_plugin._poll_once(None))

    assert handle.await_args.args[0] == 42


def test_notify_to_the_owner_goes_to_their_telegram_chat(telegram_owner):
    api = AsyncMock(return_value={"message_id": 1})
    with patch.object(telegram_plugin, "_api", new=api):
        asyncio.run(telegram_plugin.notify(config.WHITELIST["owner"], "kettle boiled"))

    # a reminder filed on Discord fires through NOTIFY_VIA with the Wren id;
    # sending that number to Telegram is a 400 "chat not found"
    assert api.await_args.kwargs["data"]["chat_id"] == 999


def test_notify_to_anyone_else_is_unmapped(telegram_owner):
    api = AsyncMock(return_value={"message_id": 1})
    with patch.object(telegram_plugin, "_api", new=api):
        asyncio.run(telegram_plugin.notify(42, "hi"))

    assert api.await_args.kwargs["data"]["chat_id"] == 42


def test_nothing_is_remapped_when_telegram_owner_id_is_unset(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_OWNER_ID", 0)
    api = AsyncMock(return_value=[_update(text="hi", user_id=config.WHITELIST["owner"])])
    with patch.object(telegram_plugin, "_api", new=api), \
         patch.object(core, "handle_message", new=AsyncMock()) as handle:
        asyncio.run(telegram_plugin._poll_once(None))

    assert handle.await_args.args[0] == config.WHITELIST["owner"]


def test_telegram_send_card_falls_back_to_the_prose():
    api = AsyncMock(return_value={})
    with patch.object(telegram_plugin, "_api", new=api):
        asyncio.run(telegram_plugin.TelegramChannel(42).send_card(
            "shopping", {"items": []}, "Shopping list is empty."))

    assert api.await_args.args[0] == "sendMessage"
    assert api.await_args.kwargs["data"] == {"chat_id": 42, "text": "Shopping list is empty."}
