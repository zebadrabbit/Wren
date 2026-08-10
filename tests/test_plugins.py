import os, asyncio, types
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from wren import registry as plugins
from wren.skills import notes_skill as notes_plugin
from wren.skills import shopping_skill as shopping_plugin
from wren.communication import gmail_plugin as email_plugin
from wren.skills import reminder_skill as reminder_plugin
from wren.skills import contacts_skill as contacts_plugin
from wren.skills import pins_skill as pins_plugin

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
    # neither notes_plugin nor shopping_plugin defines start() yet, and the
    # watchers that do (email, github) are no longer started via start_all()
    monkeypatch.setattr(plugins, "_started_plugins", set())
    monkeypatch.setattr(plugins, "_tasks", [])
    asyncio.run(plugins.start_all())  # should not raise

def test_event_only_plugin_contributes_nothing_to_intents_or_guidelines(monkeypatch):
    intents_before = plugins.all_intents()
    guidelines_before = plugins.all_guidelines()
    # no INTENTS, no PROMPT_GUIDELINES, no handle -- __name__ is still needed,
    # same as any real plugin module, now that is_enabled() keys off of it.
    fake = types.SimpleNamespace(__name__="wren.fake_event_plugin")
    monkeypatch.setattr(plugins, "PLUGINS", plugins.PLUGINS + [fake])
    assert plugins.all_intents() == intents_before
    assert plugins.all_guidelines() == guidelines_before

def test_start_all_does_not_block_on_long_running_plugin(monkeypatch):
    class SlowPlugin:
        async def start(self):
            await asyncio.sleep(3600)

    monkeypatch.setattr(plugins, "PLUGINS", [SlowPlugin()])
    monkeypatch.setattr(plugins, "_started_plugins", set())
    monkeypatch.setattr(plugins, "_tasks", [])

    async def run():
        await asyncio.wait_for(plugins.start_all(), timeout=1)
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
        async def start(self):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(3600)

    monkeypatch.setattr(plugins, "PLUGINS", [CountingPlugin()])
    monkeypatch.setattr(plugins, "_started_plugins", set())
    monkeypatch.setattr(plugins, "_tasks", [])

    async def run():
        await asyncio.wait_for(plugins.start_all(), timeout=1)
        await asyncio.wait_for(plugins.start_all(), timeout=1)
        # give the scheduled task(s) a tick to actually start running
        await asyncio.sleep(0)
        assert call_count == 1
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()

    asyncio.run(run())

def test_email_plugin_is_a_watcher_not_a_skill():
    # v2 taxonomy: the Gmail watcher is a communication plugin (ROLE="input")
    # started by run.py, not a Skill -- it owns no intents, so it must stay out
    # of PLUGINS/INTENT_HANDLERS. It is still user-visible through plugin_status().
    assert email_plugin not in plugins.PLUGINS
    assert email_plugin.ROLE == "input"
    assert email_plugin in plugins.WATCHERS
    assert email_plugin.PLUGIN_NAME in dict(plugins.plugin_status())

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
    from wren import registry as plugins
    from wren.skills import web_skill as web_plugin
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

# plugin_status() reports PLUGINS + WATCHERS, so these single-entry assertions
# blank out WATCHERS to isolate the naming/is_active behaviour under test.
def test_plugin_status_defaults_to_active_true_when_is_active_absent(monkeypatch):
    # __name__ is required here (not just PLUGIN_NAME): plugin_status() now
    # also consults is_enabled(), which keys off __name__ via skill_key(),
    # same as any real plugin module.
    fake = types.SimpleNamespace(PLUGIN_NAME="Fake Plugin", __name__="wren.fake_plugin")
    monkeypatch.setattr(plugins, "PLUGINS", [fake])
    monkeypatch.setattr(plugins, "WATCHERS", [])
    assert plugins.plugin_status() == [("Fake Plugin", True)]

def test_plugin_status_calls_is_active_when_present(monkeypatch):
    fake = types.SimpleNamespace(PLUGIN_NAME="Fake Plugin", is_active=lambda: False)
    monkeypatch.setattr(plugins, "PLUGINS", [fake])
    monkeypatch.setattr(plugins, "WATCHERS", [])
    assert plugins.plugin_status() == [("Fake Plugin", False)]

def test_plugin_status_falls_back_to_module_name_when_plugin_name_absent(monkeypatch):
    fake = types.SimpleNamespace()
    fake.__name__ = "wren.fake_plugin"
    monkeypatch.setattr(plugins, "PLUGINS", [fake])
    monkeypatch.setattr(plugins, "WATCHERS", [])
    assert plugins.plugin_status() == [("wren.fake_plugin", True)]

def test_plugin_status_reflects_real_plugins_order_and_names():
    # Skills first (in PLUGINS order), then the input-only watchers. The email
    # watcher used to sit third, between Shopping List and Reminders, back when
    # it was a capability plugin in the one flat PLUGINS list; in v2 it is a
    # communication plugin and lists with the other watchers at the end.
    names = [name for name, _ in plugins.plugin_status()]
    assert names == [
        "Notes & Ideas", "Shopping List", "Reminders", "Web Lookup",
        "Contacts", "Pins", "Gmail (IMAP) Watcher", "GitHub Watcher",
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
    assert status["Gmail (IMAP) Watcher"] is False

def test_plugin_status_email_plugin_active_with_email_watch(monkeypatch):
    from wren import config
    monkeypatch.setattr(config, "EMAIL_WATCH", {"alice@example.com": "owner"})
    status = dict(plugins.plugin_status())
    assert status["Gmail (IMAP) Watcher"] is True

def test_plugin_status_always_active_plugin_reports_true():
    status = dict(plugins.plugin_status())
    assert status["Notes & Ideas"] is True
    assert status["Shopping List"] is True
    assert status["Reminders"] is True
    assert status["Contacts"] is True
    assert status["Pins"] is True


def test_plugin_status_reflects_a_skill_disabled_through_the_panel():
    # Finding 2: plugin_status() used to report only is_active() (is it
    # configured), never is_enabled() (did the owner switch it off). An owner
    # who disables Notes in the panel and then asks "what plugins do you
    # have" was told it was still on. The existing regression test at
    # test_core.py::test_list_plugins_sends_one_line_per_plugin only counts
    # lines, so it could not have caught this.
    from wren import settings

    settings.init_db()
    plugins.set_enabled(notes_plugin, False)
    try:
        status = dict(plugins.plugin_status())
        assert status["Notes & Ideas"] is False
    finally:
        plugins.set_enabled(notes_plugin, True)
    assert dict(plugins.plugin_status())["Notes & Ideas"] is True
