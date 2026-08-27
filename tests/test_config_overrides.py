import os

import pytest

from wren import config
from wren import router
from wren import settings


def test_secrets_are_not_settable():
    # The security boundary. SETTABLE is a positive allowlist; if a credential
    # ever appears in it, GET /api/plugins leaks it.
    #
    # FIRECRAWL_API_KEY sits directly beside the settable FIRECRAWL_URL in
    # config.py -- exactly the credential a future "add Firecrawl settings"
    # change would sweep in alongside its URL by accident. OPENAI_API_KEY is
    # kept even though it is not a `config` attribute at all (it's read
    # straight from os.environ inside providers.py), so this particular
    # assertion is a no-op for it -- harmless to keep, real for the rest.
    for secret in ("DISCORD_TOKEN", "TELEGRAM_TOKEN", "IMAP_PASSWORD",
                   "GITHUB_TOKEN", "OPENAI_API_KEY", "WREN_TOKENS",
                   "FIRECRAWL_API_KEY"):
        assert secret not in config.SETTABLE


def test_set_override_rejects_a_key_outside_the_allowlist():
    settings.init_db()
    with pytest.raises(KeyError):
        config.set_override("DISCORD_TOKEN", "hunter2")
    assert settings.get("DISCORD_TOKEN") is None


def test_set_override_applies_immediately_to_the_module():
    settings.init_db()
    config.set_override("SEARXNG_URL", "http://searx.lan")
    try:
        assert config.SEARXNG_URL == "http://searx.lan"
        assert settings.get("SEARXNG_URL") == "http://searx.lan"
    finally:
        config.clear_override("SEARXNG_URL")


def test_clear_override_restores_the_env_default():
    settings.init_db()
    original = config.SEARXNG_URL
    config.set_override("SEARXNG_URL", "http://searx.lan")
    config.clear_override("SEARXNG_URL")
    assert config.SEARXNG_URL == original
    assert settings.get("SEARXNG_URL") is None


def test_poll_intervals_are_coerced_to_int():
    settings.init_db()
    config.set_override("REMINDER_POLL_SECONDS", "45")
    try:
        assert config.REMINDER_POLL_SECONDS == 45
    finally:
        config.clear_override("REMINDER_POLL_SECONDS")


@pytest.mark.parametrize("bad", ["0", "1", "4", "-10"])
def test_poll_intervals_below_the_floor_are_rejected(bad):
    # A 0 would spin the reminder loop hot and hammer IMAP and GitHub's rate
    # limit. This is the sharpest footgun in the editable set.
    settings.init_db()
    with pytest.raises(ValueError):
        config.set_override("REMINDER_POLL_SECONDS", bad)
    assert settings.get("REMINDER_POLL_SECONDS") is None


def test_a_rejected_value_is_not_persisted_and_does_not_change_config():
    settings.init_db()
    original = config.TIMEZONE
    with pytest.raises((ValueError, RuntimeError)):
        config.set_override("TIMEZONE", "Mars/Olympus_Mons")
    assert config.TIMEZONE == original
    assert settings.get("TIMEZONE") is None


def test_github_watch_is_parsed_into_a_list():
    settings.init_db()
    config.set_override("GITHUB_WATCH", "a/b, c/d")
    try:
        assert config.GITHUB_WATCH == ["a/b", "c/d"]
    finally:
        config.clear_override("GITHUB_WATCH")


def test_github_watch_round_trips_through_serialize_setting():
    # Finding 1: GET /api/plugins and PATCH /api/settings both show this value
    # via config.serialize_setting(). str(["a/b", "c/d"]) would emit the repr
    # "['a/b', 'c/d']", which _parse_github_watch's bare comma-split accepts
    # without complaint and turns into garbage entries. Feeding the
    # serialized form straight back through set_override() must reproduce
    # the original list exactly.
    settings.init_db()
    config.set_override("GITHUB_WATCH", "zebadrabbit/Wren,other/repo")
    try:
        serialized = config.serialize_setting("GITHUB_WATCH")
        config.set_override("GITHUB_WATCH", serialized)
        assert config.GITHUB_WATCH == ["zebadrabbit/Wren", "other/repo"]
    finally:
        config.clear_override("GITHUB_WATCH")


def test_email_watch_round_trips_through_serialize_setting():
    # Same failure mode as GITHUB_WATCH above, but for the dict-typed setting:
    # str({...}) emits a repr that _parse_email_watch's split(":", 1) mangles
    # into garbage keys instead of rejecting.
    settings.init_db()
    config.set_override("EMAIL_WATCH", "alice@example.com:alice,bob@example.com:bob")
    try:
        serialized = config.serialize_setting("EMAIL_WATCH")
        config.set_override("EMAIL_WATCH", serialized)
        assert config.EMAIL_WATCH == {"alice@example.com": "alice", "bob@example.com": "bob"}
    finally:
        config.clear_override("EMAIL_WATCH")


def test_serialize_setting_defaults_to_str_for_scalar_settings():
    settings.init_db()
    config.set_override("REMINDER_POLL_SECONDS", "45")
    try:
        assert config.serialize_setting("REMINDER_POLL_SECONDS") == "45"
    finally:
        config.clear_override("REMINDER_POLL_SECONDS")


def test_notify_via_must_name_a_running_channel():
    settings.init_db()
    router.reset()
    with pytest.raises(ValueError):
        config.set_override("NOTIFY_VIA", "telegram")


def test_notify_via_rejects_a_send_only_channel():
    settings.init_db()
    router.reset()

    class SendOnly:
        CAN_NOTIFY = False

    router.register("http", SendOnly())
    try:
        with pytest.raises(ValueError):
            config.set_override("NOTIFY_VIA", "http")
    finally:
        router.reset()


def test_notify_via_accepts_a_channel_that_can_push():
    settings.init_db()
    router.reset()

    class Pusher:
        CAN_NOTIFY = True

    router.register("discord", Pusher())
    try:
        config.set_override("NOTIFY_VIA", "discord")
        assert config.NOTIFY_VIA == "discord"
    finally:
        config.clear_override("NOTIFY_VIA")
        router.reset()


def test_apply_overrides_reads_stored_rows_onto_the_module():
    settings.init_db()
    settings.set("SEARXNG_URL", "http://from-db")
    try:
        config.apply_overrides()
        assert config.SEARXNG_URL == "http://from-db"
    finally:
        # clear_override, not unset()+apply_overrides(): apply_overrides only
        # overlays keys present in settings.all() and has no path that
        # restores a key to _DEFAULTS once its row is gone, so unset() alone
        # would leave config.SEARXNG_URL at "http://from-db" for the rest of
        # the process.
        config.clear_override("SEARXNG_URL")


def test_apply_overrides_skips_the_skill_namespace_but_still_warns_on_unknown_keys(caplog):
    # Finding 3: skill.<module>.enabled rows are registry.py's namespace
    # (applied via registry.is_enabled(), not SETTABLE), sharing this same
    # settings table. Without the skip, apply_overrides() treats every one
    # of them as an unknown stored setting and logs a false warning on every
    # boot that has a toggled skill -- which trains the operator to ignore
    # the one warning that would actually mean something.
    settings.init_db()
    settings.set("skill.notes_skill.enabled", "0")
    settings.set("SETTING_FROM_THE_FUTURE", "x")
    try:
        config.apply_overrides()
        assert "skill.notes_skill.enabled" not in caplog.text
        assert "SETTING_FROM_THE_FUTURE" in caplog.text
    finally:
        settings.unset("skill.notes_skill.enabled")
        settings.unset("SETTING_FROM_THE_FUTURE")


def test_apply_overrides_ignores_an_unknown_key_instead_of_crashing(caplog):
    # An upgrade may drop a setting. Wren must still boot on a row it wrote.
    settings.init_db()
    settings.set("SETTING_FROM_THE_FUTURE", "x")
    try:
        config.apply_overrides()
        assert "SETTING_FROM_THE_FUTURE" in caplog.text
    finally:
        settings.unset("SETTING_FROM_THE_FUTURE")


def test_apply_overrides_ignores_a_stored_value_that_no_longer_validates(caplog):
    settings.init_db()
    settings.set("REMINDER_POLL_SECONDS", "0")
    original = config.REMINDER_POLL_SECONDS
    try:
        config.apply_overrides()
        assert config.REMINDER_POLL_SECONDS == original
        assert "REMINDER_POLL_SECONDS" in caplog.text
    finally:
        settings.unset("REMINDER_POLL_SECONDS")
        config.apply_overrides()


def test_apply_overrides_accepts_a_stored_notify_via_with_an_empty_router():
    # Regression: run.py calls apply_overrides() right after init_dbs(), before
    # any communication plugin's start() task exists, so router._surfaces is
    # guaranteed empty at that moment. If apply_overrides() validated NOTIFY_VIA
    # through the strict, router-checking coercer, a persisted NOTIFY_VIA row
    # would fail every boot, get logged as invalid, and silently revert to the
    # .env default forever -- the stored row would never take effect. The boot
    # path must accept it leniently instead.
    settings.init_db()
    router.reset()
    settings.set("NOTIFY_VIA", "telegram")
    try:
        config.apply_overrides()
        assert config.NOTIFY_VIA == "telegram"
    finally:
        # clear_override, not unset()+apply_overrides() -- see Finding 2 above:
        # apply_overrides() only overlays keys present in settings.all() and
        # has no path back to _DEFAULTS once the row is gone.
        config.clear_override("NOTIFY_VIA")
        router.reset()


def test_every_settable_is_a_setting_record():
    for key, spec in config.SETTABLE.items():
        assert isinstance(spec, config.Setting), f"{key} is not a Setting"
        assert callable(spec.coerce), f"{key}.coerce is not callable"


def test_every_settable_round_trips_through_serialize():
    # serialize_setting(key) must produce a string that set_override(key, ...)
    # turns back into the identical value. This is the property that broke when
    # GITHUB_WATCH was served as a Python repr.
    settings.init_db()
    for key in config.SETTABLE:
        if key == "NOTIFY_VIA":
            continue  # validated against the router, which is empty in tests
        original = getattr(config, key)
        text = config.serialize_setting(key)
        try:
            config.set_override(key, text)
            assert getattr(config, key) == original, f"{key} did not round-trip"
        finally:
            config.clear_override(key)


def test_notify_via_still_validates_strictly_on_the_interactive_path():
    settings.init_db()
    router.reset()
    with pytest.raises(ValueError):
        config.set_override("NOTIFY_VIA", "telegram")


def test_notify_via_still_applies_leniently_at_boot():
    # apply_overrides runs before any plugin registers, so the router is empty
    # and a strict check would discard every stored value.
    settings.init_db()
    router.reset()
    settings.set("NOTIFY_VIA", "telegram")
    try:
        config.apply_overrides()
        assert config.NOTIFY_VIA == "telegram"
    finally:
        settings.unset("NOTIFY_VIA")
        config.apply_overrides()


def test_model_keys_are_settable_but_api_keys_are_not():
    for key in ("OLLAMA_MODEL", "LMSTUDIO_MODEL", "OPENAI_MODEL",
                "CLAUDE_MODEL", "OPENROUTER_MODEL"):
        assert key in config.SETTABLE
    for secret in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
        assert secret not in config.SETTABLE


def test_setting_a_model_changes_what_the_chain_will_use(monkeypatch):
    # The chain is resolved at import with the model baked in, so this proves
    # the rebuild actually happened rather than just an attribute being set.
    settings.init_db()
    # Snapshot LLM_CHAIN before this test's own reload_llm_chain() calls
    # touch it, and restore it after clear_override in the finally below --
    # monkeypatch's own teardown (restoring LMSTUDIO_BASE_URL) runs AFTER
    # this function returns, so relying on a post-test reload to clean up
    # LLM_CHAIN would leave a stale, fake-base-url entry in it for the rest
    # of the process. Explicit restore here means teardown order can't matter.
    original_chain = list(config.LLM_CHAIN)
    monkeypatch.setenv("LMSTUDIO_BASE_URL", "http://test")
    monkeypatch.setenv("LMSTUDIO_MODEL", "before-model")
    config.reload_llm_chain()
    try:
        assert any(c["model"] == "before-model" for c in config.LLM_CHAIN)

        config.set_override("LMSTUDIO_MODEL", "after-model")
        assert any(c["model"] == "after-model" for c in config.LLM_CHAIN)
        assert not any(c["model"] == "before-model" for c in config.LLM_CHAIN)
        assert os.environ["LMSTUDIO_MODEL"] == "after-model"
    finally:
        config.clear_override("LMSTUDIO_MODEL")
        config.LLM_CHAIN = original_chain


def test_a_rebuild_that_would_empty_the_chain_is_refused(monkeypatch):
    # Wren failing to boot with no provider is a loud, clear error. Wren
    # silently losing its last provider at runtime because someone touched a
    # dropdown is not.
    settings.init_db()
    before = list(config.LLM_CHAIN)
    monkeypatch.setattr(config, "_provider_names", [])
    with pytest.raises(RuntimeError):
        config.reload_llm_chain()
    assert config.LLM_CHAIN == before


def test_apply_model_does_not_drift_state_on_an_embedded_nul():
    # Finding 4: os.environ.__setitem__ raises ValueError on an embedded NUL
    # (e.g. a stray b"\x00" in a PATCH /api/settings body). _apply_model used
    # to write globals()[key] before os.environ, so that raise left
    # config.LMSTUDIO_MODEL holding the new (bad) value with no matching
    # os.environ write and no rollback path -- the only drift in the
    # four-way atomicity (module attr / os.environ / LLM_CHAIN / persisted
    # row) that survived the rest of the review.
    settings.init_db()
    original_chain = list(config.LLM_CHAIN)
    attr_before = config.LMSTUDIO_MODEL
    env_before = os.environ.get("LMSTUDIO_MODEL")
    try:
        with pytest.raises(ValueError):
            config.set_override("LMSTUDIO_MODEL", "bad\x00model")
        assert config.LMSTUDIO_MODEL == attr_before
        assert os.environ.get("LMSTUDIO_MODEL") == env_before
        assert config.LLM_CHAIN == original_chain
        assert settings.get("LMSTUDIO_MODEL") is None
    finally:
        config.LLM_CHAIN = original_chain


def test_a_refused_set_override_does_not_persist_the_rejected_value(monkeypatch):
    # Reproduces the real PATCH /api/settings path: an owner blanking the one
    # model that resolves. The empty-chain guard must refuse it, and the
    # settings table must not be left holding the refused value -- an
    # actively wrong persisted row is worse than an absent one.
    settings.init_db()
    original_chain = list(config.LLM_CHAIN)
    monkeypatch.setattr(config, "_provider_names", ["lmstudio"])
    monkeypatch.setenv("LMSTUDIO_BASE_URL", "http://test")
    monkeypatch.setenv("LMSTUDIO_MODEL", "only-model")
    config.reload_llm_chain()
    # config.LMSTUDIO_MODEL (the module attribute) is untouched by the
    # monkeypatch.setenv calls above -- only _apply_model writes it -- so its
    # pre-attempt value is whatever import left it at, not "only-model".
    chain_before_attempt = list(config.LLM_CHAIN)
    attr_before_attempt = config.LMSTUDIO_MODEL
    try:
        with pytest.raises(RuntimeError):
            config.set_override("LMSTUDIO_MODEL", "")

        assert settings.get("LMSTUDIO_MODEL") is None
        assert os.environ["LMSTUDIO_MODEL"] == "only-model"
        assert config.LMSTUDIO_MODEL == attr_before_attempt
        assert config.LLM_CHAIN == chain_before_attempt
    finally:
        config.LLM_CHAIN = original_chain


def test_memory_settings_are_settable():
    assert "MEMORY_SWEEP_SECONDS" in config.SETTABLE
    assert "MEMORY_DEDUP_THRESHOLD" in config.SETTABLE
    assert "MEMORY_TOP_K" in config.SETTABLE


def test_dedup_threshold_rejects_a_value_outside_zero_to_one():
    with pytest.raises(ValueError):
        config.SETTABLE["MEMORY_DEDUP_THRESHOLD"].coerce("1.5")


def test_calendar_cache_seconds_rejects_zero():
    with pytest.raises(ValueError):
        config.SETTABLE["CALENDAR_CACHE_SECONDS"].coerce("0")


def test_weather_coordinates_are_range_checked():
    assert config.SETTABLE["WEATHER_LAT"].coerce("41.88") == 41.88
    assert config.SETTABLE["WEATHER_LAT"].coerce("") is None
    with pytest.raises(ValueError):
        config.SETTABLE["WEATHER_LAT"].coerce("91")
    with pytest.raises(ValueError):
        config.SETTABLE["WEATHER_LON"].coerce("-181")


def test_weather_units_is_an_enum():
    assert config.SETTABLE["WEATHER_UNITS"].coerce(" Celsius ") == "celsius"
    with pytest.raises(ValueError):
        config.SETTABLE["WEATHER_UNITS"].coerce("kelvin")
