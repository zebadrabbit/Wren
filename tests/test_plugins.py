import os, asyncio, types
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("HUSBAND_ID", "2")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from wren import plugins
from wren import notes_plugin
from wren import shopping_plugin
from wren import email_plugin
from wren import reminder_plugin
from wren import contacts_plugin
from wren import pins_plugin

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

def test_start_all_noop_when_no_plugin_defines_start(monkeypatch):
    # neither notes_plugin nor shopping_plugin defines start() yet
    # (email_plugin does, but its start() is a no-op when EMAIL_WATCH is unset)
    monkeypatch.setattr(plugins, "_started_plugins", set())
    monkeypatch.setattr(plugins, "_tasks", [])
    asyncio.run(plugins.start_all(None))  # should not raise

def test_event_only_plugin_contributes_nothing_to_intents_or_guidelines(monkeypatch):
    intents_before = plugins.all_intents()
    guidelines_before = plugins.all_guidelines()
    fake = types.SimpleNamespace()  # no INTENTS, no PROMPT_GUIDELINES, no handle
    monkeypatch.setattr(plugins, "PLUGINS", plugins.PLUGINS + [fake])
    assert plugins.all_intents() == intents_before
    assert plugins.all_guidelines() == guidelines_before

def test_start_all_does_not_block_on_long_running_plugin(monkeypatch):
    class SlowPlugin:
        async def start(self, client):
            await asyncio.sleep(3600)

    monkeypatch.setattr(plugins, "PLUGINS", [SlowPlugin()])
    monkeypatch.setattr(plugins, "_started_plugins", set())
    monkeypatch.setattr(plugins, "_tasks", [])

    async def run():
        await asyncio.wait_for(plugins.start_all(None), timeout=1)
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()

    asyncio.run(run())

def test_start_all_is_idempotent_across_repeated_calls(monkeypatch):
    # Simulates discord.py's on_ready re-firing on reconnect/RESUME: start_all
    # may be invoked more than once per process, but a given plugin's start()
    # must only ever be scheduled once.
    call_count = 0

    class CountingPlugin:
        async def start(self, client):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(3600)

    monkeypatch.setattr(plugins, "PLUGINS", [CountingPlugin()])
    monkeypatch.setattr(plugins, "_started_plugins", set())
    monkeypatch.setattr(plugins, "_tasks", [])

    async def run():
        await asyncio.wait_for(plugins.start_all(None), timeout=1)
        await asyncio.wait_for(plugins.start_all(None), timeout=1)
        # give the scheduled task(s) a tick to actually start running
        await asyncio.sleep(0)
        assert call_count == 1
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()

    asyncio.run(run())

def test_email_plugin_registered():
    assert email_plugin in plugins.PLUGINS

def test_all_intents_unaffected_by_event_only_email_plugin():
    intents = plugins.all_intents()
    assert "save_note" in intents
    assert "add_shopping_item" in intents

def test_reminder_plugin_registered():
    assert reminder_plugin in plugins.PLUGINS

def test_all_intents_includes_reminder_intents():
    intents = plugins.all_intents()
    for intent in reminder_plugin.INTENTS:
        assert intent in intents

def test_web_intents_registered():
    from wren import plugins, web_plugin
    for intent in ("web_search", "read_page"):
        assert intent in plugins.all_intents()
        assert plugins.INTENT_HANDLERS[intent] is web_plugin

def test_contacts_plugin_registered():
    assert contacts_plugin in plugins.PLUGINS

def test_all_intents_includes_contacts_intents():
    intents = plugins.all_intents()
    for intent in contacts_plugin.INTENTS:
        assert intent in intents

def test_pins_plugin_registered():
    assert pins_plugin in plugins.PLUGINS

def test_all_intents_includes_pins_intents():
    intents = plugins.all_intents()
    for intent in pins_plugin.INTENTS:
        assert intent in intents

def test_plugin_status_defaults_to_active_true_when_is_active_absent(monkeypatch):
    fake = types.SimpleNamespace(PLUGIN_NAME="Fake Plugin")
    monkeypatch.setattr(plugins, "PLUGINS", [fake])
    assert plugins.plugin_status() == [("Fake Plugin", True)]

def test_plugin_status_calls_is_active_when_present(monkeypatch):
    fake = types.SimpleNamespace(PLUGIN_NAME="Fake Plugin", is_active=lambda: False)
    monkeypatch.setattr(plugins, "PLUGINS", [fake])
    assert plugins.plugin_status() == [("Fake Plugin", False)]

def test_plugin_status_falls_back_to_module_name_when_plugin_name_absent(monkeypatch):
    fake = types.SimpleNamespace()
    fake.__name__ = "wren.fake_plugin"
    monkeypatch.setattr(plugins, "PLUGINS", [fake])
    assert plugins.plugin_status() == [("wren.fake_plugin", True)]

def test_plugin_status_reflects_real_plugins_order_and_names():
    names = [name for name, _ in plugins.plugin_status()]
    assert names == [
        "Notes & Ideas", "Shopping List", "Email Watcher",
        "Reminders", "Web Lookup", "Contacts", "Pins", "GitHub Watcher",
    ]

def test_plugin_status_web_plugin_inactive_without_searxng_url(monkeypatch):
    from wren import config
    monkeypatch.setattr(config, "SEARXNG_URL", "")
    status = dict(plugins.plugin_status())
    assert status["Web Lookup"] is False

def test_plugin_status_web_plugin_active_with_searxng_url(monkeypatch):
    from wren import config
    monkeypatch.setattr(config, "SEARXNG_URL", "http://searxng.local")
    status = dict(plugins.plugin_status())
    assert status["Web Lookup"] is True

def test_plugin_status_email_plugin_inactive_without_email_watch(monkeypatch):
    from wren import config
    monkeypatch.setattr(config, "EMAIL_WATCH", {})
    status = dict(plugins.plugin_status())
    assert status["Email Watcher"] is False

def test_plugin_status_email_plugin_active_with_email_watch(monkeypatch):
    from wren import config
    monkeypatch.setattr(config, "EMAIL_WATCH", {"alice@example.com": "owner"})
    status = dict(plugins.plugin_status())
    assert status["Email Watcher"] is True

def test_plugin_status_always_active_plugin_reports_true():
    status = dict(plugins.plugin_status())
    assert status["Notes & Ideas"] is True
    assert status["Shopping List"] is True
    assert status["Reminders"] is True
    assert status["Contacts"] is True
    assert status["Pins"] is True
