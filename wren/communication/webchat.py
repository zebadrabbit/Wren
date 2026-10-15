import importlib
import logging
import pathlib
import pkgutil

from aiohttp import web

from .. import config
from .. import contacts
from .. import conversations
from ..channel import CollectingChannel, Ctx
from .http_plugin import file_entries

_PAGE = pathlib.Path(__file__).with_name("chat.html")



# sqlite binds 64-bit signed integers; Python ints are unbounded, so
# int("9223372036854775808") parses fine and then blows up at bind time as a
# 500 instead of an honest 404
_SQLITE_INT_MAX = 2 ** 63 - 1


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

    def _query_int(request, key, default=None):
        raw = request.query.get(key)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError:
            raise web.HTTPBadRequest(text=f'{{"error": "{key} must be an integer"}}',
                                     content_type="application/json")
        if not (0 <= value <= _SQLITE_INT_MAX):
            # sqlite binds 64-bit signed ints; an unbounded Python int would
            # blow up at bind time as a 500 instead of an honest 400
            raise web.HTTPBadRequest(text=f'{{"error": "{key} is out of range"}}',
                                     content_type="application/json")
        return value

    async def timeline(request):
        """The person's whole thread, paged. No conversation ids anywhere:
        a token maps to a person and a person has one timeline."""
        user_id = _auth(request)
        before = _query_int(request, "before")
        after = _query_int(request, "after")
        if before is not None and after is not None:
            return web.json_response({"error": "use before or after, not both"}, status=400)
        # clamped, not refused: a page asking for 999 wants "a lot", and 200
        # is the most one response should carry
        limit = max(1, min(_query_int(request, "limit", 50), 200))
        return web.json_response(
            conversations.timeline(user_id, before=before, after=after, limit=limit))

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
        person = body.get("person")
        when = body.get("when")
        list_name = body.get("list") or ""
        if (not isinstance(content, str)
                or not isinstance(tags, list) or not all(isinstance(t, str) for t in tags)
                or (person is not None and not isinstance(person, str))
                or (when is not None and not isinstance(when, str))
                or not isinstance(list_name, str)):
            # tags elements matter, not just the container: notes_store does
            # ",".join(tags), which TypeErrors on a non-str element -- a 500
            # from a crafted body, same class of bug _json_object guards
            # against for the body itself.
            return web.json_response({"error": "bad 'content', 'tags', 'person', 'when' or 'list'"},
                                     status=400)

        channel = CollectingChannel()
        ctx = Ctx(user_id=user_id, channel=channel, content=content, tags=tags,
                  person=person, when=when, list_name=list_name)
        try:
            await plugin.handle(intent, ctx)
        except Exception as e:
            # Mirrors core.handle_message's except clause in shape: log the
            # real exception server-side, tell the caller only that it
            # failed. NOT str(e) -- an exception is not the skill's own error
            # text (that arrives via channel.send() into `replies` with a
            # 200, e.g. web_skill's "Search is unavailable right now."); it
            # is a crash the skill did not anticipate, and library exception
            # text is not safe to echo verbatim. brain._complete re-raises
            # the provider client's exception on an LLM outage, and that can
            # carry the configured base_url or a hosted provider's response
            # body -- the same class of leak telegram_plugin.py already had
            # to scrub a bot token out of once.
            logging.error(f"Error dispatching {intent} for {user_id}: {e}")
            return web.json_response({"error": "Something went wrong, try again."},
                                     status=500)

        return web.json_response({
            "cards": channel.cards,
            "replies": channel.sent,
            "files": file_entries(channel.files),
        })

    async def get_spaces(request):
        # Any whitelisted user, like /api/me: the sidebar is the same for the
        # household.
        _auth(request)
        from .. import registry
        return web.json_response(registry.spaces())

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
            # which apps a person can be added on here, for the People tab's
            # form: the configured chat plugins that hold ids of their own
            "surfaces": [s for s in config.COMMUNICATION_PLUGINS if s in contacts.SURFACES],
            "skills": [registry.skill_key(p) for p in registry.PLUGINS
                       if registry.is_enabled(p)],
            # so the top bar can show which model is answering; the model
            # itself is .env-only (the live picker went on 2026-09-26: Wren
            # is tuned to one model now)
            "model": config.LLM_CHAIN[0]["model"] if config.LLM_CHAIN else None,
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
                dict(_plugin_row(p), enabled=registry.is_enabled(p))
                for p in registry.PLUGINS
            ],
            "channels": _channel_rows(),
            "settings": {key: config.serialize_setting(key) for key in config.SETTABLE},
            # shown read-only: .env defines them, and PATCH would be refused
            "env_locked": sorted(config.ENV_DEFINED),
            # booleans only, by construction -- the values live in .env on the
            # host and are managed with manage.sh, never through this surface
            "secrets": config.secret_status(),
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
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            # not bool(...): bool("false") is True, and a skill would switch on
            return web.json_response({"error": '"enabled" must be true or false'}, status=400)
        registry.set_enabled(target, enabled)
        return web.json_response(dict(_plugin_row(target),
                                      enabled=registry.is_enabled(target)))

    def _contact_row(alias: str) -> dict:
        """Ids as strings, never numbers: a Discord id is 18 digits, past the
        2**53 a JSON number survives in a browser, and JSON.parse rounds
        123456789012345678 to ...680 without a word. `wren_id` is the one they
        sign in as (the contact's stored wren_id), which is what a token is minted for."""
        row = contacts.get(alias) or {"alias": alias, **{s: None for s in contacts.SURFACES}}
        wren_id = contacts.all().get(alias)
        return {"alias": row["alias"],
                **{s: (str(row[s]) if row[s] is not None else None) for s in contacts.SURFACES},
                "wren_id": str(wren_id) if wren_id is not None else None}

    async def list_contacts(request):
        _owner(request)
        return web.json_response([_contact_row(a) for a in sorted(contacts.all())])

    async def add_contact(request):
        """Body checks here; the rules are contacts.register, shared with the
        chat skill: a shape refusal is a 400, a taken name or id a 409, each
        with its reason."""
        _owner(request)
        body = await _json_object(request)
        alias, surface, raw_id = body.get("alias"), body.get("surface"), body.get("id")
        if not isinstance(alias, str) or not alias.strip():
            return web.json_response({"error": "alias must be a name"}, status=400)
        alias = alias.strip().lower()
        if alias == "owner":
            return web.json_response({"error": '"owner" is the placeholder for you, not a contact'}, status=400)
        if surface not in contacts.SURFACES:
            return web.json_response({"error": f"surface must be one of {', '.join(contacts.SURFACES)}"}, status=400)
        if not isinstance(raw_id, (str, int)) or isinstance(raw_id, bool):
            return web.json_response({"error": "id must be the id that app calls them"}, status=400)
        try:
            new = contacts.register(alias, surface, str(raw_id), reserved=config.whitelist())
        except contacts.Invalid as e:
            return web.json_response({"error": str(e)}, status=400)
        except contacts.Taken as e:
            return web.json_response({"error": str(e)}, status=409)
        return web.json_response(_contact_row(alias), status=201 if new else 200)

    async def remove_contact(request):
        _owner(request)
        alias = request.match_info["alias"].strip().lower()
        if not contacts.remove(alias):
            return web.json_response({"error": "no such contact"}, status=404)
        return web.json_response({"ok": True})

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
    app.router.add_get("/api/timeline", timeline)
    app.router.add_post("/api/dispatch", dispatch)
    app.router.add_get("/api/me", get_me)
    app.router.add_get("/api/spaces", get_spaces)
    app.router.add_get("/api/plugins", get_plugins)
    app.router.add_patch("/api/plugins/{module}", patch_plugin)
    app.router.add_patch("/api/settings", patch_settings)
    app.router.add_get("/api/contacts", list_contacts)
    app.router.add_post("/api/contacts", add_contact)
    app.router.add_delete("/api/contacts/{alias}", remove_contact)
