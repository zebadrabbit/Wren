"""Matrix communication plugin.

Role: chat (input + output), like telegram_plugin.py.

The Client-Server API is plain HTTPS + JSON, so this speaks it directly with
aiohttp rather than pulling in matrix-nio: /sync as a long poll, send, join,
createRoom, media upload and download. Direct rooms only, unencrypted (olm is
the ceiling; the README says to let Wren open the room, which it does
unencrypted).

Enable by adding "matrix" to COMMUNICATION_PLUGINS in .env with
MATRIX_HOMESERVER, MATRIX_TOKEN (the bot account's access token) and
MATRIX_OWNER_ID (@you:home.net).
"""

import asyncio
import json as _json
import logging
import sys
import time
import uuid

import aiohttp

from .. import config
from .. import contacts
from .. import core
from .. import filetypes
from .. import router
from ..channel import Inbound
from . import chunking

ROLE = "chat"
PLUGIN_NAME = "Matrix"
CAN_NOTIFY = True

V3 = "/_matrix/client/v3"

_LONG_POLL_MS = 30000
_CLIENT_TIMEOUT_SECONDS = _LONG_POLL_MS // 1000 + 15
_RETRY_SECONDS = 5
_MAX_RETRY_SECONDS = 300
# An event is capped at 64 KiB including its envelope; half that in text
# leaves room for the JSON around it and multi-byte characters.
_MAX_MESSAGE_CHARS = 32000

_me: str = ""                          # the bot's own id, from whoami at start
_rooms: dict[str, set[str]] = {}       # room id -> joined members, from sync


def is_active() -> bool:
    return bool(config.MATRIX_HOMESERVER and config.MATRIX_TOKEN)


def inactive_reason() -> str:
    missing = [k for k in ("MATRIX_HOMESERVER", "MATRIX_TOKEN") if not getattr(config, k)]
    return (f"{' and '.join(missing)} not set. Add them and put 'matrix' in "
            "COMMUNICATION_PLUGINS, then restart.")


class MatrixError(RuntimeError):
    """A non-2xx from the homeserver. `errcode` is Matrix's (M_FORBIDDEN,
    M_LIMIT_EXCEEDED, ...) and `status` the HTTP status; together they tell a
    permanent failure from a transient one -- see notify()."""

    def __init__(self, errcode: str, status: int):
        super().__init__(f"Matrix {status} {errcode}")
        self.errcode = errcode
        self.status = status


def _scrub(text: str) -> str:
    return text.replace(config.MATRIX_TOKEN, "<token>") if config.MATRIX_TOKEN else text


async def _api(method: str, path: str, *, json: dict | None = None, params: dict | None = None,
               data: bytes | None = None, content_type: str | None = None) -> dict:
    """One request to the homeserver, the token in a header. Returns the
    JSON body; raises MatrixError on a non-2xx."""
    url = f"{config.MATRIX_HOMESERVER}{path}"
    headers = {"Authorization": f"Bearer {config.MATRIX_TOKEN}"}
    if content_type:
        headers["Content-Type"] = content_type
    timeout = aiohttp.ClientTimeout(total=_CLIENT_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, url, json=json, params=params, data=data, headers=headers) as resp:
                body = await resp.json()
                if resp.status >= 300:
                    raise MatrixError(str((body or {}).get("errcode", "M_UNKNOWN")), resp.status)
                return body or {}
    except aiohttp.ClientError as e:
        raise aiohttp.ClientError(f"{type(e).__name__}: {_scrub(str(e))}") from None


async def _download(mxc: str) -> bytes:
    """GET media by its mxc:// uri through the authenticated endpoint
    (Matrix 1.11; Synapse 1.111+). Older homeservers answer 404 here and are
    out of scope in this version."""
    server, _, media_id = mxc.removeprefix("mxc://").partition("/")
    url = f"{config.MATRIX_HOMESERVER}/_matrix/client/v1/media/download/{server}/{media_id}"
    headers = {"Authorization": f"Bearer {config.MATRIX_TOKEN}"}
    timeout = aiohttp.ClientTimeout(total=_CLIENT_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                resp.raise_for_status()
                return await resp.read()
    except aiohttp.ClientError as e:
        raise aiohttp.ClientError(f"{type(e).__name__}: {_scrub(str(e))}") from None


def _wren_user_id(matrix_id: str) -> int | None:
    """Who is talking: the env pair for the owner, the contacts row for
    everyone else, None for a stranger (dropped by the caller)."""
    if config.MATRIX_OWNER_ID and matrix_id == config.MATRIX_OWNER_ID:
        return config.WHITELIST["owner"]
    return contacts.wren_id("matrix", matrix_id)


def _matrix_user_id(user_id: int) -> str | None:
    if config.MATRIX_OWNER_ID and user_id == config.WHITELIST["owner"]:
        return config.MATRIX_OWNER_ID
    return contacts.surface_id("matrix", user_id)


async def _dm_room(matrix_id: str) -> str:
    """The direct room with this person: the joined room whose members are
    exactly the two of us, else a new one. Wren creates it unencrypted and
    invites them, so nobody has to find the encryption toggle; a
    trusted_private_chat shares history, so a message sent before they
    accept is waiting when they do."""
    for room_id, members in _rooms.items():
        if members == {_me, matrix_id}:
            return room_id
    body = await _api("POST", f"{V3}/createRoom",
                      json={"is_direct": True, "invite": [matrix_id], "preset": "trusted_private_chat"})
    room_id = body["room_id"]
    _rooms[room_id] = {_me, matrix_id}
    return room_id


async def _send_text(room_id: str, text: str) -> None:
    """The single path text takes into a room; replies and notify() both
    come through here so neither can forget to split."""
    for part in chunking.chunks(text, _MAX_MESSAGE_CHARS):
        await _api("PUT", f"{V3}/rooms/{room_id}/send/m.room.message/{uuid.uuid4()}",
                   json={"msgtype": "m.text", "body": part})


class MatrixChannel:
    """Channel implementation backed by one direct room."""

    NAME = "matrix"

    def __init__(self, room_id: str):
        self._room = room_id

    async def send(self, text: str) -> None:
        await _send_text(self._room, text)

    async def send_file(self, data: bytes, filename: str) -> None:
        mime = filetypes.sniff(data) or "application/octet-stream"
        up = await _api("POST", "/_matrix/media/v3/upload", params={"filename": filename},
                        data=data, content_type=mime)
        kind = "m.image" if mime in filetypes.IMAGE_MIMES else "m.file"
        await _api("PUT", f"{V3}/rooms/{self._room}/send/m.room.message/{uuid.uuid4()}",
                   json={"msgtype": kind, "body": filename, "url": up["content_uri"],
                         "info": {"mimetype": mime, "size": len(data)}})

    async def send_card(self, kind: str, data: dict, text: str,
                        *, intent: str = "", params: dict | None = None) -> None:
        await self.send(text)

    async def ack(self, state: str) -> None:
        # The typing indicator is the native "I heard you"; done/error no-op
        # because the reply is the acknowledgement. Never raises: core calls
        # ack("seen") outside its try block.
        if state != "seen":
            return
        try:
            await _api("PUT", f"{V3}/rooms/{self._room}/typing/{_me}", json={"typing": True, "timeout": 30000})
        except Exception as e:
            logging.warning(f"matrix typing failed: {_scrub(str(e))}")


async def notify(user_id: int, text: str) -> bool:
    target = _matrix_user_id(user_id)
    if target is None:
        return False                             # not on Matrix: permanent
    try:
        await _send_text(await _dm_room(target), text)
        return True
    except MatrixError as e:
        # router.notify()'s contract: False is PERMANENT. Forbidden means
        # Wren is no longer in the room (they left or kicked it); rate limits,
        # a bad token and 5xx raise so the caller retries next poll.
        if e.errcode == "M_FORBIDDEN":
            logging.warning(f"matrix cannot reach user {user_id}: {e}")
            return False
        raise
