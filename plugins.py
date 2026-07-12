import notes_plugin
import shopping_plugin

PLUGINS = [notes_plugin, shopping_plugin]

INTENT_HANDLERS = {intent: plugin for plugin in PLUGINS for intent in plugin.INTENTS}

def all_intents() -> list[str]:
    return [intent for plugin in PLUGINS for intent in plugin.INTENTS]

def all_guidelines() -> str:
    return "\n".join(plugin.PROMPT_GUIDELINES for plugin in PLUGINS)

async def start_all(client) -> None:
    for plugin in PLUGINS:
        if hasattr(plugin, "start"):
            await plugin.start(client)
