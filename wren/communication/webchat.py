import base64
import importlib
import pathlib
import pkgutil

from aiohttp import web

from .. import config
from .. import conversations
from .. import core

HISTORY_LIMIT = 20

_PAGE = pathlib.Path(__file__).with_name("chat.html")


class WebChannel:
    """Channel backed by the conversations table.

    Unlike CollectingChannel this one has real history — it is the only
    non-Discord surface that does, because a chat UI is multi-turn by
    definition.
    """

    def __init__(self, conversation_id: int, owner_id: int, before_id: int | None = None):
        self._conversation_id = conversation_id
        self._owner_id = owner_id
        self._before_id = before_id
        self.sent: list[str] = []
        self.files: list[tuple[bytes, str]] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)
        conversations.add_message(self._conversation_id, "assistant", text)

    async def send_file(self, data: bytes, filename: str) -> None:
        # returned inline, not stored — matches what the machine API does
        self.files.append((data, filename))

    async def history(self, limit: int = HISTORY_LIMIT) -> list[dict] | None:
        rows = conversations.messages(
            self._conversation_id, before_id=self._before_id, limit=limit
        )
        if not rows:
            return None
        return [{"role": r["role"], "content": r["content"]} for r in rows]

    async def ack(self, state: str) -> None:
        # the page renders its own thinking indicator
        return None


def _conversation_or_404(conversation_id, user_id) -> dict:
    convo = conversations.get(conversation_id, user_id)
    if convo is None:
        # 404 rather than 403 on purpose: a 403 would confirm that someone
        # else's conversation exists at this id
        raise web.HTTPNotFound(text='{"error": "no such conversation"}',
                               content_type="application/json")
    return convo


# sqlite binds 64-bit signed integers; Python ints are unbounded, so
# int("9223372036854775808") parses fine and then blows up at bind time as a
# 500 instead of an honest 404
_SQLITE_INT_MAX = 2 ** 63 - 1


def _id_from(request: web.Request) -> int:
    try:
        value = int(request.match_info["id"])
    except (KeyError, ValueError):
        raise web.HTTPNotFound(text='{"error": "no such conversation"}',
                               content_type="application/json")
    if not (-_SQLITE_INT_MAX - 1 <= value <= _SQLITE_INT_MAX):
        raise web.HTTPNotFound(text='{"error": "no such conversation"}',
                               content_type="application/json")
    return value


async def _json_object(request: web.Request) -> dict:
    """request.json() is json.loads, so `[1,2]`, `"hi"` and `123` all decode —
    and then .get() raises AttributeError, i.e. a 500 from a crafted body."""
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text='{"error": "body must be JSON"}',
                                 content_type="application/json")
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text='{"error": "body must be a JSON object"}',
                                 content_type="application/json")
    return body


def _plugin_row(module, running: bool) -> dict:
    active = getattr(module, "is_active", lambda: True)()
    row = {
        "module": module.__name__.rsplit(".", 1)[-1],
        "name": getattr(module, "PLUGIN_NAME", module.__name__),
        "active": active,
        "running": running,
    }
    if not active:
        reason = getattr(module, "inactive_reason", lambda: "Not configured.")()
        row["reason"] = reason
    return row


def _channel_rows() -> list[dict]:
    """Every communication plugin in the package, enabled or not.

    Discovered rather than read from COMMUNICATION_PLUGINS, because the panel
    has to show the channels you have NOT enabled -- that is the whole point of
    "Telegram: no TELEGRAM_TOKEN". Importing an unenabled module is safe: these
    files define constants and functions at module level and start nothing
    until start() is awaited.
    """
    from .. import communication

    rows = []
    for info in sorted(pkgutil.iter_modules(communication.__path__), key=lambda i: i.name):
        if not info.name.endswith("_plugin"):
            continue
        module = importlib.import_module(f"..communication.{info.name}", __package__)
        name = info.name[: -len("_plugin")]
        row = _plugin_row(module, running=name in config.COMMUNICATION_PLUGINS)
        row["role"] = getattr(module, "ROLE", "chat")
        rows.append(row)
    return rows


async def page(request: web.Request) -> web.Response:
    # unauthenticated on purpose: a static shell containing no user data. It
    # renders a "paste your token" box, and every request it then makes carries
    # the bearer header. Serving this without auth avoids inventing a second
    # auth path (cookies) just to fetch some HTML.
    return web.Response(text=_PAGE.read_text(encoding="utf-8"), content_type="text/html")


def register_routes(app: web.Application, authenticate) -> None:
    """Mounted by the http surface so the chat shares its port and tokens."""

    def _auth(request):
        user_id = authenticate(request)
        if user_id is None:
            raise web.HTTPUnauthorized(text='{"error": "unauthorized"}',
                                       content_type="application/json")
        # authn is not authz: a token maps to a user id, but that user must
        # also still be whitelisted. core.handle_message enforces this too, but
        # it returns silently — without the check here a de-whitelisted token
        # would get a cheerful 200 and, worse, still write rows.
        if user_id not in config.id_to_name():
            raise web.HTTPForbidden(text='{"error": "user not whitelisted"}',
                                    content_type="application/json")
        return user_id

    async def list_conversations(request):
        return web.json_response(conversations.list_for(_auth(request)))

    async def create_conversation(request):
        user_id = _auth(request)
        convo_id = conversations.create(user_id)
        return web.json_response({"id": convo_id, "title": "New chat"})

    async def get_conversation(request):
        user_id = _auth(request)
        convo = _conversation_or_404(_id_from(request), user_id)
        convo["messages"] = [
            {"role": m["role"], "content": m["content"], "created_at": m["created_at"]}
            for m in conversations.messages(convo["id"])
        ]
        return web.json_response(convo)

    async def rename_conversation(request):
        user_id = _auth(request)
        convo_id = _id_from(request)
        _conversation_or_404(convo_id, user_id)
        body = await _json_object(request)
        title = body.get("title", "")
        if not isinstance(title, str) or not conversations.rename(convo_id, user_id, title):
            return web.json_response({"error": "title must not be empty"}, status=400)
        return web.json_response({"ok": True})

    async def delete_conversation(request):
        user_id = _auth(request)
        convo_id = _id_from(request)
        _conversation_or_404(convo_id, user_id)
        conversations.delete(convo_id, user_id)
        return web.json_response({"ok": True})

    async def post_message(request):
        user_id = _auth(request)
        convo_id = _id_from(request)
        convo = _conversation_or_404(convo_id, user_id)

        body = await _json_object(request)
        text = body.get("text", "")
        if not isinstance(text, str) or not text.strip():
            return web.json_response({"error": "missing 'text'"}, status=400)

        # persist first so a failed LLM call doesn't lose what was typed, then
        # exclude it from history — core passes it separately as `text`, and
        # without before_id the model would see the same message twice
        # Auto-title from the first message, but only if the user hasn't named
        # it themselves. Both conditions are needed: title alone would clobber
        # a conversation renamed before its first message, and emptiness alone
        # would clobber one renamed straight after creation. Renaming an
        # established chat to "New chat" is safe — it already has messages.
        untitled = convo["title"] == "New chat" and not conversations.messages(convo_id, limit=1)
        user_msg_id = conversations.add_message(convo_id, "user", text)
        if untitled:
            conversations.rename(convo_id, user_id, conversations.title_from(text))

        channel = WebChannel(convo_id, user_id, before_id=user_msg_id)
        await core.handle_message(user_id, text, channel)
        conversations.touch(convo_id, user_id)

        return web.json_response({
            "replies": channel.sent,
            "files": [
                {"filename": name, "data": base64.b64encode(data).decode("ascii")}
                for data, name in channel.files
            ],
        })

    def _owner(request):
        user_id = _auth(request)
        if user_id != config.WHITELIST["owner"]:
            raise web.HTTPForbidden(text='{"error": "owner only"}',
                                    content_type="application/json")
        return user_id

    async def get_plugins(request):
        _owner(request)
        from .. import registry
        return web.json_response({
            "skills": [
                dict(_plugin_row(p, running=True), enabled=registry.is_enabled(p))
                for p in registry.PLUGINS
            ],
            "channels": _channel_rows(),
            "settings": {key: str(getattr(config, key)) for key in config.SETTABLE},
        })

    async def patch_plugin(request):
        _owner(request)
        from .. import registry
        module_name = request.match_info["module"]
        body = await request.json()
        target = next((p for p in registry.PLUGINS if registry.skill_key(p) == module_name), None)
        if target is None:
            if any(r["module"] == module_name for r in _channel_rows()):
                return web.json_response(
                    {"error": "channels cannot be toggled here; they are read once at "
                              "startup from COMMUNICATION_PLUGINS and need a restart"},
                    status=400)
            return web.json_response({"error": "unknown plugin"}, status=404)
        registry.set_enabled(target, bool(body.get("enabled")))
        return web.json_response(dict(_plugin_row(target, running=True),
                                      enabled=registry.is_enabled(target)))

    async def patch_settings(request):
        _owner(request)
        body = await request.json()
        for key, value in body.items():
            try:
                config.set_override(key, str(value))
            except KeyError:
                return web.json_response({"error": f"'{key}' is not a settable option"}, status=400)
            except (ValueError, RuntimeError) as e:
                return web.json_response({"error": f"{key}: {e}"}, status=400)
        return web.json_response({key: str(getattr(config, key)) for key in config.SETTABLE})

    app.router.add_get("/", page)
    app.router.add_get("/api/conversations", list_conversations)
    app.router.add_post("/api/conversations", create_conversation)
    app.router.add_get("/api/conversations/{id}", get_conversation)
    app.router.add_patch("/api/conversations/{id}", rename_conversation)
    app.router.add_delete("/api/conversations/{id}", delete_conversation)
    app.router.add_post("/api/conversations/{id}/message", post_message)
    app.router.add_get("/api/plugins", get_plugins)
    app.router.add_patch("/api/plugins/{module}", patch_plugin)
    app.router.add_patch("/api/settings", patch_settings)
