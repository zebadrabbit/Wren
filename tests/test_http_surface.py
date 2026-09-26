import os, asyncio, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

import base64
from aiohttp.test_utils import TestClient, TestServer

from wren import config
from wren import core
from wren.communication import http_plugin as http_surface

TOKENS = {"good-token": 1}


@pytest.fixture(autouse=True)
def tokens(monkeypatch):
    monkeypatch.setattr(config, "WREN_TOKENS", dict(TOKENS))
    monkeypatch.setattr(config, "id_to_name", lambda: {1: "owner"})


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


def _replies(*texts, files=()):
    async def fake(user_id, text, channel, *, source="text"):
        for t in texts:
            await channel.send(t)
        for data, name in files:
            await channel.send_file(data, name)
    return fake


def test_health_needs_no_auth():
    status, body = call("get", "/health")
    assert status == 200
    assert body == {"ok": True}


def test_message_without_token_is_401(monkeypatch):
    called = []
    monkeypatch.setattr(core, "handle_message", lambda *a: called.append(a))
    status, body = call("post", "/message", json={"text": "hi"})
    assert status == 401
    assert body["error"] == "unauthorized"
    assert called == []          # never reached core


def test_message_with_wrong_token_is_401():
    status, body = call("post", "/message", token="bad-token", json={"text": "hi"})
    assert status == 401


def test_malformed_bearer_header_is_401():
    status, _ = call("post", "/message", headers={"Authorization": "good-token"},
                     json={"text": "hi"})
    assert status == 401


def test_message_dispatches_and_returns_replies(monkeypatch):
    seen = {}

    async def fake(user_id, text, channel, *, source="text"):
        seen["user_id"], seen["text"] = user_id, text
        await channel.send("Saved.")

    monkeypatch.setattr(core, "handle_message", fake)
    status, body = call("post", "/message", token="good-token",
                        json={"text": "note this"})
    assert status == 200
    assert body["replies"] == ["Saved."]
    assert seen == {"user_id": 1, "text": "note this"}


def test_message_returns_files_base64(monkeypatch):
    monkeypatch.setattr(core, "handle_message",
                        _replies("here", files=[(b"# notes", "notes.md")]))
    status, body = call("post", "/message", token="good-token", json={"text": "export"})
    assert status == 200
    assert body["files"] == [
        {"filename": "notes.md", "data": base64.b64encode(b"# notes").decode()}
    ]


def test_missing_text_is_400():
    status, body = call("post", "/message", token="good-token", json={})
    assert status == 400
    assert "text" in body["error"]


def test_blank_text_is_400():
    status, _ = call("post", "/message", token="good-token", json={"text": "   "})
    assert status == 400


def test_non_json_body_is_400():
    status, body = call("post", "/message", token="good-token", data="not json")
    assert status == 400


def test_valid_token_for_non_whitelisted_user_is_403(monkeypatch):
    # token authenticates, but core's authorization gate drops the message
    monkeypatch.setattr(config, "id_to_name", lambda: {})
    monkeypatch.setattr(core, "handle_message", _replies())
    status, body = call("post", "/message", token="good-token", json={"text": "hi"})
    assert status == 403
    assert body["error"] == "user not whitelisted"


def test_voice_without_token_is_401():
    status, _ = call("post", "/voice", data=b"RIFFfake")
    assert status == 401


def test_voice_empty_body_is_400():
    status, body = call("post", "/voice", token="good-token", data=b"")
    assert status == 400


def test_voice_returns_503_when_stt_unavailable(monkeypatch):
    from wren import stt
    def unavailable(_audio):
        raise stt.STTUnavailable("not installed")
    monkeypatch.setattr(stt, "transcribe", unavailable)
    status, body = call("post", "/voice", token="good-token", data=b"RIFFfake")
    assert status == 503
    assert body["error"] == "not installed"


def test_voice_transcribes_then_dispatches(monkeypatch):
    from wren import stt
    seen = {}

    async def fake(user_id, text, channel, *, source="text"):
        seen["text"] = text
        await channel.send("Reminder set.")

    monkeypatch.setattr(stt, "transcribe", lambda _audio: "remind me at six")
    monkeypatch.setattr(core, "handle_message", fake)
    status, body = call("post", "/voice", token="good-token", data=b"RIFFfake")
    assert status == 200
    assert body["transcript"] == "remind me at six"
    assert body["replies"] == ["Reminder set."]
    assert seen["text"] == "remind me at six"


def test_voice_dispatches_with_source_voice(monkeypatch):
    from wren import stt
    seen = {}

    async def fake(user_id, text, channel, *, source="text"):
        seen["source"] = source
        await channel.send("ok")

    monkeypatch.setattr(stt, "transcribe", lambda _audio: "remove milk")
    monkeypatch.setattr(core, "handle_message", fake)
    status, body = call("post", "/voice", token="good-token", data=b"RIFFfake")
    assert status == 200
    assert seen["source"] == "voice"


def test_voice_destructive_intent_asks_for_confirmation_end_to_end(monkeypatch):
    # M5: every other /voice test patches core.handle_message itself, so none
    # of them actually exercise the destructive-intent confirmation path this
    # surface is supposed to trigger. This one leaves handle_message real and
    # walks the whole chain: stt -> detect_intent -> core -> registry -> the
    # exact confirmation wording a user (or kitchen device) would hear.
    from wren import stt, brain, settings
    from wren.skills import shopping_store, memory_store

    settings.init_db()
    shopping_store.init_db()
    memory_store.init_db()          # harmless here -- the chat branch is never reached
    shopping_store.add("milk", "owner")

    monkeypatch.setattr(stt, "transcribe", lambda _audio: "remove milk from the list")
    monkeypatch.setattr(
        brain, "detect_intent",
        lambda user_id, text, history=None: {"intent": "remove_shopping_item", "content": "milk", "tags": []},
    )
    try:
        status, body = call("post", "/voice", token="good-token", data=b"RIFFfake")
        assert status == 200
        assert body["replies"] == ['Confirm: remove "milk" from the shopping list? Say yes or no.']
        assert [i["item"] for i in shopping_store.active_items()] == ["milk"]
    finally:
        # a real handle_message arms core._pending -- clear it so it cannot
        # bleed into a later test that shares the module-level dict
        core._pending.clear()


def test_message_dispatches_with_source_text(monkeypatch):
    seen = {}

    async def fake(user_id, text, channel, *, source="text"):
        seen["source"] = source
        await channel.send("ok")

    monkeypatch.setattr(core, "handle_message", fake)
    status, body = call("post", "/message", token="good-token", json={"text": "remove milk"})
    assert status == 200
    assert seen["source"] == "text"


def test_voice_silent_audio_returns_empty(monkeypatch):
    from wren import stt
    monkeypatch.setattr(stt, "transcribe", lambda _audio: "   ")
    status, body = call("post", "/voice", token="good-token", data=b"RIFFfake")
    assert status == 200
    assert body["transcript"] == ""
    assert body["replies"] == []


def test_voice_transcription_error_is_500(monkeypatch):
    from wren import stt
    def boom(_audio):
        raise RuntimeError("cuda exploded")
    monkeypatch.setattr(stt, "transcribe", boom)
    status, body = call("post", "/voice", token="good-token", data=b"RIFFfake")
    assert status == 500
    assert body["error"] == "transcription failed"


def test_http_notify_is_send_only():
    # NOTIFY_SURFACE=http must not silently pretend to deliver
    assert asyncio.run(http_surface.notify(1, "reminder")) is False


def test_start_refuses_to_run_without_tokens(monkeypatch):
    monkeypatch.setattr(config, "WREN_TOKENS", {})
    with pytest.raises(RuntimeError, match="WREN_TOKENS"):
        asyncio.run(http_surface.start())


def test_start_keeps_serving_and_does_not_return(monkeypatch):
    # Regression: start() originally returned as soon as the socket was bound,
    # so run.py's gather() completed and the whole process exited a moment
    # after boot. A surface's start() must run for the surface's lifetime.
    from wren import router
    monkeypatch.setattr(config, "WREN_HTTP_PORT", 8899)
    monkeypatch.setattr(config, "WREN_HTTP_HOST", "127.0.0.1")
    router.reset()

    async def run():
        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            await asyncio.wait_for(http_surface.start(), timeout=0.75)

    try:
        asyncio.run(run())
    finally:
        router.reset()


# ── dry_run ───────────────────────────────────────────────────────────────

def test_dry_run_query_flag_runs_core_inside_db_dry_run(monkeypatch):
    from wren import db
    seen = []

    async def fake(user_id, text, channel, *, source="text"):
        seen.append(db.in_dry_run())
        await channel.send("ok")
    monkeypatch.setattr(core, "handle_message", fake)
    status, body = call("post", "/message?dry_run=1", token="good-token", json={"text": "hi"})
    assert status == 200
    assert seen == [True]
    assert body["dry_run"] is True and body["replies"] == ["ok"]


def test_without_the_flag_nothing_is_dry(monkeypatch):
    from wren import db
    seen = []

    async def fake(user_id, text, channel, *, source="text"):
        seen.append(db.in_dry_run())
        await channel.send("ok")
    monkeypatch.setattr(core, "handle_message", fake)
    status, body = call("post", "/message", token="good-token", json={"text": "hi"})
    assert seen == [False]
    assert "dry_run" not in body


def test_dry_run_flag_accepts_true_and_rejects_zero(monkeypatch):
    from wren import db
    seen = []

    async def fake(user_id, text, channel, *, source="text"):
        seen.append(db.in_dry_run())
        await channel.send("ok")
    monkeypatch.setattr(core, "handle_message", fake)
    call("post", "/message?dry_run=true", token="good-token", json={"text": "hi"})
    call("post", "/message?dry_run=0", token="good-token", json={"text": "hi"})
    assert seen == [True, False]


def test_dry_run_message_leaves_no_rows_end_to_end(monkeypatch):
    # The real core and a real store: the only fake is the classifier.
    from wren import brain, contacts, settings
    from wren.skills import shopping_store
    contacts.init_db(); settings.init_db(); shopping_store.init_db()
    monkeypatch.setattr(brain, "detect_intent", lambda *a, **k: {
        "intent": "add_shopping_item", "content": "potatoes", "tags": [], "person": None})
    status, body = call("post", "/message?dry_run=1", token="good-token", json={"text": "add potatoes"})
    assert status == 200 and body["dry_run"] is True
    assert body["replies"] and "potatoes" in body["replies"][0].lower()
    assert shopping_store.active_items() == []


def test_voice_honours_dry_run(monkeypatch):
    from wren import db, stt
    seen = []
    monkeypatch.setattr(stt, "transcribe", lambda audio: "hello")

    async def fake(user_id, text, channel, *, source="text"):
        seen.append(db.in_dry_run())
        await channel.send("ok")
    monkeypatch.setattr(core, "handle_message", fake)
    status, body = call("post", "/voice?dry_run=1", token="good-token", data=b"RIFF")
    assert seen == [True] and body["dry_run"] is True
