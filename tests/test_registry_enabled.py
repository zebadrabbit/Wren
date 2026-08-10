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
