import os, asyncio, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

import base64
from unittest.mock import AsyncMock, MagicMock, patch
from aiohttp.test_utils import TestClient, TestServer

from wren import config
from wren import conversations
from wren import core
from wren import registry
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
    outbound = files

    async def fake(user_id, text, channel, *, files=None):
        for t in texts:
            await channel.send(t)
        for data, name in outbound:
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

    async def fake(user_id, text, channel, *, files=None):
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

    async def fake(user_id, text, channel, *, files=None):
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
        {"filename": "notes.md", "mime": "application/octet-stream",
         "data": base64.b64encode(b"# notes").decode()}
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

    async def fake(user_id, text, channel, *, files=None):
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

    async def fake(user_id, text, channel, *, files=None):
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


# ── cards ───────────────────────────────────────────────────────────────────

def test_a_card_comes_back_in_the_message_response():
    async def fake_handle(user_id, text, channel, *, files=None):
        await channel.send_card(
            "shopping", {"items": [{"text": "milk", "added_by": "ann"}]},
            "milk", intent="recall_shopping", params={"content": ""})

    _, convo = call("post", "/api/conversations", token=TOKEN_A)
    with patch.object(core, "handle_message", new=fake_handle):
        status, body = call("post", f"/api/conversations/{convo['id']}/message",
                            token=TOKEN_A, json={"text": "what's on shopping"})
    assert status == 200
    assert body["replies"] == ["milk"]
    assert body["cards"] == [{
        "kind": "shopping",
        "data": {"items": [{"text": "milk", "added_by": "ann"}]},
        "intent": "recall_shopping",
        "params": {"content": ""},
        "text": "milk",
    }]


def test_a_card_survives_a_reload_but_its_rows_do_not():
    # what is persisted is how to re-fetch, never the rows -- that is what makes
    # a card live when you scroll back to it an hour later
    async def fake_handle(user_id, text, channel, *, files=None):
        await channel.send_card("shopping", {"items": [{"text": "milk"}]}, "milk",
                                intent="recall_shopping", params={"content": ""})

    _, convo = call("post", "/api/conversations", token=TOKEN_A)
    with patch.object(core, "handle_message", new=fake_handle):
        call("post", f"/api/conversations/{convo['id']}/message",
             token=TOKEN_A, json={"text": "what's on shopping"})

    _, reloaded = call("get", f"/api/conversations/{convo['id']}", token=TOKEN_A)
    assistant = [m for m in reloaded["messages"] if m["role"] == "assistant"][0]
    assert assistant["content"] == "milk"
    assert assistant["card"] == {"kind": "shopping", "intent": "recall_shopping",
                                 "params": {"content": ""}}
    assert "data" not in assistant["card"]        # rows are never stored


def test_the_model_does_not_see_cards():
    # history() feeds brain.detect_intent; a card must be invisible there
    async def fake_handle(user_id, text, channel, *, files=None):
        await channel.send_card("shopping", {"items": [{"text": "milk"}]}, "milk",
                                intent="recall_shopping", params={"content": ""})

    _, convo = call("post", "/api/conversations", token=TOKEN_A)
    with patch.object(core, "handle_message", new=fake_handle):
        call("post", f"/api/conversations/{convo['id']}/message",
             token=TOKEN_A, json={"text": "what's on shopping"})

    channel = webchat.WebChannel(convo["id"], USER_A)
    # asyncio.run, not get_event_loop().run_until_complete — the latter raises
    # "no current event loop" on 3.12 outside a running loop
    history = asyncio.run(channel.history())
    assert all(set(m) == {"role", "content"} for m in history)


# ── dispatch ────────────────────────────────────────────────────────────────

def test_dispatch_runs_an_intent_without_the_llm():
    from wren import brain
    calls = []

    async def fake_handle(intent, ctx):
        calls.append((intent, ctx.content))
        await ctx.channel.send_card("shopping", {"items": []}, "Shopping list is empty.")

    plugin = MagicMock()
    # __name__ is required here (not just .handle): dispatch's is_enabled()
    # check keys off __name__ via registry.skill_key(), same as any real
    # plugin module -- a bare MagicMock has no __name__ of its own.
    plugin.__name__ = "wren.fake_shopping_skill"
    plugin.handle = fake_handle
    with patch.dict(registry.INTENT_HANDLERS, {"recall_shopping": plugin}), \
         patch.object(brain, "detect_intent", side_effect=AssertionError("no LLM")):
        status, body = call("post", "/api/dispatch", token=TOKEN_A,
                            json={"intent": "recall_shopping"})

    assert status == 200
    assert calls == [("recall_shopping", "")]
    assert body["cards"][0]["kind"] == "shopping"
    assert body["replies"] == ["Shopping list is empty."]


def test_dispatch_requires_a_token():
    status, _ = call("post", "/api/dispatch", json={"intent": "recall_shopping"})
    assert status == 401


def test_dispatch_rejects_a_missing_intent():
    status, _ = call("post", "/api/dispatch", token=TOKEN_A, json={})
    assert status == 400


def test_dispatch_404s_an_unknown_intent():
    status, _ = call("post", "/api/dispatch", token=TOKEN_A,
                     json={"intent": "no_such_intent"})
    assert status == 404


def test_dispatch_404s_an_intent_whose_skill_is_disabled():
    # a skill switched off in the plugins panel must not be reachable through
    # this door either -- the same check core.handle_message makes
    plugin = MagicMock()
    plugin.handle = AsyncMock()
    with patch.dict(registry.INTENT_HANDLERS, {"recall_shopping": plugin}), \
         patch.object(registry, "is_enabled", return_value=False):
        status, _ = call("post", "/api/dispatch", token=TOKEN_A,
                         json={"intent": "recall_shopping"})
    assert status == 404
    plugin.handle.assert_not_awaited()


def test_dispatch_writes_nothing_to_any_conversation():
    # clicking a button must not manufacture a fake user message
    async def fake_handle(intent, ctx):
        await ctx.channel.send("Removed milk.")

    _, convo = call("post", "/api/conversations", token=TOKEN_A)
    plugin = MagicMock()
    # same __name__ requirement as above -- is_enabled() is on the path even
    # though this test is not exercising it directly.
    plugin.__name__ = "wren.fake_shopping_skill"
    plugin.handle = fake_handle
    with patch.dict(registry.INTENT_HANDLERS, {"remove_shopping_item": plugin}):
        call("post", "/api/dispatch", token=TOKEN_A,
             json={"intent": "remove_shopping_item", "content": "milk"})

    _, reloaded = call("get", f"/api/conversations/{convo['id']}", token=TOKEN_A)
    assert reloaded["messages"] == []


# ── dispatch fix round 1 ────────────────────────────────────────────────────

def test_dispatch_rejects_malformed_tags():
    # a non-str element, not just a non-list container -- notes_store does
    # ",".join(tags), which TypeErrors on either shape below if this were
    # allowed through
    status, _ = call("post", "/api/dispatch", token=TOKEN_A,
                     json={"intent": "recall_shopping", "tags": [1, 2]})
    assert status == 400

    status, _ = call("post", "/api/dispatch", token=TOKEN_A,
                     json={"intent": "recall_shopping", "tags": [["a"]]})
    assert status == 400


def test_dispatch_rejects_bad_person_or_when_types():
    status, _ = call("post", "/api/dispatch", token=TOKEN_A,
                     json={"intent": "recall_shopping", "person": 1})
    assert status == 400

    status, _ = call("post", "/api/dispatch", token=TOKEN_A,
                     json={"intent": "recall_shopping", "when": 1})
    assert status == 400


def test_dispatch_reports_a_skill_error_as_json_without_leaking_the_exception_text():
    # a raised exception is not the skill's own error text (that arrives via
    # channel.send() into `replies` with a 200) -- it's an unanticipated
    # crash, and library exception strings are not safe to echo verbatim
    # (brain._complete re-raises the provider client's exception, which can
    # carry a base_url or a hosted provider's response body). The message
    # below stands in for that: something that must never reach the wire.
    async def fake_handle(intent, ctx):
        raise RuntimeError("token=sk-fake-abc123 at http://internal.example/v1")

    plugin = MagicMock()
    plugin.__name__ = "wren.fake_shopping_skill"
    plugin.handle = fake_handle
    with patch.dict(registry.INTENT_HANDLERS, {"recall_shopping": plugin}):
        status, body = call("post", "/api/dispatch", token=TOKEN_A,
                            json={"intent": "recall_shopping"})
    assert status == 500
    assert "sk-fake-abc123" not in body["error"]
    assert "internal.example" not in body["error"]
    assert body["error"] == "Something went wrong, try again."


def test_dispatch_passes_person_and_when_into_ctx():
    # set_reminder, send_shopping_list and add_contact all read ctx.person /
    # ctx.when -- dropping these from Ctx makes them undriveable through here
    calls = []

    async def fake_handle(intent, ctx):
        calls.append((ctx.person, ctx.when))

    plugin = MagicMock()
    plugin.__name__ = "wren.fake_shopping_skill"
    plugin.handle = fake_handle
    with patch.dict(registry.INTENT_HANDLERS, {"recall_shopping": plugin}):
        status, _ = call("post", "/api/dispatch", token=TOKEN_A,
                         json={"intent": "recall_shopping", "person": "ann",
                              "when": "tomorrow"})
    assert status == 200
    assert calls == [("ann", "tomorrow")]


def test_dispatch_card_carries_intent_and_params_for_a_refresh():
    # a card dispatched via a button must be exactly as refreshable as one
    # that arrived from chat -- trimming intent/params here would make a
    # button-driven card unable to re-read itself
    async def fake_handle(intent, ctx):
        await ctx.channel.send_card("shopping", {"items": []}, "Shopping list is empty.",
                                    intent="recall_shopping", params={"content": ""})

    plugin = MagicMock()
    plugin.__name__ = "wren.fake_shopping_skill"
    plugin.handle = fake_handle
    with patch.dict(registry.INTENT_HANDLERS, {"recall_shopping": plugin}):
        status, body = call("post", "/api/dispatch", token=TOKEN_A,
                            json={"intent": "recall_shopping"})
    assert status == 200
    # /api/dispatch reuses CollectingChannel, same as /message's WebChannel
    # path -- so the two endpoints' cards are the same shape, "text" included.
    assert body["cards"] == [{
        "kind": "shopping",
        "data": {"items": []},
        "intent": "recall_shopping",
        "params": {"content": ""},
        "text": "Shopping list is empty.",
    }]


def test_dispatch_returns_files_from_a_send_file_intent():
    # export_notes calls send_file; without a "files" key that intent
    # produces no output at all through this door
    async def fake_handle(intent, ctx):
        await ctx.channel.send_file(b"hello", "notes.md")

    plugin = MagicMock()
    plugin.__name__ = "wren.fake_shopping_skill"
    plugin.handle = fake_handle
    with patch.dict(registry.INTENT_HANDLERS, {"export_notes": plugin}):
        status, body = call("post", "/api/dispatch", token=TOKEN_A,
                            json={"intent": "export_notes"})
    assert status == 200
    assert body["files"] == [{"filename": "notes.md",
                              "data": base64.b64encode(b"hello").decode("ascii")}]


def test_log_is_the_containing_block_for_the_speaker_labels():
    # .sr speaker labels are position:absolute; with #log static they anchor
    # to #app and pile up below the scrolling log, so the page itself grows a
    # scrollbar and the composer floats mid-window (2026-09-26).
    from pathlib import Path
    page = (Path(__file__).parent.parent / "wren" / "communication" / "chat.html").read_text()
    rule = page[page.index("#log {"):]
    rule = rule[:rule.index("}")]
    assert "position: relative" in rule


def test_reopen_does_not_remount_cards_without_a_refresh_intent():
    # The locate card has intent "" (one-shot). Re-mounting it on reopen would
    # re-prompt for geolocation, or 400 on /api/dispatch, every time.
    from pathlib import Path
    page = (Path(__file__).parent.parent / "wren" / "communication" / "chat.html").read_text()
    assert "if (m.card && m.card.intent) mountStoredCard(" in page


# ── multipart ─────────────────────────────────────────────────────────────

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 32


def _multipart(text, *files):
    import aiohttp
    form = aiohttp.FormData()
    if text is not None:
        form.add_field("text", text)
    for name, data in files:
        form.add_field("file", data, filename=name, content_type="image/jpeg")
    return form


def _convo(token=TOKEN_A):
    _, body = call("post", "/api/conversations", token=token, json={})
    return body["id"]


def test_post_message_accepts_multipart_and_stores_the_caption(monkeypatch):
    seen = {}

    async def fake(user_id, text, channel, *, files=None):
        seen["text"], seen["files"] = text, files
        await channel.send("Saved, 1 image.")
    monkeypatch.setattr(core, "handle_message", fake)
    cid = _convo()
    status, body = call("post", f"/api/conversations/{cid}/message", token=TOKEN_A,
                        data=_multipart("receipt", ("r.jpg", JPEG)))
    assert status == 200 and body["replies"] == ["Saved, 1 image."]
    assert seen["text"] == "receipt" and [f.filename for f in seen["files"]] == ["r.jpg"]
    assert conversations.messages(cid)[0]["content"] == "receipt"


def test_post_message_with_a_file_and_no_caption_stores_a_placeholder(monkeypatch):
    async def fake(user_id, text, channel, *, files=None):
        await channel.send("What is this?")
    monkeypatch.setattr(core, "handle_message", fake)
    cid = _convo()
    status, _ = call("post", f"/api/conversations/{cid}/message", token=TOKEN_A,
                     data=_multipart(None, ("r.jpg", JPEG)))
    assert status == 200
    assert conversations.messages(cid)[0]["content"] == "(sent r.jpg)"


def test_post_message_reply_files_carry_mime(monkeypatch):
    monkeypatch.setattr(core, "handle_message", _replies("here", files=[(JPEG, "a.jpg")]))
    cid = _convo()
    _, body = call("post", f"/api/conversations/{cid}/message", token=TOKEN_A, json={"text": "show"})
    assert body["files"][0]["mime"] == "image/jpeg"
