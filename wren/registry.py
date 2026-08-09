import asyncio
from .skills import notes_skill
from .skills import shopping_skill
from .communication import gmail_plugin
from .skills import reminder_skill
from .skills import web_skill
from .skills import contacts_skill
from .skills import pins_skill
from .communication import github_plugin

PLUGINS = [notes_skill, shopping_skill, reminder_skill, web_skill, contacts_skill, pins_skill]
# NOTE: gmail_plugin / github_plugin are Communication (input-only) plugins now,
# not Skills -- they do not take chat intents, so they are intentionally left out
# of PLUGINS/INTENT_HANDLERS. They are started directly by run.py alongside the
# other enabled communication plugins.

# ...but they are still part of the answer to "what plugins do you have". An
# input-only watcher is invisible in conversation until the moment it fires, so
# plugin_status() is the only place a user can see whether the email/GitHub
# watchers are configured. Chat plugins (discord, http) are deliberately NOT
# listed here: the one you are reading the answer through needs no announcing.
WATCHERS = [gmail_plugin, github_plugin]

INTENT_HANDLERS = {
    intent: plugin
    for plugin in PLUGINS
    for intent in getattr(plugin, "INTENTS", [])
}

def all_intents() -> list[str]:
    return [intent for plugin in PLUGINS for intent in getattr(plugin, "INTENTS", [])]

def all_guidelines() -> str:
    return "\n".join(
        text for plugin in PLUGINS
        if (text := getattr(plugin, "PROMPT_GUIDELINES", ""))
    )

def plugin_status() -> list[tuple[str, bool]]:
    return [
        (p.PLUGIN_NAME if hasattr(p, "PLUGIN_NAME") else p.__name__, getattr(p, "is_active", lambda: True)())
        for p in PLUGINS + WATCHERS
    ]

_started_plugins: set = set()
_tasks: list = []

async def start_all() -> None:
    for plugin in PLUGINS:
        if not hasattr(plugin, "start"):
            continue
        if plugin in _started_plugins:
            continue
        _started_plugins.add(plugin)
        _tasks.append(asyncio.create_task(plugin.start()))
