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
import collections
import html
import json as _json
import logging
import re
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
# Slack documents these as form-encoded only; a JSON body is rejected. For
# completeUploadExternal its own SDKs send `files` as a JSON string inside
# the form, so send_file does the same.
_FORM_ONLY = frozenset({"files.getUploadURLExternal", "files.completeUploadExternal"})
# Slack shows up to 4000 characters of a message and truncates the rest with
# a "show more" that many clients never render; past 40000 it is an error.
_MAX_MESSAGE_CHARS = 4000
# chat.postMessage errors that will never succeed for this person; everything
# else (ratelimited, invalid_auth, fatal_error) is retried by the caller.
# account_inactive is not here: it describes the bot's own account or
# workspace, which an admin can bring back, and a reminder consumed over it
# is gone for good.
_PERMANENT = frozenset({"channel_not_found", "user_not_found", "cannot_dm_bot",
                        "user_disabled"})


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
            body_kw = {"data": json or {}} if method in _FORM_ONLY else {"json": json or {}}
            async with session.post(url, headers=headers, **body_kw) as resp:
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


def _escape(text: str) -> str:
    """Slack's mrkdwn reads <...> as a link or mention and & as the start
    of an entity, so a literal one in a reply must be escaped (& first, or
    the other two would be escaped twice)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _send_text(channel: str, text: str) -> None:
    """The single path text takes to Slack -- a reply and an unprompted
    notify() both come through here, so neither can forget to split or
    escape. Split first: escaping can lengthen a part, and a cut must not
    land inside an entity."""
    for part in _chunks(text):
        await _api("chat.postMessage", json={"channel": channel, "text": _escape(part)})


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
                   json={"files": _json.dumps([{"id": got["file_id"], "title": filename}]),
                         "channel_id": self._channel})

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


# Slack's markup in incoming text: a link or mention arrives wrapped in angle
# brackets (<https://x|label>, <@U123>, <#C123|general>, <!here>), and a
# literal &, < or > the person typed arrives as &amp;, &lt;, &gt;. So any
# bare < in the text is markup.
_MARKUP = re.compile(r"<([^<>|]+)(?:\|([^<>]*))?>")


def _unwrap(m: re.Match) -> str:
    target, label = m.group(1), m.group(2)
    if target.startswith(("http://", "https://")):
        return target                            # the link itself, not its label
    if target.startswith("mailto:"):
        return target.removeprefix("mailto:")    # the address as it was typed
    if target[0] in "@#":
        return target[0] + (label or target[1:])
    if target[0] == "!":
        # <!here>, <!subteam^S1|@team>, <!date^...|fallback>
        return label or "@" + target[1:].split("^")[0]
    return m.group(0)


def _plain(text: str) -> str:
    """What the person typed, from what Slack sent. Translation, the same
    as turning a member id into a Wren id, so it is the door's job: without
    it "salt & pepper" reaches the brain as "salt &amp; pepper" and a link
    to save as "<https://example.com|this>". Markup first, while every
    remaining < is still escaped; entities after, so a link's own &amp;
    comes out too."""
    return html.unescape(_MARKUP.sub(_unwrap, text))


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
    text = _plain(event.get("text") or "")
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


# Socket Mode redelivers an envelope it did not see acked, and after a
# reconnect it can hand over what it held while Wren was away. Acting on a
# message hours late is worse than not acting (see telegram_plugin), so
# anything older than this gets the offline notice instead.
_STALE_SECONDS = 30 * 60


def _is_stale(event: dict) -> bool:
    try:
        ts = float(event.get("ts") or 0)
    except (TypeError, ValueError):
        return False
    return ts > 0 and time.time() - ts > _STALE_SECONDS


async def _handle_event(event: dict) -> None:
    """One acted-on event, start to finish. Raises nothing the loop cannot
    log; the envelope was acked before this ran."""
    parsed = _incoming(event)
    if parsed is None:
        return
    slack_user, text, wanted = parsed
    user_id = _wren_user_id(slack_user)
    if user_id is None:
        return                                   # a stranger: no answer, as on Telegram
    channel_id = event.get("channel") or slack_user
    if _is_stale(event):
        await _send_text(channel_id, "I was offline when some of your messages arrived, so I "
                                     "didn't act on them. Send again anything that still matters.")
        return
    files: list[Inbound] = []
    for want in wanted:
        size = want.get("size")
        if size is not None and size > filetypes.MAX_BYTES:
            await _send_text(channel_id, "That's too big, 10 MB max.")
            continue
        try:
            data = await _download(want["url"])
        except Exception as e:
            logging.warning(f"slack: file download failed: {_scrub(str(e))}")
            await _send_text(channel_id, "Couldn't fetch that file, try again.")
            continue
        files.append(Inbound(filename=want["filename"], mime=want["mime"], data=data))
    if not text and not files:
        return
    if wanted and not files:
        return                                   # every file refused; the refusal already replied
    await core.handle_message(user_id, text, SlackChannel(channel_id, event.get("ts", "")), files=files)


# Event ids already queued. Socket Mode is at-least-once: an envelope acked
# late, or held across a reconnect, comes again under a new envelope id but
# the same event id. Module-level so it spans reconnects; bounded because
# only the last few minutes can repeat.
_SEEN_MAX = 256
_seen: collections.OrderedDict[str, None] = collections.OrderedDict()


def _first_sighting(event_id) -> bool:
    if not event_id:
        return True                              # nothing to tell a repeat by
    if event_id in _seen:
        return False
    _seen[event_id] = None
    if len(_seen) > _SEEN_MAX:
        _seen.popitem(last=False)
    return True


async def _work(queue: asyncio.Queue) -> None:
    """The one consumer of a connection's events. One, so replies go out in
    the order the messages came in (see telegram_plugin for why not a task
    per message); None on the queue ends it."""
    while True:
        item = await queue.get()
        if item is None:
            return
        envelope_id, event = item
        try:
            await _handle_event(event)
        except Exception as e:
            # Never the body: DMs are as private as reminders.
            logging.error(f"slack: handling event {envelope_id} from "
                          f"{event.get('user')} failed: {_scrub(str(e))}")


async def _read(url: str, queue: asyncio.Queue) -> None:
    """Serve the websocket until Slack says disconnect (it does so routinely
    to rotate hosts) or the socket drops. Every envelope is acked the moment
    it arrives and its event queued: Slack retries an envelope it has not
    seen acked within about 3 s, and the brain can take longer than that on
    the message before -- acking behind it would answer the same message
    twice."""
    timeout = aiohttp.ClientTimeout(total=None)   # the socket lives until Slack closes it
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.ws_connect(url, heartbeat=30) as ws:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.ERROR:
                    logging.warning(f"slack websocket error ({_scrub(str(msg.data))}); reconnecting")
                    return
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                        return
                    continue
                frame = msg.json()
                kind = frame.get("type")
                if kind == "disconnect":
                    return
                envelope_id = frame.get("envelope_id")
                if envelope_id:
                    await ws.send_json({"envelope_id": envelope_id})
                if kind != "events_api":
                    continue                     # hello, slash_commands, interactive: nothing to do
                payload = frame.get("payload") or {}
                if not _first_sighting(payload.get("event_id")):
                    continue                     # a redelivery: acked again, handled once
                queue.put_nowait((envelope_id, payload.get("event") or {}))


async def _run_once() -> None:
    """One websocket connection and the worker behind it; returns when the
    connection ends so start() opens a fresh one."""
    body = await _api("apps.connections.open", token=config.SLACK_APP_TOKEN)
    queue: asyncio.Queue = asyncio.Queue()
    worker = asyncio.create_task(_work(queue))
    try:
        await _read(body["url"], queue)
    except asyncio.CancelledError:
        worker.cancel()                          # shutting down: stop mid-message
        raise
    finally:
        # Otherwise the worker finishes the queue first. Everything on it was
        # acked, so Slack will not send it again; cancelling here would drop
        # it, and Slack rotates connections every few hours. The socket is
        # closed by now, so new messages wait for the next connection.
        queue.put_nowait(None)
        await asyncio.gather(worker, return_exceptions=True)


async def start() -> None:
    """Runs for the lifetime of the process. A communication plugin's start()
    must not return while the plugin is live -- run.py gathers these, so
    returning early would shut Wren down."""
    missing = [k for k in ("SLACK_APP_TOKEN", "SLACK_BOT_TOKEN") if not getattr(config, k)]
    if missing:
        raise RuntimeError(
            f"COMMUNICATION_PLUGINS includes 'slack' but {' and '.join(missing)} not set. "
            "Set them, or drop 'slack' from COMMUNICATION_PLUGINS.")
    router.register("slack", sys.modules[__name__])
    logging.info("Slack plugin connecting (Socket Mode)")
    backoff = _RETRY_SECONDS
    while True:
        try:
            await _run_once()
            backoff = _RETRY_SECONDS             # a clean disconnect: reconnect at once
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Everything else stops here: run.py gathers start(), and a DNS
            # blip escaping would take the other doors down with it.
            logging.warning(f"slack connection failed ({_scrub(str(e))}); retrying in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_RETRY_SECONDS)
