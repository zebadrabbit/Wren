import asyncio
import notes_plugin
import shopping_plugin
import email_plugin

PLUGINS = [notes_plugin, shopping_plugin, email_plugin]

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
