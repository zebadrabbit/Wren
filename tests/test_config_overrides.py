import pytest

from wren import config
from wren import router
from wren import settings


def test_secrets_are_not_settable():
    # The security boundary. SETTABLE is a positive allowlist; if a credential
    # ever appears in it, GET /api/plugins leaks it.
    for secret in ("DISCORD_TOKEN", "TELEGRAM_TOKEN", "IMAP_PASSWORD",
                   "GITHUB_TOKEN", "OPENAI_API_KEY", "WREN_TOKENS"):
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
