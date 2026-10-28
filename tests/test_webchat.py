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
from wren import registry
from wren.communication import http_plugin as http_surface

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
    ("get", "/api/timeline"),
    ("post", "/api/dispatch"),
    ("get", "/api/me"),
    ("get", "/api/plugins"),
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


def test_dispatch_writes_nothing_to_the_timeline():
    # a card button or refresh is not a turn: nothing the person typed, and
    # nothing Wren said, so the timeline stays exactly as it was
    from wren import settings
    from wren.skills import shopping_store
    settings.init_db()
    shopping_store.init_db()
    shopping_store.add("milk", "ann")
    status, body = call("post", "/api/dispatch", token=TOKEN_A, json={"intent": "recall_shopping"})
    assert status == 200 and body["cards"][0]["kind"] == "shopping"
    assert conversations.timeline(USER_A) == []


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
    # /api/dispatch reuses CollectingChannel, same as /message
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
                              "mime": "application/octet-stream",
                              "data": base64.b64encode(b"hello").decode("ascii")}]


def test_dispatch_reply_files_carry_mime():
    # dispatch's response used to build {"filename", "data"} with no "mime",
    # unlike /message -- the page decides
    # inline image vs download link from mime, so a card-driven reply that
    # happens to carry a file must get the same treatment.
    async def fake_handle(intent, ctx):
        await ctx.channel.send_file(JPEG, "receipt.jpg")

    plugin = MagicMock()
    plugin.__name__ = "wren.fake_notes_skill"
    plugin.handle = fake_handle
    with patch.dict(registry.INTENT_HANDLERS, {"export_notes": plugin}):
        status, body = call("post", "/api/dispatch", token=TOKEN_A,
                            json={"intent": "export_notes"})
    assert status == 200
    assert body["files"][0]["mime"] == "image/jpeg"


def test_log_is_the_containing_block_for_the_speaker_labels():
    # .sr speaker labels are position:absolute; with #log static they anchor
    # to #app and pile up below the scrolling log, so the page itself grows a
    # scrollbar and the composer floats mid-window (2026-09-26).
    from pathlib import Path
    page = (Path(__file__).parent.parent / "wren" / "communication" / "chat.html").read_text()
    rule = page[page.index("#log {"):]
    rule = rule[:rule.index("}")]
    assert "position: relative" in rule


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


def _page():
    from pathlib import Path
    return (Path(__file__).parent.parent / "wren" / "communication" / "chat.html").read_text()


def test_page_has_the_attachment_plumbing():
    page = _page()
    assert 'id="attach"' in page
    assert "pendingFiles" in page
    assert "new FormData()" in page
    assert 'addEventListener("paste"' in page or ".onpaste" in page
    assert '.ondrop' in page or 'addEventListener("drop"' in page


def test_a_drop_outside_the_composer_does_not_navigate_the_tab():
    page = _page()
    assert 'document.addEventListener("drop"' in page


def test_api_helper_does_not_force_json_content_type_on_form_bodies():
    page = _page()
    assert "instanceof FormData" in page


def test_reply_images_render_inline_and_pdfs_as_links():
    page = _page()
    assert 'f.mime.startsWith("image/")' in page
    assert '<img class="rimg"' in page


def test_notes_card_rows_show_a_thumbnail_for_attached_images():
    page = _page()
    assert "renderCard(c, b.querySelector(\".body\"), data.files)" in page
    assert 'class="cthumb"' in page


def test_user_bubble_thumbnails_revoke_their_object_urls():
    page = _page()
    # both the chip and the sent bubble revoke after load; count both
    assert page.count("URL.revokeObjectURL(img.src)") >= 2


def test_every_render_card_call_passes_the_reply_files():
    import re
    page = _page()
    calls = [m for m in re.findall(r"renderCard\(([^;]*?)\);", page)]
    assert calls, "no renderCard calls found"
    for args in calls:
        assert args.count(",") == 2, f"renderCard call without files: {args}"


def test_dispatch_passes_the_list_name_into_ctx():
    # the shopping card of a named list re-reads itself with {"list": "packing"};
    # without this the card falls back to the shopping list on every reload
    calls = []

    async def fake_handle(intent, ctx):
        calls.append(ctx.list_name)

    plugin = MagicMock()
    plugin.__name__ = "wren.fake_shopping_skill"
    plugin.handle = fake_handle
    with patch.dict(registry.INTENT_HANDLERS, {"recall_shopping": plugin}):
        status, _ = call("post", "/api/dispatch", token=TOKEN_A,
                         json={"intent": "recall_shopping", "list": "packing"})
    assert status == 200
    assert calls == ["packing"]


def test_dispatch_rejects_a_non_string_list_name():
    status, _ = call("post", "/api/dispatch", token=TOKEN_A,
                     json={"intent": "recall_shopping", "list": ["packing"]})
    assert status == 400


# ── the timeline ───────────────────────────────────────────────────────────

def _seed(user, *rows):
    h = conversations.home(user)
    return [conversations.add_message(h, role, text, channel=ch) for role, text, ch in rows]


def test_timeline_is_per_user_oldest_first_with_the_row_shape():
    _seed(USER_A, ("user", "hi", "web"), ("assistant", "hello", "web"))
    _seed(USER_B, ("user", "bob's", "telegram"))
    status, rows = call("get", "/api/timeline", token=TOKEN_A)
    assert status == 200
    assert [r["content"] for r in rows] == ["hi", "hello"]
    assert set(rows[0]) == {"id", "role", "content", "created_at", "channel", "kind", "card"}
    assert rows[0]["channel"] == "web" and rows[0]["kind"] is None and rows[0]["card"] is None


def test_timeline_after_returns_only_newer_rows():
    ids = _seed(USER_A, ("user", "a", "web"), ("assistant", "b", "web"), ("user", "c", "web"))
    status, rows = call("get", f"/api/timeline?after={ids[0]}", token=TOKEN_A)
    assert [r["content"] for r in rows] == ["b", "c"]


def test_timeline_before_pages_backwards():
    ids = _seed(USER_A, *[("user", f"m{n}", "web") for n in range(5)])
    status, rows = call("get", f"/api/timeline?before={ids[3]}&limit=2", token=TOKEN_A)
    assert [r["content"] for r in rows] == ["m1", "m2"]


def test_timeline_cursor_naming_another_users_row_pages_nothing():
    (bob,) = _seed(USER_B, ("user", "secret", "web"))
    _seed(USER_A, ("user", "mine", "web"))
    for q in (f"before={bob}", f"after={bob}"):
        status, rows = call("get", f"/api/timeline?{q}", token=TOKEN_A)
        assert (status, rows) == (200, [])


@pytest.mark.parametrize("query", ["before=abc", "after=1.5", "limit=x", "before=1&after=2",
                                   "before=-1", "before=99999999999999999999"])
def test_timeline_rejects_bad_parameters(query):
    status, body = call("get", f"/api/timeline?{query}", token=TOKEN_A)
    assert status == 400 and "error" in body


def test_timeline_limit_is_clamped_not_refused():
    _seed(USER_A, *[("user", f"m{n}", "web") for n in range(3)])
    status, rows = call("get", "/api/timeline?limit=999", token=TOKEN_A)
    assert status == 200 and len(rows) == 3
    status, rows = call("get", "/api/timeline?limit=0", token=TOKEN_A)
    assert status == 200 and len(rows) == 1


def test_timeline_requires_a_whitelisted_user(monkeypatch):
    monkeypatch.setattr(config, "id_to_name", lambda: {USER_A: "ann"})
    status, body = call("get", "/api/timeline", token=TOKEN_B)
    assert status == 403


def test_conversation_routes_are_gone():
    for method, path in (("get", "/api/conversations"), ("post", "/api/conversations"),
                         ("get", "/api/conversations/1"), ("post", "/api/conversations/1/message")):
        status, _ = call(method, path, token=TOKEN_A, expect_json=False)
        assert status in (404, 405), (method, path)


# ── the page is the timeline ───────────────────────────────────────────────

def test_page_polls_the_timeline_and_has_no_conversation_list():
    _, _, html = asyncio.run(_page_request())
    assert "/api/timeline" in html
    assert "/api/conversations" not in html
    assert 'id="new"' not in html and 'id="convos"' not in html
    assert "prompt(" not in html and "confirm(" not in html
    for anchor in ('id="nav-today"', 'id="spaces"', 'id="today"', 'id="reads"',
                   'id="attach"', 'id="menu"', 'id="modeltext"'):
        assert anchor in html, anchor


def test_page_posts_to_the_shared_message_route():
    _, _, html = asyncio.run(_page_request())
    assert 'api("/message"' in html


def test_page_font_stack_is_system_first():
    _, _, html = asyncio.run(_page_request())
    assert "font: 15px/1.6 system-ui" in html


def test_sign_in_copy_names_no_env_var_or_command():
    _, _, html = asyncio.run(_page_request())
    login = html[html.index('id="login"'):html.index('id="app"')]
    assert "WREN_TOKENS" not in login and "manage.sh" not in login


def test_page_mounts_the_today_panel_from_the_briefing():
    _, _, html = asyncio.run(_page_request())
    assert "briefing: briefingCard" in html
    assert 'dispatch("briefing"' in html


def test_page_has_a_phone_layout():
    _, _, html = asyncio.run(_page_request())
    assert "@media (max-width: 700px)" in html
    assert "matchMedia(" in html
    assert 'id="scrim"' in html
    assert "@media (max-width: 640px)" not in html


def test_spaces_route_lists_spaces_for_any_whitelisted_user():
    from wren.skills import shopping_store
    shopping_store.init_db()
    status, spaces = call("get", "/api/spaces", token=TOKEN_B)
    assert status == 200
    assert {"key": "list:shopping", "label": "Shopping", "intent": "recall_shopping",
            "params": {"content": "", "list": "shopping"}} in spaces


def test_spaces_route_requires_a_token():
    status, _ = call("get", "/api/spaces")
    assert status == 401


def test_page_lists_spaces_and_can_return_to_today():
    _, _, html = asyncio.run(_page_request())
    assert "/api/spaces" in html
    assert 'id="space"' in html
    assert "function openSpace" in html
    assert "openKey" in html
    assert 'toggle("on", s.key === openKey)' in html


def test_panel_has_a_people_tab_with_the_invite_explanation():
    _, _, html = asyncio.run(_page_request())
    assert 'data-tab="people"' in html and 'id="tab-people"' in html
    assert "/api/contacts" in html
    assert "#token=" in html                      # the invite link is explained here
    assert "manage.sh token add" in html           # and only here (owner's panel)


def test_the_today_panel_is_capped_and_folds_on_a_phone():
    # A full briefing in one column on a phone was taller than the screen:
    # #today is a flex item (min-height:auto), so it squeezed #log to its
    # padding and pushed the composer below the fold (review, 2026-10-14).
    page = _page()
    rule = page[page.index("#today {"):]
    rule = rule[:rule.index("}")]
    assert "flex: none" in rule and "max-height: 40vh" in rule and "overflow-y: auto" in rule
    # first visit at phone width starts folded; a choice the reader made wins
    body = page[page.index("async function loadToday"):]
    body = body[:body.index("\n}\n")]
    assert 'matchMedia("(max-width: 700px)")' in body
