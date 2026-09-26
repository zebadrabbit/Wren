"""Telegram communication plugin.

Role: chat (input + output) — like discord_plugin.py, not gmail_plugin.py.

The Bot API is plain HTTPS + JSON, so this speaks it directly with aiohttp
(already a dependency, for the HTTP plugin) rather than pulling in a Telegram
library. Four endpoints are all Wren needs: getUpdates, sendMessage,
sendDocument, sendChatAction.

Enable by adding "telegram" to COMMUNICATION_PLUGINS in .env, with a
TELEGRAM_TOKEN from @BotFather.
"""

import asyncio
import logging
import sys

import aiohttp

from .. import config
from .. import core
from .. import router
from . import chunking

ROLE = "chat"          # "chat" | "input" | "output"
PLUGIN_NAME = "Telegram"
CAN_NOTIFY = True

API_ROOT = "https://api.telegram.org"

# How long Telegram holds a getUpdates request open when there is nothing new.
# This is what makes the loop a long poll rather than a hot loop: one request
# per 30s while idle, and a reply within milliseconds when a message lands.
_LONG_POLL_SECONDS = 30
# The client-side deadline MUST be longer than the server-side long poll, or
# every quiet poll would die on our own timeout before Telegram ever answers —
# an error loop that still technically "works" but logs a failure every 30s.
_CLIENT_TIMEOUT_SECONDS = _LONG_POLL_SECONDS + 15
_RETRY_SECONDS = 5
_MAX_RETRY_SECONDS = 300

# Telegram rejects a longer sendMessage with a 400. notify() reads 400 as
# permanent (see there), so an over-long reminder would be marked delivered and
# destroyed — hence _chunks() rather than trusting callers to be brief.
_MAX_MESSAGE_CHARS = 4096


def is_active() -> bool:
    return bool(config.TELEGRAM_TOKEN)


def inactive_reason() -> str:
    return ("TELEGRAM_TOKEN is not set. Add it and put 'telegram' in "
            "COMMUNICATION_PLUGINS, then restart.")


class TelegramError(RuntimeError):
    """The Bot API answered with ok=false. `code` is Telegram's error_code,
    which is what tells a permanent failure (403, user blocked the bot) from a
    transient one (429, 5xx) — see notify()."""

    def __init__(self, method: str, code: int | None, description: str):
        super().__init__(f"Telegram {method} failed ({code}): {description}")
        self.code = code


async def _api(method: str, *, data=None):
    """POST to the Bot API and return its `result`.

    `data` is either a plain dict (form-encoded) or an aiohttp.FormData
    (multipart, for uploads) — Telegram accepts both, so one code path covers
    sending text and sending files.
    """
    url = f"{API_ROOT}/bot{config.TELEGRAM_TOKEN}/{method}"
    client_timeout = aiohttp.ClientTimeout(total=_CLIENT_TIMEOUT_SECONDS)
    # ponytail: a fresh ClientSession per request. That is one TLS handshake
    # per 30s poll, which is nothing, and it dodges the whole lifecycle problem
    # a module-level session brings (must be created on the running loop, must
    # be closed on cancellation). Hoist it only if the request rate ever climbs.
    try:
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.post(url, data=data) as resp:
                body = await resp.json()
    except aiohttp.ClientError as e:
        # `url` above is the ONLY place the bot token is ever interpolated into
        # a string, so it is the only place a token can get into an exception —
        # and several aiohttp errors put the request URL in str(): the
        # ClientResponseError family (an HTML 502 from Telegram's edge makes
        # resp.json() raise ContentTypeError) and InvalidURL. Every caller
        # eventually logs the exception, and one of them is core's own handler,
        # which this plugin cannot reach. So scrub here, at the source, or a
        # 20-minute Telegram wobble writes the token to journald once per
        # retry. Still a bare ClientError, so notify() keeps reading it as
        # transient and raises rather than destroying the message.
        detail = str(e)
        if config.TELEGRAM_TOKEN:
            detail = detail.replace(config.TELEGRAM_TOKEN, "<token>")
        raise aiohttp.ClientError(f"{type(e).__name__}: {detail}") from None
    if not body.get("ok"):
        # `url` is deliberately not in the message: it embeds the bot token,
        # and this text goes to the system journal.
        raise TelegramError(method, body.get("error_code"), body.get("description", ""))
    return body.get("result")


def _chunks(text: str) -> list[str]:
    """`text` split into pieces Telegram will accept, longest-first.

    The algorithm (why it cuts where it does) lives in chunking.chunks, shared
    with every other plugin that has a message-length ceiling; only the
    ceiling itself — Telegram's — is this module's.
    """
    return chunking.chunks(text, _MAX_MESSAGE_CHARS)


async def _send_text(chat_id: int, text: str) -> None:
    """The single path text takes to a Telegram chat — both the reply to a
    message and an unprompted notify() come through here, so neither can be
    the one that forgets to split."""
    for part in _chunks(text):
        await _api("sendMessage", data={"chat_id": chat_id, "text": part})


def _wren_user_id(chat_id: int) -> int:
    """The Telegram id of whoever is talking -> the Wren user id they are.

    Ids are per-surface, and core keys the authz gate plus every note, reminder
    and pin off exactly one id per person. Left untranslated, the owner
    arriving from Telegram is an unknown id that core drops in silence; and
    whitelisting that id separately would make Telegram a second, empty Wren
    with its own notes. Translating is authn, which core.handle_message's
    docstring puts in the surface — so it belongs here and not in core.

    ponytail: the owner only. A second person on Telegram needs a real
    per-surface id column in contacts; add it when there is a second person.
    """
    if config.TELEGRAM_OWNER_ID and chat_id == config.TELEGRAM_OWNER_ID:
        return config.WHITELIST["owner"]
    return chat_id


def _telegram_chat_id(user_id: int) -> int:
    """The inverse, for notify(): a reminder filed from any surface carries the
    Wren user id, and sending that number to Telegram is a 400 'chat not
    found'."""
    if config.TELEGRAM_OWNER_ID and user_id == config.WHITELIST["owner"]:
        return config.TELEGRAM_OWNER_ID
    return user_id


class TelegramChannel:
    """Channel implementation backed by one Telegram chat."""

    def __init__(self, chat_id: int):
        self._chat_id = chat_id

    async def send(self, text: str) -> None:
        await _send_text(self._chat_id, text)

    async def send_file(self, data: bytes, filename: str) -> None:
        form = aiohttp.FormData()
        form.add_field("chat_id", str(self._chat_id))
        form.add_field("document", data, filename=filename,
                       content_type="application/octet-stream")
        await _api("sendDocument", data=form)

    async def send_card(self, kind: str, data: dict, text: str,
                        *, intent: str = "", params: dict | None = None) -> None:
        # Inline keyboards would mean a callback route and a per-surface
        # interaction model. Prose is what send_card's signature exists for.
        await self.send(text)

    async def history(self, limit: int = 10) -> list[dict] | None:
        # ponytail: None, same as CollectingChannel — but here it is a protocol
        # limit, not a shortcut. A bot cannot read a chat's backlog: getUpdates
        # only ever hands over messages that arrived after it started, and the
        # Bot API has no "fetch the last N messages" call at all. The only way
        # to have history on Telegram is to keep our own table of what went
        # past, which is the wren/conversations.py path, not this one.
        return None

    async def ack(self, state: str) -> None:
        # Discord draws acks as reactions. Telegram's bot reactions
        # (setMessageReaction) would mean threading the message id through the
        # Channel and littering the chat with emoji, so "seen" gets the native
        # equivalent instead — the typing indicator, which is exactly the
        # "I heard you, the LLM is thinking" signal — and done/error no-op
        # because the reply itself is the acknowledgement.
        if state != "seen":
            return
        try:
            await _api("sendChatAction",
                       data={"chat_id": self._chat_id, "action": "typing"})
        except Exception as e:
            # core calls ack("seen") OUTSIDE its try block, so raising here
            # would kill the message before the brain ever saw it. A missing
            # typing bubble is not worth a dropped message.
            logging.warning(f"telegram sendChatAction failed: {e}")


def _incoming(update: dict) -> tuple[int, str] | None:
    """(user_id, text) for an update Wren should act on, else None.

    Everything else is skipped in one place: edited messages and channel posts
    arrive under different keys than "message", groups are filtered the way
    discord_plugin filters to DMs, and photos/stickers/voice notes simply have
    no "text". A shared location is the one non-text message that is acted on.
    """
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    if (message.get("chat") or {}).get("type") != "private":
        return None
    text = message.get("text")
    location = message.get("location")
    if not text and isinstance(location, dict):
        # The paperclip "Location" share is how a phone hands over its GPS fix.
        # Rendered as the words a user would type so weather_skill.set_location
        # stays the only write path -- translation, not domain logic.
        text = f"my location is {location.get('latitude')}, {location.get('longitude')}"
    if not text:
        return None
    user_id = (message.get("from") or {}).get("id")
    if user_id is None:
        return None
    return user_id, text


async def _poll_once(offset: int | None) -> int | None:
    """One getUpdates round trip. Returns the offset for the next call."""
    params: dict = {"timeout": _LONG_POLL_SECONDS}
    if offset is not None:
        params["offset"] = offset
    updates = await _api("getUpdates", data=params) or []

    for update in updates:
        # Advance past every update we have SEEN, not just the ones we act on,
        # and do it before dispatching. Telegram redelivers anything at or
        # above the offset on every poll until it is confirmed, so a sticker we
        # skip — or a message whose handling blew up — would otherwise come
        # back forever and be answered forever.
        offset = update["update_id"] + 1
        parsed = _incoming(update)
        if parsed is None:
            continue
        user_id, text = parsed
        try:
            # Two different ids on purpose: core gets the Wren user id (see
            # _wren_user_id), the channel keeps the raw Telegram chat id it has
            # to answer into. In a private chat the chat id IS the user id, so
            # the one from the update serves as both.
            #
            # Awaited inline rather than spawned as a task: handle_message can
            # sit in the LLM for many seconds, and the cost of waiting is that
            # later messages queue on Telegram's side until this one finishes —
            # fine for a single-user assistant, and it keeps replies in order.
            # A task would be more responsive but needs a strong reference kept
            # somewhere (fire-and-forget tasks can be garbage collected
            # mid-flight) and lets two replies interleave in one chat.
            await core.handle_message(_wren_user_id(user_id), text, TelegramChannel(user_id))
        except Exception as e:
            # Never log the body — Telegram DMs are as private as reminders
            # (see http_plugin.notify).
            logging.error(f"telegram: handling a message from {user_id} failed: {e}")
    return offset


async def notify(user_id: int, text: str) -> bool:
    try:
        # ponytail: a text long enough to split can be delivered in part and
        # then fail, and this still answers False, so the caller consumes it.
        # Losing the tail of a >4096-char reminder to a mid-send block beats
        # the alternative of re-delivering the head on every retry.
        await _send_text(_telegram_chat_id(user_id), text)
        return True
    except TelegramError as e:
        # router.notify()'s contract, and it matters: False means PERMANENT.
        # The caller consumes what it was delivering when it sees False — the
        # reminder gets marked fired, the email gets flagged \Seen. So only
        # answers that will never succeed may return False. 403 (bot blocked,
        # or the user never pressed Start) and 400 (chat not found) are those.
        # Everything else — 429, 5xx — is transient and must RAISE so the
        # caller retries on its next poll instead of destroying the message.
        # Network errors are not TelegramError at all and propagate already.
        if e.code in (400, 403):
            logging.warning(f"telegram cannot reach user {user_id}: {e}")
            return False
        raise


async def start() -> None:
    """Runs for the lifetime of the process. A communication plugin's start()
    must not return while the plugin is live — run.py gathers these, so
    returning early would shut Wren down."""
    if not config.TELEGRAM_TOKEN:
        raise RuntimeError(
            "COMMUNICATION_PLUGINS includes 'telegram' but TELEGRAM_TOKEN is not set. "
            "Set it, or drop 'telegram' from COMMUNICATION_PLUGINS."
        )
    router.register("telegram", sys.modules[__name__])
    logging.info("Telegram plugin polling for updates")

    # ponytail: the offset lives in memory only. Restarting Wren means
    # Telegram replays whatever it still holds and has not seen confirmed (up
    # to 24h of it), so a message sent while Wren was down may be answered
    # twice — or, if the last poll before the restart confirmed it, not at all.
    # Persisting it is one more row in a state table if that ever stings.
    offset: int | None = None
    backoff = _RETRY_SECONDS
    while True:
        try:
            offset = await _poll_once(offset)
            backoff = _RETRY_SECONDS        # healthy again
        except asyncio.CancelledError:
            # Redundant since 3.8 (CancelledError is a BaseException, so the
            # clause below already misses it) but spelled out on purpose:
            # swallowing this would make the task un-killable and hang
            # shutdown. Do not fold it into the handler below.
            raise
        except Exception as e:
            # Everything else stops here. run.py gathers start(), and
            # asyncio.gather re-raises the first exception it sees — letting a
            # DNS blip out of this loop would take Discord and the web chat
            # down with it.
            logging.warning(f"telegram poll failed ({e}); retrying in {backoff}s")
            await asyncio.sleep(backoff)
            # Grow the gap: a revoked token fails identically forever, and one
            # doomed request every 5 minutes beats one every 5 seconds.
            backoff = min(backoff * 2, _MAX_RETRY_SECONDS)
