import os, asyncio, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from aiohttp.test_utils import TestClient, TestServer

from wren import config
from wren import registry
from wren import settings
from wren.communication import http_plugin as http_surface
from wren.communication import webchat

TOKEN_OWNER, USER_OWNER = "tok-a", 1          # matches OWNER_ID above
TOKEN_OTHER, USER_OTHER = "tok-b", 2


@pytest.fixture(autouse=True)
def tokens(monkeypatch, isolated_db):
    # isolated_db (conftest) must run first so init_db lands on the tmp file
    monkeypatch.setattr(config, "WREN_TOKENS", {TOKEN_OWNER: USER_OWNER, TOKEN_OTHER: USER_OTHER})
    monkeypatch.setattr(config, "id_to_name", lambda: {USER_OWNER: "ann", USER_OTHER: "bob"})
    settings.init_db()


async def _request(method, path, *, token=None, **kw):
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    async with TestClient(TestServer(http_surface.build_app())) as client:
        resp = await getattr(client, method)(path, headers=headers, **kw)
        return resp.status, await resp.json()


def call(method, path, **kw):
    return asyncio.run(_request(method, path, **kw))


def test_owner_can_read_the_plugin_list():
    status, body = call("get", "/api/plugins", token=TOKEN_OWNER)
    assert status == 200
    assert {"skills", "channels", "settings"} <= set(body)
    assert any(s["module"] == "notes_skill" for s in body["skills"])


def test_a_non_owner_is_refused():
    status, _ = call("get", "/api/plugins", token=TOKEN_OTHER)
    assert status == 403


def test_no_token_is_unauthorized():
    status, _ = call("get", "/api/plugins")
    assert status == 401


def test_settings_payload_contains_no_credential():
    # Pairs with the SETTABLE allowlist test: this endpoint returns values, so
    # the allowlist is the only thing keeping secrets out of the response.
    _, body = call("get", "/api/plugins", token=TOKEN_OWNER)
    assert "DISCORD_TOKEN" not in body["settings"]


def test_channels_include_one_that_is_not_enabled():
    # The case the panel exists to show: Telegram is present in the package but
    # absent from COMMUNICATION_PLUGINS.
    rows = webchat._channel_rows()
    telegram = next(r for r in rows if r["module"] == "telegram_plugin")
    assert telegram["running"] is False
    assert "TELEGRAM_TOKEN" in telegram["reason"]


def test_owner_can_disable_a_skill():
    try:
        status, _ = call("patch", "/api/plugins/notes_skill",
                         token=TOKEN_OWNER, json={"enabled": False})
        assert status == 200
        assert "save_note" not in registry.all_intents()
    finally:
        settings.unset("skill.notes_skill.enabled")
        from wren.skills import notes_skill
        registry.set_enabled(notes_skill, True)


def test_a_non_owner_cannot_disable_a_skill():
    status, _ = call("patch", "/api/plugins/notes_skill",
                     token=TOKEN_OTHER, json={"enabled": False})
    assert status == 403
    assert settings.get("skill.notes_skill.enabled") is None


def test_toggling_a_channel_is_refused():
    # Channels need a restart. A toggle that silently does nothing is worse
    # than no toggle.
    status, _ = call("patch", "/api/plugins/telegram_plugin",
                     token=TOKEN_OWNER, json={"enabled": True})
    assert status == 400


def test_an_unknown_module_is_a_404():
    status, _ = call("patch", "/api/plugins/nope_skill",
                     token=TOKEN_OWNER, json={"enabled": False})
    assert status == 404


def test_owner_can_change_a_setting():
    try:
        status, _ = call("patch", "/api/settings",
                         token=TOKEN_OWNER, json={"SEARXNG_URL": "http://searx.lan"})
        assert status == 200
        assert config.SEARXNG_URL == "http://searx.lan"
    finally:
        config.clear_override("SEARXNG_URL")


def test_an_invalid_setting_is_400_and_persists_nothing():
    original = config.REMINDER_POLL_SECONDS
    status, _ = call("patch", "/api/settings",
                     token=TOKEN_OWNER, json={"REMINDER_POLL_SECONDS": "0"})
    assert status == 400
    assert config.REMINDER_POLL_SECONDS == original
    assert settings.get("REMINDER_POLL_SECONDS") is None


def test_a_secret_cannot_be_set_through_the_api():
    status, _ = call("patch", "/api/settings",
                     token=TOKEN_OWNER, json={"DISCORD_TOKEN": "hunter2"})
    assert status == 400
    assert settings.get("DISCORD_TOKEN") is None


# ── malformed bodies must be a clean 400, not a 500 ─────────────────────────
# request.json() is plain json.loads: a non-dict body like [1,2,3] decodes
# fine and then .get()/.items() raises AttributeError deeper in the handler,
# and malformed text raises inside request.json() itself. _json_object()
# (already used by the pre-existing conversation routes) turns both into a
# clean 400 -- these routes must go through it too, not call request.json()
# directly.

def test_patch_plugin_with_a_non_dict_body_is_400_not_500():
    status, _ = call("patch", "/api/plugins/notes_skill",
                     token=TOKEN_OWNER, json=[1, 2, 3])
    assert status == 400
    assert settings.get("skill.notes_skill.enabled") is None


def test_patch_plugin_with_malformed_json_is_400_not_500():
    status, _ = call("patch", "/api/plugins/notes_skill",
                     token=TOKEN_OWNER, data="not json")
    assert status == 400
    assert settings.get("skill.notes_skill.enabled") is None


def test_patch_settings_with_a_non_dict_body_is_400_not_500():
    status, _ = call("patch", "/api/settings", token=TOKEN_OWNER, json=[1, 2, 3])
    assert status == 400
    assert settings.get("SEARXNG_URL") is None


def test_patch_settings_with_malformed_json_is_400_not_500():
    status, _ = call("patch", "/api/settings", token=TOKEN_OWNER, data="not json")
    assert status == 400
    assert settings.get("SEARXNG_URL") is None
