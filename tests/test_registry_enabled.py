import sqlite3

import pytest

from wren import registry
from wren import settings
from wren.skills import notes_skill
from wren.skills import web_skill


def test_a_skill_with_no_stored_row_is_enabled():
    settings.init_db()
    assert registry.is_enabled(notes_skill) is True


def test_skill_key_is_the_module_basename():
    assert registry.skill_key(notes_skill) == "notes_skill"


def test_set_enabled_false_is_readable_back():
    settings.init_db()
    registry.set_enabled(notes_skill, False)
    try:
        assert registry.is_enabled(notes_skill) is False
    finally:
        registry.set_enabled(notes_skill, True)


def test_disabled_skill_drops_out_of_all_intents():
    settings.init_db()
    assert "save_note" in registry.all_intents()
    registry.set_enabled(notes_skill, False)
    try:
        assert "save_note" not in registry.all_intents()
    finally:
        registry.set_enabled(notes_skill, True)
    assert "save_note" in registry.all_intents()


def test_disabled_skill_drops_out_of_all_guidelines():
    settings.init_db()
    registry.set_enabled(web_skill, False)
    try:
        assert "web_search" not in registry.all_guidelines()
    finally:
        registry.set_enabled(web_skill, True)


def test_intent_handlers_is_left_intact_when_a_skill_is_disabled():
    # INTENT_HANDLERS stays a plain dict that core indexes and tests assert
    # against; enforcement happens at dispatch, not by mutating it.
    settings.init_db()
    registry.set_enabled(notes_skill, False)
    try:
        assert registry.INTENT_HANDLERS["save_note"] is notes_skill
    finally:
        registry.set_enabled(notes_skill, True)


def test_set_enabled_reregisters_the_intent_list_with_brain():
    # The trap: core.py calls brain.register_plugins() once at import, so the
    # intent list is baked into brain's globals at startup. Without this
    # re-registration a toggle never reaches the LLM at all.
    from wren import brain

    settings.init_db()
    registry.set_enabled(notes_skill, False)
    try:
        assert "save_note" not in brain._plugin_intents
    finally:
        registry.set_enabled(notes_skill, True)
    assert "save_note" in brain._plugin_intents


def test_enabled_plugins_excludes_the_disabled_ones():
    settings.init_db()
    registry.set_enabled(web_skill, False)
    try:
        assert web_skill not in registry.enabled_plugins()
        assert notes_skill in registry.enabled_plugins()
    finally:
        registry.set_enabled(web_skill, True)


def test_is_enabled_defaults_true_when_settings_table_does_not_exist_yet():
    # Deliberately no settings.init_db() call. This is the bootstrap path
    # is_enabled() must tolerate: core.py's module-level
    # brain.register_plugins(registry.all_intents(), ...) walks is_enabled()
    # for every plugin, and that can run before anything has created the
    # settings table (e.g. a test importing wren.core directly, or a
    # brand-new install before run.py's init_dbs() has run). conftest's
    # isolated_db fixture points WREN_DB at a fresh, table-less file for this
    # test, so this is the "no such table" branch, not the "row is missing"
    # one -- both must default to enabled.
    assert registry.is_enabled(notes_skill) is True


def test_is_enabled_reraises_operational_errors_other_than_missing_table(monkeypatch):
    # The narrowing this guards: only "no such table" (bootstrap) is
    # swallowed. A locked database or disk I/O error must NOT be silently
    # reported as "enabled" -- Task 4 wires is_enabled() into per-message
    # dispatch, where swallowing that would mean a broken DB silently
    # ignores the owner's configuration on every message.
    def boom(key):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(settings, "get", boom)
    with pytest.raises(sqlite3.OperationalError):
        registry.is_enabled(notes_skill)


def test_web_skill_explains_why_it_is_inactive(monkeypatch):
    from wren import config

    monkeypatch.setattr(config, "SEARXNG_URL", "")
    assert web_skill.is_active() is False
    assert "SEARXNG_URL" in web_skill.inactive_reason()


def test_gmail_explains_why_it_is_inactive(monkeypatch):
    from wren import config
    from wren.communication import gmail_plugin

    monkeypatch.setattr(config, "EMAIL_WATCH", {})
    assert gmail_plugin.is_active() is False
    assert "EMAIL_WATCH" in gmail_plugin.inactive_reason()


def test_telegram_explains_that_it_needs_a_token(monkeypatch):
    from wren import config
    from wren.communication import telegram_plugin

    monkeypatch.setattr(config, "TELEGRAM_TOKEN", "")
    assert telegram_plugin.is_active() is False
    assert "TELEGRAM_TOKEN" in telegram_plugin.inactive_reason()
