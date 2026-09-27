import logging

import pytest

from wren import config
from wren import settings


def test_env_defined_counts_present_keys_including_empty_ones():
    # "SEARXNG_URL=" is a deliberate "web search off", so it locks too; a key
    # that is absent (commented out in .env) does not.
    env = {"TIMEZONE": "America/Chicago", "SEARXNG_URL": "", "DISCORD_TOKEN": "x"}
    assert config._env_defined(env) == {"TIMEZONE", "SEARXNG_URL"}


def test_a_key_set_in_env_cannot_be_overridden(monkeypatch):
    settings.init_db()
    monkeypatch.setattr(config, "ENV_DEFINED", frozenset({"SEARXNG_URL"}))
    with pytest.raises(config.SettingLocked, match=r"\.env"):
        config.set_override("SEARXNG_URL", "http://elsewhere")
    assert settings.get("SEARXNG_URL") is None


def test_a_locked_setting_is_a_valueerror_so_every_caller_already_handles_it():
    assert issubclass(config.SettingLocked, ValueError)


def test_boot_drops_a_stored_row_that_env_now_defines(monkeypatch, caplog):
    # Set in the panel in June, put in .env in December: .env wins, and the
    # stale row is removed rather than lying in wait for the .env line to go.
    settings.init_db()
    settings.set("SEARXNG_URL", "http://june")
    monkeypatch.setattr(config, "ENV_DEFINED", frozenset({"SEARXNG_URL"}))
    monkeypatch.setattr(config, "SEARXNG_URL", "http://december")
    with caplog.at_level(logging.WARNING):
        config.apply_overrides()
    assert config.SEARXNG_URL == "http://december"
    assert settings.get("SEARXNG_URL") is None
    assert "SEARXNG_URL" in caplog.text


def test_keys_not_in_env_stay_editable(monkeypatch):
    settings.init_db()
    monkeypatch.setattr(config, "ENV_DEFINED", frozenset({"TIMEZONE"}))
    try:
        config.set_override("SEARXNG_URL", "http://searx.lan")
        assert config.SEARXNG_URL == "http://searx.lan"
    finally:
        config.clear_override("SEARXNG_URL")
