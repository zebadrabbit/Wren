import os, asyncio
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("HUSBAND_ID", "2")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

import plugins
import notes_plugin
import shopping_plugin

def test_all_intents_includes_both_plugins():
    intents = plugins.all_intents()
    for intent in notes_plugin.INTENTS + shopping_plugin.INTENTS:
        assert intent in intents

def test_intent_handlers_maps_to_correct_plugin():
    assert plugins.INTENT_HANDLERS["save_note"] is notes_plugin
    assert plugins.INTENT_HANDLERS["add_shopping_item"] is shopping_plugin

def test_all_guidelines_includes_both_plugins_text():
    guidelines = plugins.all_guidelines()
    assert "save_note" in guidelines
    assert "add_shopping_item" in guidelines

def test_start_all_noop_when_no_plugin_defines_start():
    # neither notes_plugin nor shopping_plugin defines start() yet
    asyncio.run(plugins.start_all(None))  # should not raise
