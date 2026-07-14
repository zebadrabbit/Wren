import asyncio
from . import notes_plugin
from . import shopping_plugin
from . import email_plugin
from . import reminder_plugin
from . import web_plugin
from . import contacts_plugin

PLUGINS = [notes_plugin, shopping_plugin, email_plugin, reminder_plugin, web_plugin, contacts_plugin]

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

_started_plugins: set = set()
_tasks: list = []

async def start_all(client) -> None:
    for plugin in PLUGINS:
        if not hasattr(plugin, "start"):
            continue
        if plugin in _started_plugins:
            continue
        _started_plugins.add(plugin)
        _tasks.append(asyncio.create_task(plugin.start(client)))
