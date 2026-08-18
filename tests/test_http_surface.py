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
    async def fake(user_id, text, channel):
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

    async def fake(user_id, text, channel):
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

    async def fake(user_id, text, channel):
        seen["text"] = text
        await channel.send("Reminder set.")

    monkeypatch.setattr(stt, "transcribe", lambda _audio: "remind me at six")
    monkeypatch.setattr(core, "handle_message", fake)
    status, body = call("post", "/voice", token="good-token", data=b"RIFFfake")
    assert status == 200
    assert body["transcript"] == "remind me at six"
    assert body["replies"] == ["Reminder set."]
    assert seen["text"] == "remind me at six"


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
