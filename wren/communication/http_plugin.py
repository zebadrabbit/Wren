import asyncio
import base64
import hmac
import logging
import sys

from aiohttp import web

from .. import config
from .. import core
from .. import router
from ..channel import CollectingChannel

# 25 MB — a minute of 16 kHz mono WAV is about 2 MB, so this is generous for
# voice while still refusing anything absurd. aiohttp's default is 1 MB, which
# a few seconds of audio would blow straight through.
MAX_BODY_BYTES = 25 * 1024 * 1024


def _authenticate(request: web.Request) -> int | None:
    """Bearer token -> Wren user id, or None."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    # compare as bytes: hmac.compare_digest raises TypeError on non-ASCII str
    # operands, so a token with a stray unicode character would turn an
    # unauthenticated request into a 500 with a traceback instead of a 401
    presented = header[len("Bearer "):].encode("utf-8", "surrogatepass")
    # compare against every configured token rather than a dict lookup, so a
    # wrong token costs the same time as a right one
    matched = None
    for token, user_id in config.WREN_TOKENS.items():
        if hmac.compare_digest(presented, token.encode("utf-8")):
            matched = user_id
    return matched


def _payload(channel: CollectingChannel, **extra) -> dict:
    return {
        "replies": channel.sent,
        "files": [
            {"filename": name, "data": base64.b64encode(data).decode("ascii")}
            for data, name in channel.files
        ],
        **extra,
    }


async def _dispatch(user_id: int, text: str, **extra) -> web.Response:
    channel = CollectingChannel()
    await core.handle_message(user_id, text, channel)
    if not channel.sent and not channel.files:
        # core's authorization gate returned silently — the token is valid but
        # maps to a user who is not whitelisted. Say so rather than returning
        # an empty 200 that looks like a bug.
        if user_id not in config.id_to_name():
            return web.json_response(
                {"error": "user not whitelisted"}, status=403
            )
    return web.json_response(_payload(channel, **extra))


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def message(request: web.Request) -> web.Response:
    user_id = _authenticate(request)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "body must be JSON"}, status=400)
    text = (body or {}).get("text", "")
    if not isinstance(text, str) or not text.strip():
        return web.json_response({"error": "missing 'text'"}, status=400)
    return await _dispatch(user_id, text)


async def voice(request: web.Request) -> web.Response:
    user_id = _authenticate(request)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)

    from .. import stt

    audio = await request.read()
    if not audio:
        return web.json_response({"error": "empty audio body"}, status=400)
    try:
        # to_thread, not a direct call: transcription is CPU-bound and takes
        # seconds. Inline it would freeze the event loop this shares with the
        # Discord gateway (heartbeat -> disconnect) and every plugin poller.
        transcript = await asyncio.to_thread(stt.transcribe, audio)
    except stt.STTUnavailable as e:
        return web.json_response({"error": str(e)}, status=503)
    except Exception as e:
        logging.error(f"transcription failed: {e}")
        return web.json_response({"error": "transcription failed"}, status=500)

    if not transcript.strip():
        return web.json_response({"transcript": "", "replies": [], "files": []})
    return await _dispatch(user_id, transcript, transcript=transcript)


def build_app() -> web.Application:
    from . import webchat

    app = web.Application(client_max_size=MAX_BODY_BYTES)
    app.router.add_get("/health", health)
    app.router.add_post("/message", message)
    app.router.add_post("/voice", voice)
    # the browser chat rides the same port and the same tokens
    webchat.register_routes(app, _authenticate)
    return app


ROLE = "chat"   # chat: input + output, but send-only (see notify() below)
PLUGIN_NAME = "HTTP / Web Chat"
CAN_NOTIFY = False


async def notify(user_id: int, text: str) -> bool:
    # ponytail: send-only by design — no WebSocket, no subscriber registry.
    # If desktop push is ever wanted, this is where polling or SSE would land.
    #
    # The message body is deliberately NOT logged: reminders and person-to-
    # person messages are private, and this would put all of them in the
    # system journal on an http-first install.
    logging.warning(
        f"NOTIFY_SURFACE=http cannot deliver unprompted messages (send-only); "
        f"dropped a {len(text)}-char message for user {user_id}"
    )
    return False


async def start() -> None:
    """Runs for the lifetime of the process. A surface's start() must not
    return while the surface is live — run.py gathers these, so returning
    early would shut Wren down."""
    if not config.WREN_TOKENS:
        raise RuntimeError(
            "COMMUNICATION_PLUGINS includes 'http' but WREN_TOKENS is empty. "
            "Set WREN_TOKENS=<token>:<user_id> so requests can be authenticated."
        )
    router.register("http", sys.modules[__name__])
    runner = web.AppRunner(build_app())
    await runner.setup()
    site = web.TCPSite(runner, config.WREN_HTTP_HOST, config.WREN_HTTP_PORT)
    await site.start()
    logging.info(f"HTTP surface on http://{config.WREN_HTTP_HOST}:{config.WREN_HTTP_PORT}")
    try:
        await asyncio.Event().wait()   # serve until cancelled
    finally:
        await runner.cleanup()
