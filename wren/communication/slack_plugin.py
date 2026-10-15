"""Slack communication plugin.

Role: chat (input + output), like telegram_plugin.py.

Socket Mode: Wren asks Slack for a websocket URL and dials out to it, so there
is no public URL, no inbound port and no request signing -- the same posture
as Telegram's long poll and Discord's gateway. The Web API is plain HTTPS +
JSON with the bot token in a header, so this speaks both directly with
aiohttp rather than pulling in slack_sdk.

Enable by adding "slack" to COMMUNICATION_PLUGINS in .env with SLACK_APP_TOKEN
(xapp-, scope connections:write), SLACK_BOT_TOKEN (xoxb-) and SLACK_OWNER_ID.
"""

import asyncio
import logging
import sys
import time

import aiohttp

from .. import config
from .. import contacts
from .. import core
from .. import filetypes
from .. import router
from ..channel import Inbound
from . import chunking

ROLE = "chat"
PLUGIN_NAME = "Slack"
CAN_NOTIFY = True

API_ROOT = "https://slack.com/api"

_CLIENT_TIMEOUT_SECONDS = 45
_RETRY_SECONDS = 5
_MAX_RETRY_SECONDS = 300
# Slack shows up to 4000 characters of a message and truncates the rest with
# a "show more" that many clients never render; past 40000 it is an error.
_MAX_MESSAGE_CHARS = 4000
# chat.postMessage errors that will never succeed for this person; everything
# else (ratelimited, invalid_auth, fatal_error) is retried by the caller.
_PERMANENT = frozenset({"channel_not_found", "user_not_found", "cannot_dm_bot",
                        "user_disabled", "account_inactive"})


def is_active() -> bool:
    return bool(config.SLACK_APP_TOKEN and config.SLACK_BOT_TOKEN)


def inactive_reason() -> str:
    missing = [k for k in ("SLACK_APP_TOKEN", "SLACK_BOT_TOKEN") if not getattr(config, k)]
    return (f"{' and '.join(missing)} not set. Add them and put 'slack' in "
            "COMMUNICATION_PLUGINS, then restart.")


class SlackError(RuntimeError):
    """The Web API answered ok=false. `error` is Slack's error string, which
    is what tells a permanent failure from a transient one -- see notify()."""

    def __init__(self, method: str, error: str):
        super().__init__(f"Slack {method} failed: {error}")
        self.error = error


def _scrub(text: str) -> str:
    """Both tokens out of anything that could reach a log. Tokens travel in
    headers, never URLs, so this is belt and braces -- but aiohttp puts
    request details into some exception strings, and journald is forever."""
    for token in (config.SLACK_BOT_TOKEN, config.SLACK_APP_TOKEN):
        if token:
            text = text.replace(token, "<token>")
    return text


async def _api(method: str, *, json: dict | None = None, token: str | None = None) -> dict:
    """POST to the Web API and return its body (callers read `ok` fields
    like `upload_url` off it). Raises SlackError on ok=false."""
    url = f"{API_ROOT}/{method}"
    headers = {"Authorization": f"Bearer {token or config.SLACK_BOT_TOKEN}"}
    timeout = aiohttp.ClientTimeout(total=_CLIENT_TIMEOUT_SECONDS)
    # ponytail: a fresh ClientSession per call, as telegram_plugin does; one
    # handshake per message is nothing and it dodges session lifecycle.
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=json or {}, headers=headers) as resp:
                body = await resp.json()
    except aiohttp.ClientError as e:
        raise aiohttp.ClientError(f"{type(e).__name__}: {_scrub(str(e))}") from None
    if not body.get("ok"):
        raise SlackError(method, str(body.get("error", "unknown")))
    return body


async def _download(url: str) -> bytes:
    """GET a file from files.slack.com; private files need the bot token."""
    headers = {"Authorization": f"Bearer {config.SLACK_BOT_TOKEN}"}
    timeout = aiohttp.ClientTimeout(total=_CLIENT_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                resp.raise_for_status()
                return await resp.read()
    except aiohttp.ClientError as e:
        raise aiohttp.ClientError(f"{type(e).__name__}: {_scrub(str(e))}") from None


async def _upload_bytes(url: str, data: bytes) -> None:
    """The middle call of Slack's upload flow: the bytes go to a one-time URL
    with no auth at all."""
    timeout = aiohttp.ClientTimeout(total=_CLIENT_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, data=data) as resp:
                resp.raise_for_status()
    except aiohttp.ClientError as e:
        raise aiohttp.ClientError(f"{type(e).__name__}: {_scrub(str(e))}") from None


def _chunks(text: str) -> list[str]:
    return chunking.chunks(text, _MAX_MESSAGE_CHARS)


async def _send_text(channel: str, text: str) -> None:
    """The single path text takes to Slack -- a reply and an unprompted
    notify() both come through here, so neither can forget to split."""
    for part in _chunks(text):
        await _api("chat.postMessage", json={"channel": channel, "text": part})


def _wren_user_id(slack_id: str) -> int | None:
    """Who is talking. The owner is the env pair (SLACK_OWNER_ID ->
    WREN_OWNER_ID); everyone else is their contacts row; a stranger is
    None and is dropped by the caller. Translation is authn, the surface's
    job (telegram_plugin does the same)."""
    if config.SLACK_OWNER_ID and slack_id == config.SLACK_OWNER_ID:
        return config.WHITELIST["owner"]
    return contacts.wren_id("slack", slack_id)


def _slack_user_id(user_id: int) -> str | None:
    """The inverse, for notify(): the Slack member to DM for a Wren id."""
    if config.SLACK_OWNER_ID and user_id == config.WHITELIST["owner"]:
        return config.SLACK_OWNER_ID
    return contacts.surface_id("slack", user_id)


class SlackChannel:
    """Channel implementation backed by one Slack DM and the message that
    opened the turn (its ts is what a reaction attaches to)."""

    NAME = "slack"

    def __init__(self, channel_id: str, ts: str):
        self._channel = channel_id
        self._ts = ts

    async def send(self, text: str) -> None:
        await _send_text(self._channel, text)

    async def send_file(self, data: bytes, filename: str) -> None:
        # files.upload is gone (2025); the replacement is three calls: ask
        # for a URL, put the bytes there, then tell Slack where to show it.
        got = await _api("files.getUploadURLExternal", json={"filename": filename, "length": len(data)})
        await _upload_bytes(got["upload_url"], data)
        await _api("files.completeUploadExternal",
                   json={"files": [{"id": got["file_id"], "title": filename}], "channel_id": self._channel})

    async def send_card(self, kind: str, data: dict, text: str,
                        *, intent: str = "", params: dict | None = None) -> None:
        # Block Kit would mean an interactivity route and a per-surface
        # action model. Prose is what send_card's signature exists for.
        await self.send(text)

    async def ack(self, state: str) -> None:
        # Reactions, as Discord draws them. Failures are logged, never raised:
        # core calls ack("seen") outside its try block.
        name = {"seen": "eyes", "done": "white_check_mark", "error": "x"}.get(state)
        if not name:
            return
        try:
            await _api("reactions.add", json={"channel": self._channel, "timestamp": self._ts, "name": name})
        except Exception as e:
            logging.warning(f"slack reactions.add failed: {_scrub(str(e))}")


def _incoming(event: dict) -> tuple[str, str, list[dict]] | None:
    """(slack_user_id, text, wanted_files) for an event Wren should act on,
    else None. DMs only (channel_type im); nothing from a bot, including
    Wren's own echoes; edits and deletions arrive as subtypes and are not
    new messages. A file_share is the one subtype acted on; its text is
    the caption. `wanted_files` are descriptors -- downloading is async and
    happens in the loop, so this stays a pure function."""
    if not isinstance(event, dict) or event.get("type") != "message":
        return None
    if event.get("channel_type") != "im" or event.get("bot_id"):
        return None
    if event.get("subtype") not in (None, "file_share"):
        return None
    user = event.get("user")
    if not isinstance(user, str) or not user:
        return None
    wanted = [
        {"url": f.get("url_private_download"), "filename": f.get("name") or "file",
         "mime": f.get("mimetype"), "size": f.get("size")}
        for f in (event.get("files") or [])
        if isinstance(f, dict) and f.get("mimetype") in filetypes.ACCEPTED and f.get("url_private_download")
    ]
    text = event.get("text") or ""
    if not text and not wanted:
        return None
    return user, text, wanted


async def notify(user_id: int, text: str) -> bool:
    target = _slack_user_id(user_id)
    if target is None:
        # Not on Slack at all: permanent, the router tries the next surface
        # (or, with NOTIFY_VIA=slack, the caller hears "unreachable").
        return False
    try:
        # chat.postMessage to a member id opens (or reuses) the DM; im:write
        # is the scope that allows it.
        await _send_text(target, text)
        return True
    except SlackError as e:
        # router.notify()'s contract: False is PERMANENT and the caller
        # consumes the message. Only errors that will never succeed may say
        # so; ratelimited, invalid_auth and the rest raise so the caller
        # retries on its next poll.
        if e.error in _PERMANENT:
            logging.warning(f"slack cannot reach user {user_id}: {e}")
            return False
        raise
