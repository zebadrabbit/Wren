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
_created: dict[str, str] = {}          # matrix id -> the room Wren made for them this run


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
                # A reverse proxy's 502 page is HTML: still a MatrixError
                # with its status, not a bare transport error.
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = {}
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


def _find_dm(matrix_id: str) -> str | None:
    """The joined room whose members are exactly the two of us and that Wren
    can read. An encrypted one does not count: replies sent there would be
    plaintext in a room whose messages Wren cannot read back."""
    for room_id, members in _rooms.items():
        if members == {_me, matrix_id} and room_id not in _encrypted:
            return room_id
    return None


async def _dm_room(matrix_id: str) -> str:
    """The direct room with this person (see _find_dm), else a new one. Wren
    creates it unencrypted and invites them, so nobody has to find the
    encryption toggle; a trusted_private_chat shares history, so a message
    sent before they accept is waiting when they do."""
    if not _me:
        # Before whoami has answered, {"", them} would be cached as their
        # room and every later message sent into a fresh one. Raised, not
        # False: notify()'s caller retries, and whoami will have answered.
        raise RuntimeError("matrix: not signed in yet")
    room_id = _find_dm(matrix_id)
    if room_id is not None:
        return room_id
    made = _created.get(matrix_id)
    if made is not None and _rooms.get(made) == {_me, matrix_id}:
        # The room Wren made for them this run came back encrypted, so the
        # homeserver encrypts every new room (see README). Another would be
        # too, and one per sync would bury them in invites; what Wren sends
        # there still reaches them.
        return made
    body = await _api("POST", f"{V3}/createRoom",
                      json={"is_direct": True, "invite": [matrix_id], "preset": "trusted_private_chat"})
    room_id = body["room_id"]
    _rooms[room_id] = {_me, matrix_id}
    _created[matrix_id] = room_id
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


# What /sync should carry: timeline and member state. Members are NOT lazily
# loaded: the "exactly two members" DM gate and _dm_room's identity match
# need the full list, and lazy loading sends only recent speakers plus Wren,
# so a group room with a silent third member would look like a DM.
_SYNC_FILTER = _json.dumps({
    "room": {"timeline": {"limit": 50}, "ephemeral": {"types": []}, "account_data": {"types": []}},
    "presence": {"types": []}, "account_data": {"types": []},
})
# A restart replays whatever the initial sync carries and anything missed
# since the last token (process-local, as telegram_plugin's offset is).
# Acting on a message hours late is worse than not acting.
_STALE_SECONDS = 30 * 60
# Rooms Wren cannot read: m.room.encryption in their state, or m.room.encrypted
# events in their timeline. Never a DM room (see _find_dm). Encryption cannot
# be turned off once on, so a room never leaves this set.
_encrypted: set[str] = set()
_ENCRYPTION_TYPES = ("m.room.encryption", "m.room.encrypted")
_warned: set[str] = set()             # encrypted rooms already logged and answered with an invite


def _is_stale(event: dict) -> bool:
    ts = event.get("origin_server_ts")
    return isinstance(ts, (int, float)) and time.time() - ts / 1000 > _STALE_SECONDS


def _learn_rooms(rooms: dict) -> None:
    """Fold a sync's membership and encryption into _rooms and _encrypted.
    State events carry what was already true; timeline events carry what
    changed since. Runs on the initial sync too, so an encrypted room is
    known before start() looks for the owner's room."""
    for room_id, room in (rooms.get("join") or {}).items():
        members = _rooms.setdefault(room_id, set())
        events = list((room.get("state") or {}).get("events") or []) + \
                 list((room.get("timeline") or {}).get("events") or [])
        for ev in events:
            if ev.get("type") in _ENCRYPTION_TYPES:
                _encrypted.add(room_id)
                continue
            if ev.get("type") != "m.room.member":
                continue
            who = ev.get("state_key")
            if not who:
                continue
            if (ev.get("content") or {}).get("membership") in ("join", "invite"):
                members.add(who)
            else:
                members.discard(who)
    for room_id in (rooms.get("leave") or {}):
        _rooms.pop(room_id, None)


def _incoming(event: dict) -> tuple[str, str, list[dict]] | None:
    """(sender, text, wanted_files) for a timeline event Wren should act on,
    else None: m.room.message only, never our own echo; m.text is text,
    m.image/m.file of an accepted type is a file whose caption is `body`
    when the sender gave one (then `filename` holds the name)."""
    if not isinstance(event, dict) or event.get("type") != "m.room.message":
        return None
    sender = event.get("sender")
    if not isinstance(sender, str) or sender == _me:
        return None
    content = event.get("content") or {}
    if (content.get("m.relates_to") or {}).get("rel_type") == "m.replace":
        return None                              # an edit, not a new command
    kind = content.get("msgtype")
    body = content.get("body") or ""
    if kind == "m.text":
        return (sender, body, []) if body else None
    if kind in ("m.image", "m.file"):
        info = content.get("info") or {}
        if info.get("mimetype") not in filetypes.ACCEPTED or not content.get("url"):
            return None
        filename = content.get("filename") or body or "file"
        text = body if content.get("filename") and body != content.get("filename") else ""
        return sender, text, [{"url": content["url"], "filename": filename,
                               "mime": info["mimetype"], "size": info.get("size")}]
    return None


def _known(matrix_id: str) -> bool:
    return _wren_user_id(matrix_id) is not None


async def _handle(room_id: str, event: dict) -> None:
    parsed = _incoming(event)
    if parsed is None:
        return
    sender, text, wanted = parsed
    user_id = _wren_user_id(sender)
    if user_id is None:
        return
    if _is_stale(event):
        await _send_text(room_id, "I was offline when some of your messages arrived, so I "
                                  "didn't act on them. Send again anything that still matters.")
        return
    files: list[Inbound] = []
    for want in wanted:
        size = want.get("size")
        if size is not None and size > filetypes.MAX_BYTES:
            await _send_text(room_id, "That's too big, 10 MB max.")
            continue
        try:
            data = await _download(want["url"])
        except Exception as e:
            logging.warning(f"matrix: media download failed: {_scrub(str(e))}")
            await _send_text(room_id, "Couldn't fetch that file, try again.")
            continue
        files.append(Inbound(filename=want["filename"], mime=want["mime"], data=data))
    if not text and not files:
        return
    if wanted and not files:
        return
    await core.handle_message(user_id, text, MatrixChannel(room_id), files=files)


async def _redirect(room_id: str, events: list) -> None:
    """Someone Wren knows wrote in an encrypted direct room, which Wren
    cannot read: say so in the log, once, and invite them to one it can
    (_dm_room skips encrypted rooms, so this opens a new one unless it
    exists already). A failed invite is tried again on their next message."""
    if room_id in _warned:
        return
    writer = next((ev.get("sender") for ev in events
                   if ev.get("type") in ("m.room.encrypted", "m.room.message")
                   and ev.get("sender") not in (None, _me) and _known(ev.get("sender"))), None)
    if writer is None:
        return
    _warned.add(room_id)
    try:
        readable = await _dm_room(writer)
    except Exception as e:
        _warned.discard(room_id)
        logging.warning(f"matrix: room {room_id} is encrypted and Wren cannot read it; could not "
                        f"open one it can for {writer}: {_scrub(str(e))}")
        return
    if readable == room_id:
        logging.warning(f"matrix: the homeserver encrypted room {room_id}, which Wren made, so Wren "
                        "cannot read replies there; set encryption_enabled_by_default_for_room_type "
                        "to off (see README)")
    else:
        logging.warning(f"matrix: room {room_id} is encrypted and Wren cannot read it; "
                        f"invited {writer} to {readable} (see README)")


async def _sync_once(since: str | None) -> str:
    """One /sync round trip. Returns the token for the next call. With no
    token this is the initial sync: it carries history, so it only teaches
    us the rooms and is otherwise not acted on."""
    params = {"timeout": _LONG_POLL_MS, "filter": _SYNC_FILTER}
    if since:
        params["since"] = since
    body = await _api("GET", f"{V3}/sync", params=params)
    rooms = body.get("rooms") or {}
    _learn_rooms(rooms)
    # Invites are joined on the initial sync too: the server does not repeat
    # them, and joining is idempotent.
    for room_id, room in (rooms.get("invite") or {}).items():
        inviter = next((ev.get("sender") for ev in ((room.get("invite_state") or {}).get("events") or [])
                        if ev.get("type") == "m.room.member" and ev.get("state_key") == _me), None)
        if inviter and _known(inviter):
            try:
                await _api("POST", f"{V3}/rooms/{room_id}/join", json={})
            except Exception as e:
                logging.warning(f"matrix: join {room_id} failed: {_scrub(str(e))}")
    if since is None:
        return body["next_batch"]
    for room_id, room in (rooms.get("join") or {}).items():
        events = (room.get("timeline") or {}).get("events") or []
        if len(_rooms.get(room_id, ())) != 2:
            continue                             # a group room, or membership unknown yet
        if room_id in _encrypted:
            await _redirect(room_id, events)
            continue
        for ev in events:
            try:
                await _handle(room_id, ev)
            except Exception as e:
                # Never the body: direct messages are as private as reminders.
                logging.error(f"matrix: handling {ev.get('event_id')} from {ev.get('sender')} "
                              f"failed: {_scrub(str(e))}")
    return body["next_batch"]


async def start() -> None:
    """Runs for the lifetime of the process (see telegram_plugin.start)."""
    global _me
    missing = [k for k in ("MATRIX_HOMESERVER", "MATRIX_TOKEN") if not getattr(config, k)]
    if missing:
        raise RuntimeError(
            f"COMMUNICATION_PLUGINS includes 'matrix' but {' and '.join(missing)} not set. "
            "Set them, or drop 'matrix' from COMMUNICATION_PLUGINS.")
    router.register("matrix", sys.modules[__name__])
    since: str | None = None
    backoff = _RETRY_SECONDS
    while True:
        try:
            if not _me:
                _me = (await _api("GET", f"{V3}/account/whoami"))["user_id"]
                logging.info(f"Matrix plugin online as {_me}")
            if config.MATRIX_OWNER_ID and _find_dm(config.MATRIX_OWNER_ID) is None:
                # The owner's door is opened from this side, unencrypted, so
                # their first step is accepting an invite rather than finding
                # the encryption toggle. Runs once the initial sync has shown
                # whether such a room already exists (since is None before);
                # an encrypted one does not count.
                if since is not None:
                    try:
                        await _dm_room(config.MATRIX_OWNER_ID)
                    except Exception as e:
                        # A bad MATRIX_OWNER_ID must not block every contact's DMs.
                        logging.warning(f"matrix: could not open the owner's room: {_scrub(str(e))}")
            since = await _sync_once(since)
            backoff = _RETRY_SECONDS
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.warning(f"matrix sync failed ({_scrub(str(e))}); retrying in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_RETRY_SECONDS)
