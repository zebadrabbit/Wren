import os, asyncio, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

import base64
from aiohttp.test_utils import TestClient, TestServer

from wren import config
from wren import conversations
from wren import core
from wren.communication import http_plugin as http_surface
from wren.communication import webchat

TOKEN_A, USER_A = "tok-a", 1
TOKEN_B, USER_B = "tok-b", 2
TOKENS = {TOKEN_A: USER_A, TOKEN_B: USER_B}


@pytest.fixture(autouse=True)
def tokens(monkeypatch, isolated_db):
    # isolated_db (conftest) must run first so init_db lands on the tmp file
    monkeypatch.setattr(config, "WREN_TOKENS", dict(TOKENS))
    monkeypatch.setattr(config, "id_to_name", lambda: {USER_A: "ann", USER_B: "bob"})
    conversations.init_db()


async def _request(method, path, *, token=None, expect_json=True, **kw):
    headers = kw.pop("headers", {})
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    async with TestClient(TestServer(http_surface.build_app())) as client:
        resp = await getattr(client, method)(path, headers=headers, **kw)
        body = await resp.json() if expect_json else await resp.text()
        return resp.status, body


def call(method, path, **kw):
    return asyncio.run(_request(method, path, **kw))


async def _page_request(**kw):
    async with TestClient(TestServer(http_surface.build_app())) as client:
        resp = await client.get("/", **kw)
        return resp.status, resp.content_type, await resp.text()


def _replies(*texts, files=()):
    async def fake(user_id, text, channel):
        for t in texts:
            await channel.send(t)
        for data, name in files:
            await channel.send_file(data, name)
    return fake


def new_convo(token=TOKEN_A):
    status, body = call("post", "/api/conversations", token=token)
    assert status == 200
    return body["id"]


# ── the page shell ─────────────────────────────────────────────────────────

def test_page_is_served_without_a_token():
    status, content_type, text = asyncio.run(_page_request())
    assert status == 200
    assert content_type == "text/html"
    assert "<title>Wren</title>" in text


def test_page_ignores_a_bad_token():
    # the shell holds no user data, so a stale token must not lock you out of
    # the very screen that lets you paste a new one
    status, _, text = asyncio.run(
        _page_request(headers={"Authorization": "Bearer nope"})
    )
    assert status == 200
    assert "<title>Wren</title>" in text


# ── auth on every api route ────────────────────────────────────────────────

API_ROUTES = [
    ("get", "/api/conversations"),
    ("post", "/api/conversations"),
    ("get", "/api/conversations/1"),
    ("patch", "/api/conversations/1"),
    ("delete", "/api/conversations/1"),
    ("post", "/api/conversations/1/message"),
]


@pytest.mark.parametrize("method,path", API_ROUTES)
def test_api_route_without_token_is_401(method, path):
    status, body = call(method, path, json={"text": "hi", "title": "t"})
    assert status == 401
    assert body["error"] == "unauthorized"


@pytest.mark.parametrize("method,path", API_ROUTES)
def test_api_route_with_wrong_token_is_401(method, path):
    status, body = call(method, path, token="not-a-token",
                        json={"text": "hi", "title": "t"})
    assert status == 401
    assert body["error"] == "unauthorized"


def test_message_route_never_reaches_core_without_a_token(monkeypatch):
    called = []
    monkeypatch.setattr(core, "handle_message", lambda *a: called.append(a))
    status, _ = call("post", "/api/conversations/1/message", json={"text": "hi"})
    assert status == 401
    assert called == []


# ── conversation crud ──────────────────────────────────────────────────────

def test_create_then_list():
    status, body = call("post", "/api/conversations", token=TOKEN_A)
    assert status == 200
    assert body["title"] == "New chat"
    assert isinstance(body["id"], int)

    status, listed = call("get", "/api/conversations", token=TOKEN_A)
    assert status == 200
    assert [c["id"] for c in listed] == [body["id"]]
    assert listed[0]["title"] == "New chat"
    assert listed[0]["created_at"] and listed[0]["updated_at"]


def test_list_is_empty_for_a_fresh_user():
    status, listed = call("get", "/api/conversations", token=TOKEN_B)
    assert status == 200
    assert listed == []


def test_get_returns_messages(monkeypatch):
    monkeypatch.setattr(core, "handle_message", _replies("Saved."))
    cid = new_convo()
    call("post", f"/api/conversations/{cid}/message", token=TOKEN_A,
         json={"text": "note this"})

    status, body = call("get", f"/api/conversations/{cid}", token=TOKEN_A)
    assert status == 200
    assert body["id"] == cid
    assert [(m["role"], m["content"]) for m in body["messages"]] == [
        ("user", "note this"),
        ("assistant", "Saved."),
    ]
    assert all(m["created_at"] for m in body["messages"])


def test_get_on_a_brand_new_conversation_has_no_messages():
    cid = new_convo()
    status, body = call("get", f"/api/conversations/{cid}", token=TOKEN_A)
    assert status == 200
    assert body["messages"] == []


def test_patch_renames():
    cid = new_convo()
    status, body = call("patch", f"/api/conversations/{cid}", token=TOKEN_A,
                        json={"title": "  Plumber  saga "})
    assert status == 200
    assert body == {"ok": True}

    _, listed = call("get", "/api/conversations", token=TOKEN_A)
    assert listed[0]["title"] == "Plumber saga"


def test_patch_with_blank_title_is_400():
    cid = new_convo()
    status, body = call("patch", f"/api/conversations/{cid}", token=TOKEN_A,
                        json={"title": "   "})
    assert status == 400
    assert "title" in body["error"]


def test_patch_with_non_json_body_is_400():
    cid = new_convo()
    status, body = call("patch", f"/api/conversations/{cid}", token=TOKEN_A,
                        data="not json")
    assert status == 400
    assert "JSON" in body["error"]


def test_delete_removes_conversation_and_its_messages(monkeypatch):
    monkeypatch.setattr(core, "handle_message", _replies("ok"))
    cid = new_convo()
    call("post", f"/api/conversations/{cid}/message", token=TOKEN_A,
         json={"text": "hello"})
    assert conversations.messages(cid)

    status, body = call("delete", f"/api/conversations/{cid}", token=TOKEN_A)
    assert status == 200
    assert body == {"ok": True}

    assert conversations.messages(cid) == []
    assert call("get", f"/api/conversations/{cid}", token=TOKEN_A)[0] == 404
    assert call("get", "/api/conversations", token=TOKEN_A)[1] == []


# ── unknown / malformed ids ────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["abc", "1.5", "%20", "12x", "-", "1;drop"])
def test_non_integer_id_is_404_not_500(bad):
    status, body = call("get", f"/api/conversations/{bad}", token=TOKEN_A)
    assert status == 404
    assert body["error"] == "no such conversation"


def test_trailing_slash_id_is_404_not_500():
    # no route matches, so aiohttp's own plain-text 404 answers — still not a 500
    status, text = call("get", "/api/conversations/", token=TOKEN_A, expect_json=False)
    assert status == 404
    assert "Traceback" not in text


def test_unknown_id_is_404():
    for method, path in (("get", "/api/conversations/99999"),
                         ("patch", "/api/conversations/99999"),
                         ("delete", "/api/conversations/99999"),
                         ("post", "/api/conversations/99999/message")):
        status, body = call(method, path, token=TOKEN_A,
                            json={"text": "hi", "title": "t"})
        assert status == 404, (method, path, status)
        assert body["error"] == "no such conversation"


def test_non_integer_id_on_message_route_is_404_not_500(monkeypatch):
    called = []
    monkeypatch.setattr(core, "handle_message", lambda *a: called.append(a))
    status, _ = call("post", "/api/conversations/abc/message", token=TOKEN_A,
                     json={"text": "hi"})
    assert status == 404
    assert called == []


# ── cross-user isolation ───────────────────────────────────────────────────

def test_other_users_conversation_is_404_on_get(monkeypatch):
    monkeypatch.setattr(core, "handle_message", _replies("ok"))
    cid = new_convo(TOKEN_A)
    call("post", f"/api/conversations/{cid}/message", token=TOKEN_A,
         json={"text": "private"})

    status, body = call("get", f"/api/conversations/{cid}", token=TOKEN_B)
    # exactly 404: a 403 would confirm the conversation exists
    assert status == 404
    assert body["error"] == "no such conversation"
    assert "private" not in str(body)


def test_other_users_conversation_is_404_on_patch():
    cid = new_convo(TOKEN_A)
    status, _ = call("patch", f"/api/conversations/{cid}", token=TOKEN_B,
                     json={"title": "hijacked"})
    assert status == 404
    _, listed = call("get", "/api/conversations", token=TOKEN_A)
    assert listed[0]["title"] == "New chat"


def test_other_users_conversation_is_404_on_delete():
    cid = new_convo(TOKEN_A)
    status, _ = call("delete", f"/api/conversations/{cid}", token=TOKEN_B)
    assert status == 404
    assert [c["id"] for c in call("get", "/api/conversations", token=TOKEN_A)[1]] == [cid]


def test_other_users_conversation_is_404_on_post_message(monkeypatch):
    called = []

    async def fake(user_id, text, channel):
        called.append((user_id, text))
        await channel.send("leaked")

    monkeypatch.setattr(core, "handle_message", fake)
    cid = new_convo(TOKEN_A)
    status, body = call("post", f"/api/conversations/{cid}/message", token=TOKEN_B,
                        json={"text": "whose chat is this"})
    assert status == 404
    assert body["error"] == "no such conversation"
    assert called == []                     # core never dispatched
    assert conversations.messages(cid) == []  # nothing written to A's convo


def test_list_does_not_leak_other_users_conversations():
    a1, a2 = new_convo(TOKEN_A), new_convo(TOKEN_A)
    b1 = new_convo(TOKEN_B)
    assert sorted(c["id"] for c in call("get", "/api/conversations", token=TOKEN_A)[1]) == [a1, a2]
    assert [c["id"] for c in call("get", "/api/conversations", token=TOKEN_B)[1]] == [b1]


# ── posting a message ──────────────────────────────────────────────────────

def test_message_dispatches_and_returns_replies(monkeypatch):
    seen = {}

    async def fake(user_id, text, channel):
        seen["user_id"], seen["text"] = user_id, text
        await channel.send("Saved.")

    monkeypatch.setattr(core, "handle_message", fake)
    cid = new_convo()
    status, body = call("post", f"/api/conversations/{cid}/message", token=TOKEN_A,
                        json={"text": "note this"})
    assert status == 200
    assert body["replies"] == ["Saved."]
    assert body["files"] == []
    assert seen == {"user_id": USER_A, "text": "note this"}


def test_message_persists_both_roles(monkeypatch):
    monkeypatch.setattr(core, "handle_message", _replies("one", "two"))
    cid = new_convo()
    call("post", f"/api/conversations/{cid}/message", token=TOKEN_A,
         json={"text": "hello"})

    rows = conversations.messages(cid)
    assert [(r["role"], r["content"]) for r in rows] == [
        ("user", "hello"),
        ("assistant", "one"),
        ("assistant", "two"),
    ]


def test_message_returns_files_base64(monkeypatch):
    monkeypatch.setattr(core, "handle_message",
                        _replies("here", files=[(b"# notes", "notes.md")]))
    cid = new_convo()
    status, body = call("post", f"/api/conversations/{cid}/message", token=TOKEN_A,
                        json={"text": "export"})
    assert status == 200
    assert body["files"] == [
        {"filename": "notes.md", "data": base64.b64encode(b"# notes").decode()}
    ]


def test_send_file_does_not_write_a_message_row(monkeypatch):
    monkeypatch.setattr(core, "handle_message",
                        _replies(files=[(b"data", "export.md")]))
    cid = new_convo()
    status, body = call("post", f"/api/conversations/{cid}/message", token=TOKEN_A,
                        json={"text": "export"})
    assert status == 200
    assert body["replies"] == []
    assert len(body["files"]) == 1
    # only the user's own message got persisted — the attachment is inline-only
    assert [(r["role"], r["content"]) for r in conversations.messages(cid)] == [
        ("user", "export")
    ]


def test_webchannel_send_file_collects_without_persisting():
    cid = conversations.create(USER_A)
    channel = webchat.WebChannel(cid, USER_A)

    async def go():
        await channel.send_file(b"bytes", "a.md")
        await channel.ack("done")

    asyncio.run(go())
    assert channel.files == [(b"bytes", "a.md")]
    assert channel.sent == []
    assert conversations.messages(cid) == []


def test_webchannel_send_persists_an_assistant_row():
    cid = conversations.create(USER_A)
    channel = webchat.WebChannel(cid, USER_A)
    asyncio.run(channel.send("hi there"))
    assert channel.sent == ["hi there"]
    assert [(r["role"], r["content"]) for r in conversations.messages(cid)] == [
        ("assistant", "hi there")
    ]


def test_message_updates_updated_at(monkeypatch):
    ticks = iter([f"2026-01-01T00:00:{n:02d}+00:00" for n in range(1, 60)])
    monkeypatch.setattr(conversations, "_now", lambda: next(ticks))
    monkeypatch.setattr(core, "handle_message", _replies("ok"))

    cid = new_convo()
    _, before = call("get", f"/api/conversations/{cid}", token=TOKEN_A)
    call("post", f"/api/conversations/{cid}/message", token=TOKEN_A,
         json={"text": "hello"})
    _, after = call("get", f"/api/conversations/{cid}", token=TOKEN_A)

    assert after["updated_at"] > before["updated_at"]
    assert after["created_at"] == before["created_at"]


def test_first_message_auto_titles_a_new_chat(monkeypatch):
    monkeypatch.setattr(core, "handle_message", _replies("ok"))
    cid = new_convo()
    call("post", f"/api/conversations/{cid}/message", token=TOKEN_A,
         json={"text": "  what's the weather   in Chicago "})

    _, body = call("get", f"/api/conversations/{cid}", token=TOKEN_A)
    assert body["title"] == "what's the weather in Chicago"


def test_second_message_does_not_retitle(monkeypatch):
    monkeypatch.setattr(core, "handle_message", _replies("ok"))
    cid = new_convo()
    call("post", f"/api/conversations/{cid}/message", token=TOKEN_A,
         json={"text": "first thing"})
    call("post", f"/api/conversations/{cid}/message", token=TOKEN_A,
         json={"text": "second thing"})

    _, body = call("get", f"/api/conversations/{cid}", token=TOKEN_A)
    assert body["title"] == "first thing"


def test_renamed_conversation_is_not_auto_titled(monkeypatch):
    monkeypatch.setattr(core, "handle_message", _replies("ok"))
    cid = new_convo()
    call("patch", f"/api/conversations/{cid}", token=TOKEN_A, json={"title": "Plumber"})
    call("post", f"/api/conversations/{cid}/message", token=TOKEN_A,
         json={"text": "the sink is leaking"})

    _, body = call("get", f"/api/conversations/{cid}", token=TOKEN_A)
    assert body["title"] == "Plumber"


def test_missing_text_is_400(monkeypatch):
    called = []
    monkeypatch.setattr(core, "handle_message", lambda *a: called.append(a))
    cid = new_convo()
    status, body = call("post", f"/api/conversations/{cid}/message", token=TOKEN_A,
                        json={})
    assert status == 400
    assert "text" in body["error"]
    assert called == []
    assert conversations.messages(cid) == []


def test_blank_text_is_400():
    cid = new_convo()
    status, _ = call("post", f"/api/conversations/{cid}/message", token=TOKEN_A,
                     json={"text": "   "})
    assert status == 400
    assert conversations.messages(cid) == []


def test_non_string_text_is_400():
    cid = new_convo()
    status, _ = call("post", f"/api/conversations/{cid}/message", token=TOKEN_A,
                     json={"text": 42})
    assert status == 400


def test_non_json_body_is_400():
    cid = new_convo()
    status, body = call("post", f"/api/conversations/{cid}/message", token=TOKEN_A,
                        data="not json")
    assert status == 400
    assert "JSON" in body["error"]
    assert conversations.messages(cid) == []


# ── history correctness ────────────────────────────────────────────────────

def test_history_excludes_the_in_flight_message(monkeypatch):
    """The subtle one: core passes the current text separately, so if history
    included it the model would see the same message twice."""
    seen = []

    async def fake(user_id, text, channel):
        seen.append({"text": text, "history": await channel.history()})
        await channel.send(f"echo:{text}")

    monkeypatch.setattr(core, "handle_message", fake)
    cid = new_convo()
    call("post", f"/api/conversations/{cid}/message", token=TOKEN_A,
         json={"text": "first"})
    call("post", f"/api/conversations/{cid}/message", token=TOKEN_A,
         json={"text": "second"})

    assert [s["text"] for s in seen] == ["first", "second"]
    # nothing preceded the very first message
    assert seen[0]["history"] is None

    history = seen[1]["history"]
    assert history == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "echo:first"},
    ]
    assert "second" not in [m["content"] for m in history]


def test_history_is_scoped_to_one_conversation(monkeypatch):
    seen = []

    async def fake(user_id, text, channel):
        seen.append(await channel.history())
        await channel.send(f"echo:{text}")

    monkeypatch.setattr(core, "handle_message", fake)
    one, two = new_convo(), new_convo()
    call("post", f"/api/conversations/{one}/message", token=TOKEN_A,
         json={"text": "in one"})
    call("post", f"/api/conversations/{two}/message", token=TOKEN_A,
         json={"text": "in two"})
    call("post", f"/api/conversations/{two}/message", token=TOKEN_A,
         json={"text": "also two"})

    assert seen[0] is None
    assert seen[1] is None                      # separate conversation, no bleed
    assert [m["content"] for m in seen[2]] == ["in two", "echo:in two"]


def test_history_is_capped_and_keeps_the_newest(monkeypatch):
    cid = conversations.create(USER_A)
    for n in range(30):
        conversations.add_message(cid, "user", f"m{n}")
    channel = webchat.WebChannel(cid, USER_A)
    history = asyncio.run(channel.history())

    assert len(history) == webchat.HISTORY_LIMIT
    # newest N, oldest-first — not the oldest N
    assert [m["content"] for m in history] == [f"m{n}" for n in range(10, 30)]


# ── hardening found by the adversarial audit (2026-08-08) ───────────────────

def test_non_whitelisted_user_is_refused_and_writes_nothing(monkeypatch):
    """authn is not authz. A valid token whose user was de-whitelisted (owner
    ran "remove hubby") must not be able to create conversations or persist
    message content. core's gate returns silently, so without a check in the
    surface this was a cheerful 200 that had already written rows."""
    monkeypatch.setattr(config, "id_to_name", lambda: {})
    status, body = call("post", "/api/conversations", token=TOKEN_A)
    assert status == 403
    assert body["error"] == "user not whitelisted"
    for method, path in [("get", "/api/conversations"),
                         ("get", "/api/conversations/1"),
                         ("delete", "/api/conversations/1")]:
        assert call(method, path, token=TOKEN_A)[0] == 403


def test_oversized_conversation_id_is_404_not_500():
    # int() parses unbounded Python ints, but sqlite binds int64 — this used to
    # blow up at bind time as a 500
    status, _ = call("get", "/api/conversations/9223372036854775808", token=TOKEN_A)
    assert status == 404


@pytest.mark.parametrize("body", ['"just a string"', "123", "[1,2]", "true"])
def test_non_object_json_body_is_400_not_500(body, monkeypatch):
    monkeypatch.setattr(core, "handle_message", _replies("ok"))
    cid = new_convo()
    for method, path in [("post", f"/api/conversations/{cid}/message"),
                         ("patch", f"/api/conversations/{cid}")]:
        status, _ = call(method, path, token=TOKEN_A, data=body,
                         headers={"Content-Type": "application/json"})
        assert status == 400, f"{method} {path} with body {body}"


def test_non_string_title_is_400_not_500(monkeypatch):
    monkeypatch.setattr(core, "handle_message", _replies("ok"))
    cid = new_convo()
    status, _ = call("patch", f"/api/conversations/{cid}", token=TOKEN_A,
                     json={"title": {"nested": "object"}})
    assert status == 400


def test_conversation_renamed_before_first_message_keeps_its_name(monkeypatch):
    monkeypatch.setattr(core, "handle_message", _replies("ok"))
    cid = new_convo()
    call("patch", f"/api/conversations/{cid}", token=TOKEN_A, json={"title": "Plumber"})
    call("post", f"/api/conversations/{cid}/message", token=TOKEN_A,
         json={"text": "the sink is leaking"})
    assert call("get", f"/api/conversations/{cid}", token=TOKEN_A)[1]["title"] == "Plumber"


def test_established_conversation_renamed_to_new_chat_is_not_retitled(monkeypatch):
    monkeypatch.setattr(core, "handle_message", _replies("ok"))
    cid = new_convo()
    call("post", f"/api/conversations/{cid}/message", token=TOKEN_A, json={"text": "first"})
    call("patch", f"/api/conversations/{cid}", token=TOKEN_A, json={"title": "New chat"})
    call("post", f"/api/conversations/{cid}/message", token=TOKEN_A, json={"text": "second"})
    assert call("get", f"/api/conversations/{cid}", token=TOKEN_A)[1]["title"] == "New chat"
