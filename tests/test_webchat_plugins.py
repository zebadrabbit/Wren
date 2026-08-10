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


def test_skill_rows_have_no_running_field():
    # Finding 5: `running` only means something for channels (started from
    # COMMUNICATION_PLUGINS at boot, restart-required to change). Skill rows
    # hardcoded running=True, a value chat.html never reads for skills --
    # dead data on a public JSON surface.
    _, body = call("get", "/api/plugins", token=TOKEN_OWNER)
    assert all("running" not in s for s in body["skills"])
    assert all("running" in c for c in body["channels"])


def test_patch_plugin_response_has_no_running_field():
    try:
        status, body = call("patch", "/api/plugins/notes_skill",
                             token=TOKEN_OWNER, json={"enabled": False})
        assert status == 200
        assert "running" not in body
    finally:
        settings.unset("skill.notes_skill.enabled")
        from wren.skills import notes_skill
        registry.set_enabled(notes_skill, True)


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


def test_github_watch_survives_a_round_trip_through_the_panel():
    # Finding 1. Set it, read back what GET /api/plugins shows, PATCH that
    # exact string back in (this is what the panel does on every save) --
    # the result must be the same list, not mangled garbage that GITHUB_WATCH
    # then silently applies.
    try:
        status, _ = call("patch", "/api/settings", token=TOKEN_OWNER,
                         json={"GITHUB_WATCH": "zebadrabbit/Wren,other/repo"})
        assert status == 200
        _, body = call("get", "/api/plugins", token=TOKEN_OWNER)
        shown = body["settings"]["GITHUB_WATCH"]

        status, body = call("patch", "/api/settings", token=TOKEN_OWNER,
                            json={"GITHUB_WATCH": shown})
        assert status == 200
        assert config.GITHUB_WATCH == ["zebadrabbit/Wren", "other/repo"]
    finally:
        config.clear_override("GITHUB_WATCH")


def test_email_watch_survives_a_round_trip_through_the_panel():
    try:
        status, _ = call("patch", "/api/settings", token=TOKEN_OWNER,
                         json={"EMAIL_WATCH": "alice@example.com:alice"})
        assert status == 200
        _, body = call("get", "/api/plugins", token=TOKEN_OWNER)
        shown = body["settings"]["EMAIL_WATCH"]

        status, body = call("patch", "/api/settings", token=TOKEN_OWNER,
                            json={"EMAIL_WATCH": shown})
        assert status == 200
        assert config.EMAIL_WATCH == {"alice@example.com": "alice"}
    finally:
        config.clear_override("EMAIL_WATCH")


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


# ── GET /api/me ──────────────────────────────────────────────────────────────
# Not owner-only: /api/plugins is, so a household member loading the page
# would get a 403 and no greeting at all. This endpoint returns only the
# caller's own name, plus two non-sensitive facts (enabled skills, active
# model) the landing screen needs and that a household member cannot get from
# any owner-only route.

def test_me_returns_the_owner_name_setting():
    config.set_override("OWNER_NAME", "Erin")
    try:
        status, body = call("get", "/api/me", token=TOKEN_OWNER)
        assert status == 200
        assert body["name"] == "Erin"
    finally:
        config.clear_override("OWNER_NAME")


def test_me_omits_the_name_when_owner_name_is_unset():
    # Better no name than greeting somebody as "owner".
    status, body = call("get", "/api/me", token=TOKEN_OWNER)
    assert status == 200
    assert body["name"] is None


def test_me_is_not_owner_only():
    # The whole reason this endpoint exists: /api/plugins 403s for a household
    # member, so the greeting cannot come from there.
    status, body = call("get", "/api/me", token=TOKEN_OTHER)
    assert status == 200
    assert "skills" in body


def test_me_titlecases_a_contact_alias():
    # The `tokens` fixture already monkeypatches config.id_to_name to map
    # USER_OTHER -> "bob", so no extra monkeypatching is needed here.
    from wren import contacts
    contacts.init_db()
    contacts.add("bob", USER_OTHER)
    status, body = call("get", "/api/me", token=TOKEN_OTHER)
    assert body["name"] == "Bob"


def test_me_requires_a_token():
    status, _ = call("get", "/api/me")
    assert status == 401


def test_me_reports_the_active_model_to_a_non_owner(monkeypatch):
    # /api/models is owner-only, so this is the only way a household member's
    # composer can show which model is answering.
    monkeypatch.setattr(config, "LLM_CHAIN", [
        {"name": "ollama", "base_url": "http://test", "api_key": "x", "model": "a-model"}])
    status, body = call("get", "/api/me", token=TOKEN_OTHER)
    assert status == 200
    assert body["model"] == "a-model"


def test_me_lists_only_enabled_skills():
    from wren.skills import notes_skill
    status, body = call("get", "/api/me", token=TOKEN_OWNER)
    assert "notes_skill" in body["skills"]
    registry.set_enabled(notes_skill, False)
    try:
        _, body = call("get", "/api/me", token=TOKEN_OWNER)
        assert "notes_skill" not in body["skills"]
    finally:
        registry.set_enabled(notes_skill, True)
