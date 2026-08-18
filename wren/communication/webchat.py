import base64
import importlib
import json
import logging
import pathlib
import pkgutil

from aiohttp import web

from .. import config
from .. import conversations
from .. import core
from ..channel import CollectingChannel, Ctx

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
        self.cards: list[dict] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)
        conversations.add_message(self._conversation_id, "assistant", text)

    async def send_file(self, data: bytes, filename: str) -> None:
        # returned inline, not stored — matches what the machine API does
        self.files.append((data, filename))

    async def send_card(self, kind: str, data: dict, text: str,
                        *, intent: str = "", params: dict | None = None) -> None:
        """`data` goes to the page for this render; `intent`+`params` are what
        get stored, because that is what re-renders the card an hour from now.

        intent/params are keyword-only and defaulted so this still satisfies the
        Channel protocol, which knows nothing about them -- a surface that draws
        cards needs them, and one that prints prose does not.
        """
        ref = {"kind": kind, "intent": intent, "params": params or {}}
        # `text` rides along in the response but is NOT in `ref`, so it is not
        # stored twice -- the messages row already holds it in `content`. The
        # page needs it to draw the bubble a card sits in.
        self.cards.append({**ref, "data": data, "text": text})
        # Mirrors send()'s bookkeeping half so `replies` still carries the
        # prose (a card-blind reader of the response sees a normal reply) --
        # but not send()'s add_message call, which would double the row.
        self.sent.append(text)
        conversations.add_message(self._conversation_id, "assistant", text, card=ref)

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


def _plugin_row(module, running: bool | None = None) -> dict:
    active = getattr(module, "is_active", lambda: True)()
    row = {
        "module": module.__name__.rsplit(".", 1)[-1],
        "name": getattr(module, "PLUGIN_NAME", module.__name__),
        "active": active,
    }
    if running is not None:
        # Only channels have a meaningful "running" (started from
        # COMMUNICATION_PLUGINS at boot). Skills are always live the moment
        # they're enabled, so callers building skill rows omit `running`
        # entirely rather than pass a hardcoded filler chat.html never reads.
        row["running"] = running
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


async def _fetch_models(base_url: str, api_key: str) -> list[str]:
    """The active provider's /v1/models, as plain ids.

    Proxied rather than fetched by the browser: once Wren is bound to
    127.0.0.1 the browser can only reach the reverse proxy, and this keeps the
    internal LLM endpoint out of the page source.
    """
    import aiohttp

    url = base_url.rstrip("/") + "/models"
    # Some providers (ollama) need no key at all, so a falsy value omits the
    # header rather than sending "Bearer None". A placeholder like ollama's
    # own "not-needed" IS sent as a real Bearer value -- harmless, and
    # consistent with how brain.py treats the same api_key for chat traffic.
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers) as resp:
            resp.raise_for_status()  # a JSON error body must not read as "zero models"
            body = await resp.json()
    return [m["id"] for m in body.get("data", []) if m.get("id")]


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
            {"role": m["role"], "content": m["content"], "created_at": m["created_at"],
             "card": m["card"]}
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
            "cards": channel.cards,
        })

    async def dispatch(request):
        """Run one intent directly, skipping the classifier.

        This is the card API: buttons, refresh and space panes all arrive here.
        Deliberately NOT a shortcut around authorization -- every intent
        reachable here is already reachable by typing a sentence, and dispatch
        goes through the skill's own handle(), so a skill's internal checks
        (contacts_skill's owner gate, for one) still run untouched.

        Nothing is written to any conversation: clicking a card control must not
        manufacture a fake user turn. The cost is that the model does not learn
        about button-driven changes conversationally; it re-reads the store on
        the next question, so this is only visible if you immediately ask what
        you just clicked.
        """
        user_id = _auth(request)
        from .. import registry

        body = await _json_object(request)
        intent = body.get("intent")
        if not isinstance(intent, str) or not intent:
            return web.json_response({"error": "missing 'intent'"}, status=400)

        plugin = registry.INTENT_HANDLERS.get(intent)
        if plugin is None or not registry.is_enabled(plugin):
            # 404 for both: "no such intent" and "that skill is off" are the
            # same answer from the caller's side -- it is not available.
            return web.json_response({"error": "unknown intent"}, status=404)

        content = body.get("content") or ""
        tags = body.get("tags") or []
        if not isinstance(content, str) or not isinstance(tags, list):
            return web.json_response({"error": "bad 'content' or 'tags'"}, status=400)

        channel = CollectingChannel()
        ctx = Ctx(user_id=user_id, channel=channel, content=content, tags=tags)
        await plugin.handle(intent, ctx)

        return web.json_response({
            "cards": [{"kind": c["kind"], "data": c["data"]} for c in channel.cards],
            "replies": channel.sent,
        })

    async def get_me(request):
        # Any authenticated whitelisted user, NOT owner-only: /api/plugins is,
        # so a household member loading the page would get a 403 and no
        # greeting at all. This returns only the caller's own name plus two
        # non-sensitive facts the landing screen needs.
        user_id = _auth(request)
        from .. import registry

        if user_id == config.WHITELIST["owner"]:
            # The whitelist alias for the owner is the literal string "owner"
            # -- a placeholder, not a name -- so never fall back to it here.
            name = config.OWNER_NAME or None
        else:
            alias = config.id_to_name().get(user_id)
            # Same placeholder refusal as the owner branch above: contacts.py
            # reserves no aliases, so contacts.add("owner", ...) is accepted,
            # and without this a contact using that alias would be greeted
            # "Good evening, Owner." -- the placeholder leaking through the
            # one path that was supposed to keep it out entirely.
            name = alias.title() if alias and alias != "owner" else None
        return web.json_response({
            "name": name,
            "skills": [registry.skill_key(p) for p in registry.PLUGINS
                       if registry.is_enabled(p)],
            # Also here, not just in the owner-only /api/models, so a
            # household member's composer can show which model is answering.
            "model": config.LLM_CHAIN[0]["model"] if config.LLM_CHAIN else None,
        })

    def _owner(request):
        user_id = _auth(request)
        if user_id != config.WHITELIST["owner"]:
            raise web.HTTPForbidden(text='{"error": "owner only"}',
                                    content_type="application/json")
        return user_id

    async def get_models(request):
        _owner(request)
        if not config.LLM_CHAIN:
            return web.json_response(
                {"provider": None, "current": None, "models": [],
                 "reason": "no LLM provider is configured"})
        active = config.LLM_CHAIN[0]
        try:
            models = await _fetch_models(active["base_url"], active["api_key"])
            reason = None
        except Exception as e:
            # 200 with an empty list, never a 5xx: this feeds the landing
            # screen, which must render even when the LLM host is down. The
            # dropdown degrades to the current model as static text.
            logging.warning(f"could not list models from {active['name']}: {e}")
            models, reason = [], f"{active['name']} is not reachable right now"
        return web.json_response({
            "provider": active["name"],
            "current": active["model"],
            "models": models,
            "reason": reason,
        })

    async def get_plugins(request):
        _owner(request)
        from .. import registry
        return web.json_response({
            "skills": [
                dict(_plugin_row(p), enabled=registry.is_enabled(p))
                for p in registry.PLUGINS
            ],
            "channels": _channel_rows(),
            "settings": {key: config.serialize_setting(key) for key in config.SETTABLE},
        })

    async def patch_plugin(request):
        _owner(request)
        from .. import registry
        module_name = request.match_info["module"]
        body = await _json_object(request)
        target = next((p for p in registry.PLUGINS if registry.skill_key(p) == module_name), None)
        if target is None:
            if any(r["module"] == module_name for r in _channel_rows()):
                return web.json_response(
                    {"error": "channels cannot be toggled here; they are read once at "
                              "startup from COMMUNICATION_PLUGINS and need a restart"},
                    status=400)
            return web.json_response({"error": "unknown plugin"}, status=404)
        registry.set_enabled(target, bool(body.get("enabled")))
        return web.json_response(dict(_plugin_row(target),
                                      enabled=registry.is_enabled(target)))

    async def patch_settings(request):
        _owner(request)
        body = await _json_object(request)
        for key, value in body.items():
            try:
                config.set_override(key, str(value))
            except KeyError:
                return web.json_response({"error": f"'{key}' is not a settable option"}, status=400)
            except (ValueError, RuntimeError) as e:
                return web.json_response({"error": f"{key}: {e}"}, status=400)
        return web.json_response({key: config.serialize_setting(key) for key in config.SETTABLE})

    app.router.add_get("/", page)
    app.router.add_get("/api/conversations", list_conversations)
    app.router.add_post("/api/conversations", create_conversation)
    app.router.add_get("/api/conversations/{id}", get_conversation)
    app.router.add_patch("/api/conversations/{id}", rename_conversation)
    app.router.add_delete("/api/conversations/{id}", delete_conversation)
    app.router.add_post("/api/conversations/{id}/message", post_message)
    app.router.add_post("/api/dispatch", dispatch)
    app.router.add_get("/api/me", get_me)
    app.router.add_get("/api/models", get_models)
    app.router.add_get("/api/plugins", get_plugins)
    app.router.add_patch("/api/plugins/{module}", patch_plugin)
    app.router.add_patch("/api/settings", patch_settings)
