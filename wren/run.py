import asyncio
import importlib
import logging

from . import config
from . import contacts
from . import conversations
from .communication import github_state
from .skills import memory_store as memory
from .skills import notes_store as notes
from .skills import pins_store as pins
from . import registry
from .skills import reminders_store as reminders
from .skills import shopping_store as shopping
from . import settings

logging.basicConfig(level=logging.INFO)

_STORAGE = (notes, shopping, reminders, contacts, github_state, pins, conversations, settings, memory)


def init_dbs() -> None:
    for module in _STORAGE:
        module.init_db()


def _warn_if_notifications_go_nowhere(loaded: dict) -> None:
    """Say so loudly at boot rather than silently eating reminders at 3am."""
    target = config.NOTIFY_VIA
    surface = loaded.get(target)
    if surface is None:
        logging.warning(
            f"NOTIFY_VIA='{target}' is not in COMMUNICATION_PLUGINS ({', '.join(config.COMMUNICATION_PLUGINS)}). "
            f"Reminders and watcher alerts will NOT be delivered."
        )
    elif not getattr(surface, "CAN_NOTIFY", True):
        logging.warning(
            f"NOTIFY_VIA='{target}' is send-only and cannot deliver unprompted "
            f"messages. Reminders and watcher alerts will be DISCARDED. "
            f"Set NOTIFY_VIA to a communication plugin that can push."
        )


async def main() -> None:
    init_dbs()
    # After init_dbs (the settings table must exist) and before any plugin
    # starts (or a plugin reads a stale value during startup). The ordering
    # looks arbitrary and is not.
    config.apply_overrides()

    if not config.COMMUNICATION_PLUGINS:
        raise RuntimeError("No communication plugins enabled. Set COMMUNICATION_PLUGINS (e.g. COMMUNICATION_PLUGINS=discord,http).")

    tasks = []
    loaded = {}
    for name in config.COMMUNICATION_PLUGINS:
        try:
            surface = importlib.import_module(f".communication.{name}_plugin", __package__)
        except ModuleNotFoundError as e:
            raise RuntimeError(f"Unknown communication plugin '{name}' in COMMUNICATION_PLUGINS: {e}") from e
        loaded[name] = surface
        logging.info(f"Starting communication plugin: {name}")
        tasks.append(asyncio.create_task(surface.start(), name=f"communication:{name}"))

    _warn_if_notifications_go_nowhere(loaded)

    await registry.start_all()
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
