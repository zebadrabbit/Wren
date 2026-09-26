import re
import logging

from . import config
from . import contacts
from . import db

# name -> surface module/object exposing `async notify(user_id, text) -> bool`
_surfaces: dict = {}


def register(name: str, surface) -> None:
    _surfaces[name] = surface


def registered() -> list[str]:
    return sorted(_surfaces)


def surface(name: str):
    """The registered surface for a name, or None. Exists so callers can check
    a channel's CAN_NOTIFY without reaching into _surfaces."""
    return _surfaces.get(name)


def reset() -> None:
    _surfaces.clear()


async def notify(user_id: int, text: str, via: str | None = None) -> bool:
    """Deliver an unprompted message (reminder, watcher alert) to a user on
    whichever surface this install is configured to use, or the one `via`
    names when the caller has a reason to override that (a reminder the user
    asked for on Discord while everything else goes to Telegram).

    Contract, and it matters: `False` means a PERMANENT failure — this user
    cannot be reached and retrying will not help (their DMs are closed, they
    blocked the bot). Transient failures — Discord 503, network blip — must
    RAISE, not return False.

    Callers consume the thing they were delivering when this returns False:
    reminder_plugin marks the reminder fired, email_plugin flags the mail
    \\Seen, github_plugin advances its watermark. So swallowing a transient
    error here silently destroys the message instead of retrying it on the
    next poll. Do not add a blanket `except Exception` back to this function.
    """
    if db.in_dry_run():
        # A dry run (http ?dry_run=1) must never DM a real person. True, not
        # False: the caller under test should take its "delivered" branch.
        logging.info(f"dry run: would notify {user_id}: {text[:80]!r}")
        return True
    name = via or config.NOTIFY_VIA
    if via is None:
        # Notifications follow the person: a contact with no id on the default
        # surface but one on another running surface is reached there. A
        # contact on the default surface, or the owner (not a contact, so an
        # empty list), takes the default. An explicit `via` is never
        # second-guessed -- the user asked for that surface by name.
        theirs = contacts.surfaces_of(user_id)
        if theirs and name not in theirs:
            name = next((s for s in theirs if s in _surfaces), name)
    surface = _surfaces.get(name)
    if surface is None:
        logging.warning(
            f"Cannot notify {user_id}: surface '{name}' "
            f"is not registered (have: {registered() or 'none'})"
        )
        return False
    return await surface.notify(user_id, text)


async def notify_name(contact_name: str, text: str) -> bool:
    """Same, addressed by contact alias rather than id."""
    user_id = config.whitelist().get((contact_name or "").lower())
    if not user_id:
        return False
    return await notify(user_id, text)


_NAMED = re.compile(r"\b(?:on|via|through)\s+([a-z]+)\b", re.I)


def named_surface(text: str) -> str | None:
    """The surface the user named in a sentence ("remind me on discord",
    "add 555 as hubby on telegram"), or None.

    Matched against what this install actually has rather than a list of
    transport names -- a skill has no business knowing what a "discord" is
    (CLAUDE.md rule 1), and the same check is what stops "remind me on
    tuesday" reading as a routing request. An unrecognised word after
    "on/via/through" is not a surface, it is part of the sentence.
    """
    m = _NAMED.search(text or "")
    if not m:
        return None
    name = m.group(1).lower()
    known = set(registered()) | set(config.COMMUNICATION_PLUGINS)
    return name if name in known else None
