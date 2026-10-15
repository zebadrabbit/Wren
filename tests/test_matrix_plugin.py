import os, asyncio, logging, time
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import AsyncMock

import aiohttp
import pytest

from wren import config, contacts, core, router
from wren.communication import matrix_plugin

ME = "@wren:home.net"
OWNER = "@erin:home.net"
KIM = "@kim:home.net"


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    router.reset()
    monkeypatch.setenv("WREN_DB", str(tmp_path / "wren.db"))
    contacts.init_db()
    monkeypatch.setattr(config, "MATRIX_HOMESERVER", "https://home.net")
    monkeypatch.setattr(config, "MATRIX_TOKEN", "syt_secret_token")
    monkeypatch.setattr(config, "MATRIX_OWNER_ID", OWNER)
    monkeypatch.setattr(matrix_plugin, "_me", ME)
    monkeypatch.setattr(matrix_plugin, "_rooms", {})
    yield
    router.reset()


class Recorder:
    """A fake _api that records calls and answers from a dict keyed by
    (method, path-prefix)."""
    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}
    async def __call__(self, method, path, *, json=None, params=None, data=None, content_type=None):
        self.calls.append((method, path, json, params))
        for (m, prefix), value in self.answers.items():
            if m == method and path.startswith(prefix):
                if isinstance(value, Exception):
                    raise value
                return value
        return {}


# ── module shape ───────────────────────────────────────────────────────────────

def test_module_declares_a_chat_door():
    assert matrix_plugin.ROLE == "chat" and matrix_plugin.CAN_NOTIFY is True
    assert matrix_plugin.PLUGIN_NAME == "Matrix"
    assert matrix_plugin.MatrixChannel.NAME == "matrix"

def test_is_active_needs_homeserver_and_token(monkeypatch):
    assert matrix_plugin.is_active()
    monkeypatch.setattr(config, "MATRIX_TOKEN", "")
    assert not matrix_plugin.is_active()
    assert "MATRIX_TOKEN" in matrix_plugin.inactive_reason()


# ── id translation ─────────────────────────────────────────────────────────────

def test_the_owners_matrix_id_becomes_their_wren_id():
    assert matrix_plugin._wren_user_id(OWNER) == 1
    assert matrix_plugin._matrix_user_id(1) == OWNER

def test_a_contact_is_looked_up_and_a_stranger_is_none():
    contacts.register("kim", "matrix", KIM)
    wid = contacts.all()["kim"]
    assert matrix_plugin._wren_user_id(KIM) == wid
    assert matrix_plugin._matrix_user_id(wid) == KIM
    assert matrix_plugin._wren_user_id("@who:else.org") is None
    assert matrix_plugin._matrix_user_id(424242) is None


# ── _api ───────────────────────────────────────────────────────────────────────

def test_api_raises_matrix_error_with_errcode_and_status(monkeypatch):
    class Resp:
        status = 403
        async def json(self, **k): return {"errcode": "M_FORBIDDEN", "error": "not in room"}
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    class Session:
        def __init__(self, *a, **k): pass
        def request(self, method, url, **k):
            assert url == "https://home.net/_matrix/client/v3/rooms/!r:home.net/send/m.room.message/t1"
            assert "syt_" not in url
            assert k["headers"]["Authorization"] == "Bearer syt_secret_token"
            return Resp()
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    with pytest.raises(matrix_plugin.MatrixError) as info:
        asyncio.run(matrix_plugin._api("PUT", "/_matrix/client/v3/rooms/!r:home.net/send/m.room.message/t1", json={}))
    assert (info.value.errcode, info.value.status) == ("M_FORBIDDEN", 403)
    assert "syt_" not in str(info.value)

def test_a_proxy_error_page_is_a_matrix_error_with_its_status(monkeypatch):
    class Resp:
        status = 502
        async def json(self, **k): raise aiohttp.ContentTypeError(None, ())
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    class Session:
        def __init__(self, *a, **k): pass
        def request(self, method, url, **k): return Resp()
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    with pytest.raises(matrix_plugin.MatrixError) as info:
        asyncio.run(matrix_plugin._api("GET", "/_matrix/client/v3/sync"))
    assert (info.value.errcode, info.value.status) == ("M_UNKNOWN", 502)

def test_transport_error_is_scrubbed_of_the_token(monkeypatch):
    class Session:
        def __init__(self, *a, **k): pass
        def request(self, method, url, **k):
            raise aiohttp.ClientError("boom syt_secret_token")
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    with pytest.raises(aiohttp.ClientError) as info:
        asyncio.run(matrix_plugin._api("GET", "/_matrix/client/v3/account/whoami"))
    assert "syt_" not in str(info.value) and "<token>" in str(info.value)


# ── rooms ──────────────────────────────────────────────────────────────────────

def test_dm_room_reuses_the_two_member_room_with_that_person(monkeypatch):
    matrix_plugin._rooms.update({"!a:home.net": {ME, OWNER}, "!b:home.net": {ME, OWNER, KIM}})
    api = Recorder()
    monkeypatch.setattr(matrix_plugin, "_api", api)
    assert asyncio.run(matrix_plugin._dm_room(OWNER)) == "!a:home.net"
    assert api.calls == []

def test_dm_room_creates_an_unencrypted_direct_room_and_caches_it(monkeypatch):
    api = Recorder({("POST", "/_matrix/client/v3/createRoom"): {"room_id": "!new:home.net"}})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    assert asyncio.run(matrix_plugin._dm_room(KIM)) == "!new:home.net"
    method, path, body, _ = api.calls[0]
    assert body == {"is_direct": True, "invite": [KIM], "preset": "trusted_private_chat"}
    assert matrix_plugin._rooms["!new:home.net"] == {ME, KIM}


# ── channel ────────────────────────────────────────────────────────────────────

def test_channel_send_puts_text_events_in_chunks(monkeypatch):
    api = Recorder()
    monkeypatch.setattr(matrix_plugin, "_api", api)
    ch = matrix_plugin.MatrixChannel("!r:home.net")
    asyncio.run(ch.send("a" * 32000 + "\n" + "b"))
    assert len(api.calls) == 2
    method, path, body, _ = api.calls[0]
    assert method == "PUT" and path.startswith("/_matrix/client/v3/rooms/!r:home.net/send/m.room.message/")
    assert body == {"msgtype": "m.text", "body": "a" * 32000}
    assert api.calls[0][1] != api.calls[1][1]          # a fresh txn id per event

def test_channel_send_file_uploads_then_sends_an_image_or_file_event(monkeypatch):
    api = Recorder({("POST", "/_matrix/media/v3/upload"): {"content_uri": "mxc://home.net/abc"}})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    ch = matrix_plugin.MatrixChannel("!r:home.net")
    asyncio.run(ch.send_file(b"\x89PNG\r\n\x1a\n" + b"0" * 20, "cat.png"))
    assert api.calls[0][3] == {"filename": "cat.png"}
    assert api.calls[1][2] == {"msgtype": "m.image", "body": "cat.png", "url": "mxc://home.net/abc",
                               "info": {"mimetype": "image/png", "size": 28}}
    asyncio.run(ch.send_file(b"%PDF-1.4 ...", "doc.pdf"))
    assert api.calls[3][2]["msgtype"] == "m.file" and api.calls[3][2]["info"]["mimetype"] == "application/pdf"

def test_channel_ack_seen_sets_typing_and_swallows_failure(monkeypatch, caplog):
    api = Recorder({("PUT", "/_matrix/client/v3/rooms/!r:home.net/typing/"): matrix_plugin.MatrixError("M_LIMIT_EXCEEDED", 429)})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    ch = matrix_plugin.MatrixChannel("!r:home.net")
    with caplog.at_level(logging.WARNING):
        asyncio.run(ch.ack("seen"))
    assert api.calls[0][1] == f"/_matrix/client/v3/rooms/!r:home.net/typing/{ME}"
    assert api.calls[0][2] == {"typing": True, "timeout": 30000}
    assert "typing" in caplog.text
    asyncio.run(ch.ack("done"))
    assert len(api.calls) == 1

def test_channel_send_card_falls_back_to_the_prose(monkeypatch):
    api = Recorder()
    monkeypatch.setattr(matrix_plugin, "_api", api)
    asyncio.run(matrix_plugin.MatrixChannel("!r:home.net").send_card("shopping", {}, "The list is empty."))
    assert api.calls[0][2]["body"] == "The list is empty."


# ── notify ─────────────────────────────────────────────────────────────────────

def test_notify_sends_into_the_owners_dm_room(monkeypatch):
    matrix_plugin._rooms["!a:home.net"] = {ME, OWNER}
    api = Recorder()
    monkeypatch.setattr(matrix_plugin, "_api", api)
    assert asyncio.run(matrix_plugin.notify(1, "dinner")) is True
    assert api.calls[0][1].startswith("/_matrix/client/v3/rooms/!a:home.net/send/")
    assert api.calls[0][2]["body"] == "dinner"

def test_notify_reuses_the_room_it_created(monkeypatch):
    contacts.register("kim", "matrix", KIM)
    wid = contacts.all()["kim"]
    api = Recorder({("POST", "/_matrix/client/v3/createRoom"): {"room_id": "!new:home.net"}})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    assert asyncio.run(matrix_plugin.notify(wid, "one")) is True
    assert asyncio.run(matrix_plugin.notify(wid, "two")) is True
    creates = [c for c in api.calls if c[1].endswith("/createRoom")]
    assert len(creates) == 1

def test_notify_to_someone_not_on_matrix_is_permanent(monkeypatch):
    api = Recorder()
    monkeypatch.setattr(matrix_plugin, "_api", api)
    assert asyncio.run(matrix_plugin.notify(424242, "dinner")) is False
    assert api.calls == []

def test_notify_returns_false_when_forbidden_and_raises_otherwise(monkeypatch):
    matrix_plugin._rooms["!a:home.net"] = {ME, OWNER}
    api = Recorder({("PUT", "/_matrix/client/v3/rooms/!a:home.net/send/"): matrix_plugin.MatrixError("M_FORBIDDEN", 403)})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    assert asyncio.run(matrix_plugin.notify(1, "dinner")) is False
    api.answers = {("PUT", "/_matrix/client/v3/rooms/!a:home.net/send/"): matrix_plugin.MatrixError("M_LIMIT_EXCEEDED", 429)}
    with pytest.raises(matrix_plugin.MatrixError):
        asyncio.run(matrix_plugin.notify(1, "dinner"))


# ── the loop ───────────────────────────────────────────────────────────────────

def _member(user, membership="join", sender=None):
    return {"type": "m.room.member", "state_key": user, "sender": sender or user,
            "content": {"membership": membership}}

def _msg(sender, body, ts=None, **content):
    return {"type": "m.room.message", "sender": sender, "event_id": f"$ev{abs(hash(body))}",
            "origin_server_ts": int((ts or time.time()) * 1000),
            "content": {"msgtype": "m.text", "body": body, **content}}

def _sync(joined=None, invited=None, next_batch="s2"):
    return {"next_batch": next_batch, "rooms": {"join": joined or {}, "invite": invited or {}}}

def _joined(room, members, events):
    return {room: {"state": {"events": [_member(m) for m in members]}, "timeline": {"events": events}}}


def test_incoming_text_and_media():
    assert matrix_plugin._incoming(_msg(OWNER, "hi")) == (OWNER, "hi", [])
    img = _msg(OWNER, "cat.jpg", msgtype="m.image", url="mxc://home.net/abc",
               info={"mimetype": "image/jpeg", "size": 10})
    assert matrix_plugin._incoming(img) == (OWNER, "", [{"url": "mxc://home.net/abc", "filename": "cat.jpg",
                                                         "mime": "image/jpeg", "size": 10}])
    cap = _msg(OWNER, "look at this", msgtype="m.image", url="mxc://home.net/abc", filename="cat.jpg",
               info={"mimetype": "image/jpeg", "size": 10})
    assert matrix_plugin._incoming(cap)[1] == "look at this"

@pytest.mark.parametrize("event", [
    _msg(ME, "my own echo"),
    {"type": "m.room.encrypted", "sender": OWNER, "content": {}},
    {"type": "m.reaction", "sender": OWNER, "content": {}},
    _msg(OWNER, "* add milk", **{"m.relates_to": {"rel_type": "m.replace", "event_id": "$orig"},
                                 "m.new_content": {"msgtype": "m.text", "body": "add milk"}}),
    _msg(OWNER, "clip", msgtype="m.video", url="mxc://home.net/v", info={"mimetype": "video/mp4"}),
])
def test_incoming_ignores_what_it_cannot_act_on(event):
    assert matrix_plugin._incoming(event) is None


def test_initial_sync_acts_on_nothing_but_learns_rooms(monkeypatch):
    api = Recorder({("GET", f"{matrix_plugin.V3}/sync"): _sync(
        _joined("!a:home.net", [ME, OWNER], [_msg(OWNER, "old news")]), next_batch="s1")})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    handle = AsyncMock()
    monkeypatch.setattr(core, "handle_message", handle)
    assert asyncio.run(matrix_plugin._sync_once(None)) == "s1"
    handle.assert_not_awaited()
    assert matrix_plugin._rooms == {"!a:home.net": {ME, OWNER}}
    assert "since" not in (api.calls[0][3] or {})


def test_a_dm_message_reaches_core_with_the_room_channel(monkeypatch):
    matrix_plugin._rooms["!a:home.net"] = {ME, OWNER}
    api = Recorder({("GET", f"{matrix_plugin.V3}/sync"): _sync(
        _joined("!a:home.net", [ME, OWNER], [_msg(OWNER, "add milk")]))})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    handle = AsyncMock()
    monkeypatch.setattr(core, "handle_message", handle)
    assert asyncio.run(matrix_plugin._sync_once("s1")) == "s2"
    assert api.calls[0][3]["since"] == "s1" and api.calls[0][3]["timeout"] == 30000
    args = handle.await_args
    assert args.args[0] == 1 and args.args[1] == "add milk" and args.args[2].NAME == "matrix"
    assert args.args[2]._room == "!a:home.net"


def test_a_group_room_and_a_stranger_are_ignored(monkeypatch):
    api = Recorder({("GET", f"{matrix_plugin.V3}/sync"): _sync({
        **_joined("!g:home.net", [ME, OWNER, KIM], [_msg(OWNER, "group chat")]),
        **_joined("!s:home.net", [ME, "@who:else.org"], [_msg("@who:else.org", "hey")]),
    })})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    handle = AsyncMock()
    monkeypatch.setattr(core, "handle_message", handle)
    asyncio.run(matrix_plugin._sync_once("s1"))
    handle.assert_not_awaited()


def test_invites_from_known_people_are_joined_and_others_ignored(monkeypatch):
    contacts.register("kim", "matrix", KIM)
    invited = {
        "!k:home.net": {"invite_state": {"events": [_member(ME, "invite", sender=KIM)]}},
        "!x:home.net": {"invite_state": {"events": [_member(ME, "invite", sender="@who:else.org")]}},
    }
    api = Recorder({("GET", f"{matrix_plugin.V3}/sync"): _sync(invited=invited)})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    asyncio.run(matrix_plugin._sync_once("s1"))
    joins = [c[1] for c in api.calls if c[0] == "POST" and c[1].endswith("/join")]
    assert joins == [f"{matrix_plugin.V3}/rooms/!k:home.net/join"]


def test_an_encrypted_room_is_skipped_and_logged_once(monkeypatch, caplog):
    matrix_plugin._encrypted.clear()
    room = _joined("!e:home.net", [ME, OWNER], [
        {"type": "m.room.encrypted", "sender": OWNER, "content": {}},
        {"type": "m.room.encrypted", "sender": OWNER, "content": {}}])
    api = Recorder({("GET", f"{matrix_plugin.V3}/sync"): _sync(room)})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    handle = AsyncMock()
    monkeypatch.setattr(core, "handle_message", handle)
    with caplog.at_level(logging.WARNING):
        asyncio.run(matrix_plugin._sync_once("s1"))
        asyncio.run(matrix_plugin._sync_once("s2"))
    handle.assert_not_awaited()
    assert caplog.text.count("encrypted") == 1


def test_stale_message_gets_the_offline_notice(monkeypatch):
    matrix_plugin._rooms["!a:home.net"] = {ME, OWNER}
    api = Recorder({("GET", f"{matrix_plugin.V3}/sync"): _sync(
        _joined("!a:home.net", [ME, OWNER], [_msg(OWNER, "ages ago", ts=time.time() - 3600)]))})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    handle = AsyncMock(); sent = AsyncMock()
    monkeypatch.setattr(core, "handle_message", handle)
    monkeypatch.setattr(matrix_plugin, "_send_text", sent)
    asyncio.run(matrix_plugin._sync_once("s1"))
    handle.assert_not_awaited()
    assert sent.await_args.args[0] == "!a:home.net" and "offline" in sent.await_args.args[1]


def test_media_is_downloaded_and_handed_to_core(monkeypatch):
    matrix_plugin._rooms["!a:home.net"] = {ME, OWNER}
    img = _msg(OWNER, "cat.jpg", msgtype="m.image", url="mxc://home.net/abc", info={"mimetype": "image/jpeg", "size": 10})
    api = Recorder({("GET", f"{matrix_plugin.V3}/sync"): _sync(_joined("!a:home.net", [ME, OWNER], [img]))})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    monkeypatch.setattr(matrix_plugin, "_download", AsyncMock(return_value=b"jpegbytes"))
    handle = AsyncMock()
    monkeypatch.setattr(core, "handle_message", handle)
    asyncio.run(matrix_plugin._sync_once("s1"))
    files = handle.await_args.kwargs["files"]
    assert [(f.filename, f.mime, f.data) for f in files] == [("cat.jpg", "image/jpeg", b"jpegbytes")]


def test_a_failed_message_does_not_stall_the_batch(monkeypatch, caplog):
    matrix_plugin._rooms["!a:home.net"] = {ME, OWNER}
    api = Recorder({("GET", f"{matrix_plugin.V3}/sync"): _sync(
        _joined("!a:home.net", [ME, OWNER], [_msg(OWNER, "first"), _msg(OWNER, "second")]))})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    handle = AsyncMock(side_effect=[RuntimeError("brain down"), None])
    monkeypatch.setattr(core, "handle_message", handle)
    with caplog.at_level(logging.ERROR):
        assert asyncio.run(matrix_plugin._sync_once("s1")) == "s2"
    assert handle.await_count == 2
    assert "brain down" in caplog.text and "first" not in caplog.text


def test_start_raises_without_settings(monkeypatch):
    monkeypatch.setattr(config, "MATRIX_TOKEN", "")
    with pytest.raises(RuntimeError, match="MATRIX_TOKEN"):
        asyncio.run(matrix_plugin.start())


def test_start_learns_who_it_is_opens_the_owners_room_and_registers(monkeypatch):
    monkeypatch.setattr(matrix_plugin, "_me", "")
    api = Recorder({("GET", f"{matrix_plugin.V3}/account/whoami"): {"user_id": ME},
                    ("POST", f"{matrix_plugin.V3}/createRoom"): {"room_id": "!o:home.net"}})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    async def idle(since):
        await asyncio.sleep(0.01)
        return since or "s1"
    monkeypatch.setattr(matrix_plugin, "_sync_once", idle)
    async def _run():
        task = asyncio.create_task(matrix_plugin.start())
        await asyncio.sleep(0.05)
        registered = router.registered()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return registered
    assert "matrix" in asyncio.run(_run())
    assert matrix_plugin._me == ME
    assert matrix_plugin._rooms == {"!o:home.net": {ME, OWNER}}


def test_start_feeds_each_since_into_the_next_sync(monkeypatch):
    monkeypatch.setattr(matrix_plugin, "_rooms", {"!o:home.net": {ME, OWNER}})
    seen = []
    second = asyncio.Event()
    async def fake_sync(since):
        seen.append(since)
        if len(seen) == 2:
            second.set()
            await asyncio.sleep(3600)
        return f"s{len(seen)}"
    monkeypatch.setattr(matrix_plugin, "_sync_once", fake_sync)
    monkeypatch.setattr(matrix_plugin, "_api", Recorder({("GET", f"{matrix_plugin.V3}/account/whoami"): {"user_id": ME}}))
    async def _run():
        task = asyncio.create_task(matrix_plugin.start())
        await asyncio.wait_for(second.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(_run())
    assert seen == [None, "s1"]


def test_transient_error_does_not_escape_start(monkeypatch):
    monkeypatch.setattr(matrix_plugin, "_RETRY_SECONDS", 0.001)
    monkeypatch.setattr(matrix_plugin, "_api", Recorder({("GET", f"{matrix_plugin.V3}/account/whoami"): {"user_id": ME}}))
    monkeypatch.setattr(matrix_plugin, "_rooms", {"!o:home.net": {ME, OWNER}})
    sync = AsyncMock(side_effect=aiohttp.ClientError("reset"))
    monkeypatch.setattr(matrix_plugin, "_sync_once", sync)
    async def _run():
        task = asyncio.create_task(matrix_plugin.start())
        await asyncio.sleep(0.05)
        alive = not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return alive, sync.await_count
    alive, attempts = asyncio.run(_run())
    assert alive and attempts >= 2


def test_a_room_wren_created_stays_the_dm_room_while_the_invite_is_pending(monkeypatch):
    matrix_plugin._rooms["!new:home.net"] = {ME, KIM}
    room = {"!new:home.net": {"state": {"events": [
        _member(ME),
        {"type": "m.room.member", "state_key": KIM, "sender": ME, "content": {"membership": "invite"}}]},
        "timeline": {"events": []}}}
    api = Recorder({("GET", f"{matrix_plugin.V3}/sync"): _sync(room)})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    asyncio.run(matrix_plugin._sync_once("s1"))
    assert matrix_plugin._rooms["!new:home.net"] == {ME, KIM}
    assert asyncio.run(matrix_plugin._dm_room(KIM)) == "!new:home.net"
    assert not [c for c in api.calls if c[1].endswith("/createRoom")]


def test_sync_filter_loads_full_membership():
    assert "lazy_load_members" not in matrix_plugin._SYNC_FILTER


def test_a_failing_owner_room_does_not_block_the_sync(monkeypatch):
    monkeypatch.setattr(matrix_plugin, "_RETRY_SECONDS", 0.001)
    monkeypatch.setattr(matrix_plugin, "_api", Recorder({
        ("GET", f"{matrix_plugin.V3}/account/whoami"): {"user_id": ME},
        ("POST", f"{matrix_plugin.V3}/createRoom"): matrix_plugin.MatrixError("M_INVALID_PARAM", 400)}))
    seen = []
    second = asyncio.Event()
    async def fake_sync(since):
        seen.append(since)
        if len(seen) >= 3:
            second.set()
        await asyncio.sleep(0.001)
        return "s1"
    monkeypatch.setattr(matrix_plugin, "_sync_once", fake_sync)
    async def _run():
        task = asyncio.create_task(matrix_plugin.start())
        await asyncio.wait_for(second.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(_run())
    assert len(seen) >= 2


def test_invites_pending_at_startup_are_joined_but_history_is_not_acted_on(monkeypatch):
    contacts.register("kim", "matrix", KIM)
    matrix_plugin._rooms["!a:home.net"] = {ME, OWNER}
    body = _sync(_joined("!a:home.net", [ME, OWNER], [_msg(OWNER, "old news")]),
                 invited={"!k:home.net": {"invite_state": {"events": [_member(ME, "invite", sender=KIM)]}}},
                 next_batch="s1")
    api = Recorder({("GET", f"{matrix_plugin.V3}/sync"): body})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    handle = AsyncMock()
    monkeypatch.setattr(core, "handle_message", handle)
    assert asyncio.run(matrix_plugin._sync_once(None)) == "s1"
    assert [c[1] for c in api.calls if c[0] == "POST" and c[1].endswith("/join")] == \
        [f"{matrix_plugin.V3}/rooms/!k:home.net/join"]
    handle.assert_not_awaited()
