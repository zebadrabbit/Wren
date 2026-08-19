import logging

from . import config

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
    name = via or config.NOTIFY_VIA
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
