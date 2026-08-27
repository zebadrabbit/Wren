import asyncio
import sqlite3

from . import settings
from .skills import notes_skill
from .skills import shopping_skill
from .communication import gmail_plugin
from .skills import reminder_skill
from .skills import web_skill
from .skills import contacts_skill
from .skills import pins_skill
from .skills import memory_skill
from .skills import calendar_skill
from .skills import weather_skill
from .communication import github_plugin

PLUGINS = [notes_skill, shopping_skill, reminder_skill, web_skill, contacts_skill,
           pins_skill, memory_skill, calendar_skill, weather_skill]
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

def skill_key(plugin) -> str:
    """Settings key for a skill's on/off row.

    The module basename, not PLUGIN_NAME: a display name can be reworded, and
    a stored row must not lose its meaning when it is.
    """
    return plugin.__name__.rsplit(".", 1)[-1]


def is_enabled(plugin) -> bool:
    # Default on: a skill with no row is enabled, so the table records only
    # what the owner changed and a fresh install behaves exactly as before.
    try:
        return settings.get(f"skill.{skill_key(plugin)}.enabled") != "0"
    except sqlite3.OperationalError as e:
        if "no such table" not in str(e):
            raise
        # ponytail: a missing TABLE reads the same way as a missing ROW (no
        # stored deviation -> enabled), but ONLY for that one specific cause.
        # Reachable only during import: core.py registers the intent list
        # with brain at module scope, which walks all_intents() ->
        # is_enabled(), and that can run before any init_db(). In production
        # run.py's init_dbs() has always run first, so a missing table here
        # means bootstrap, not damage. Anything else -- a locked database, a
        # disk I/O error -- must NOT be silently reported as "enabled"; once
        # Task 4 wires this into per-message dispatch, swallowing those would
        # mean a broken DB silently ignores the owner's configuration on
        # every message instead of surfacing the failure. It propagates.
        return True


def set_enabled(plugin, on: bool) -> None:
    settings.set(f"skill.{skill_key(plugin)}.enabled", "1" if on else "0")
    # core.py registers the intent list with brain ONCE, at import. Without
    # re-registering here the LLM would keep being offered a skill the owner
    # just switched off. Local import: this is the single choke point every
    # writer goes through, so doing it here makes it impossible to forget --
    # and keeps brain out of registry's module-level imports.
    from . import brain

    brain.register_plugins(all_intents(), all_guidelines())


def enabled_plugins() -> list:
    return [p for p in PLUGINS if is_enabled(p)]


def all_intents() -> list[str]:
    return [intent for plugin in enabled_plugins() for intent in getattr(plugin, "INTENTS", [])]

def all_guidelines() -> str:
    return "\n".join(
        text for plugin in enabled_plugins()
        if (text := getattr(plugin, "PROMPT_GUIDELINES", ""))
    )

def plugin_status() -> list[tuple[str, bool]]:
    # is_active() alone answers "is it configured", not "is it on" -- without
    # the is_enabled() check here, an owner who switches a skill off in the
    # panel still gets told in chat that it's active. Safe for WATCHERS too:
    # they have no skill.<module>.enabled row, so is_enabled() defaults True
    # and their behaviour is unchanged.
    return [
        (p.PLUGIN_NAME if hasattr(p, "PLUGIN_NAME") else p.__name__,
         getattr(p, "is_active", lambda: True)() and is_enabled(p))
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
