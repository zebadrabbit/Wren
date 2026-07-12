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

async def start_all(client) -> None:
    for plugin in PLUGINS:
        if hasattr(plugin, "start"):
            asyncio.create_task(plugin.start(client))
