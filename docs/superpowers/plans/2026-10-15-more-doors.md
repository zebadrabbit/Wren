# More Doors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Slack and Matrix become chat doors into Wren with Telegram's posture (outbound only, no new dependency), after contact ids learn to be strings.

**Architecture:** `contacts` keeps an integer Wren id per person and gains string per-surface ids with a shape per surface. Each new door is one module in `wren/communication/` in the exact shape of `telegram_plugin.py`: an `_api` helper that scrubs tokens, a pure `_incoming` parser, a `Channel` class, `notify`, and a `start` loop that never lets an exception escape. Core, the router and every skill are untouched.

**Tech Stack:** Python 3.12, aiohttp (HTTP and websockets), sqlite3, pytest with `AsyncMock`/`monkeypatch`. No new packages.

**Spec:** `docs/superpowers/specs/2026-10-15-more-doors-design.md`

## Global Constraints

- A communication plugin never contains domain logic; it authenticates, builds a `Channel`, calls `core.handle_message(user_id, text, channel, files=...)` (CLAUDE.md rule 2).
- Every door declares `ROLE = "chat"`, `PLUGIN_NAME`, `CAN_NOTIFY = True`, `is_active()`, `inactive_reason()`, `async start()`, `async notify(user_id, text) -> bool`, and a `Channel` class with `NAME`.
- `notify`'s contract: `False` means permanent, transient failures raise (router.py docstring).
- `start()` must never return while the door is live and must never let an exception other than `CancelledError` escape (run.py gathers them).
- No token ever appears in a URL, a log line or an exception string. Message bodies are never logged.
- Tests mock nothing network-level; they monkeypatch the module's `_api` (and the websocket for Slack) and call functions directly.
- Never run anything against the real `wren.db`; tests set `WREN_DB` per test via the fixtures shown.
- Each test file begins with the six `os.environ.setdefault` lines the other test files use (copied verbatim in each task).
- Commits one per task, message in the repo's `type(scope): summary` style, ending with the attribution line the harness supplies.

## Review Focus

1. A Slack `message` event for an edited message (`subtype: "message_changed"`) must be ignored, not answered as new. Pinned by `test_incoming_ignores_edits_and_bots` (Task 4).
2. Slack retries an envelope it did not get acked within 3 s; the ack must be sent before `core.handle_message` is awaited, or a slow LLM reply is answered twice. Pinned by `test_envelope_is_acked_before_the_message_is_handled` (Task 5).
3. A Matrix initial sync carries recent history; acting on it after a restart re-answers old messages. Pinned by `test_initial_sync_acts_on_nothing` (Task 7).
4. A contact id typed with spaces or in lower case (`u0123abc`) should be refused clearly rather than stored and never matched. Pinned by `test_register_refuses_a_malformed_id` (Task 1).
5. `notify` to a Matrix contact who has not accepted Wren's room invite yet must still count as delivered once sent (history is shared on join), and must not create a second room on the next reminder. Pinned by `test_notify_reuses_the_room_it_created` (Task 6).

---

### Task 1: Contact ids as strings in the store

**Files:**
- Modify: `wren/contacts.py` (whole module)
- Test: `tests/test_contacts.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces: `contacts.SURFACES == ("discord", "telegram", "slack", "matrix")`; `contacts.ID_SHAPE: dict[str, re.Pattern]`; `contacts.NUMERIC: frozenset[str]` (`{"discord", "telegram"}`); `class Invalid(ValueError)`; `register(alias: str, surface: str, raw_id: str, reserved=()) -> bool` raising `Invalid` or `Taken`; `add(alias, surface_id: int | str, surface="discord")`; `set_id(alias, surface, surface_id: int | str) -> bool`; `get(alias) -> dict | None` with per-surface values typed `int` for numeric surfaces and `str` otherwise; `surface_id(surface, wren_id) -> int | str | None` typed the same way; `wren_id(surface, surface_id: int | str) -> int | None`; `mint(surface, surface_id) -> int`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_contacts.py`:

```python
# --- string ids per surface (2026-10-15) ---------------------------------------
# Slack and Matrix ids are not numbers. The per-surface id is a string in the
# store's interface; the Wren id stays an int, minted for string-id surfaces.

def test_surfaces_and_shapes():
    assert contacts.SURFACES == ("discord", "telegram", "slack", "matrix")
    assert contacts.NUMERIC == frozenset({"discord", "telegram"})
    assert contacts.ID_SHAPE["slack"].fullmatch("U0123ABCDEF")
    assert contacts.ID_SHAPE["matrix"].fullmatch("@kim:home.net")
    assert not contacts.ID_SHAPE["slack"].fullmatch("u0123abcdef")
    assert not contacts.ID_SHAPE["discord"].fullmatch("U0123ABCDEF")

def test_register_refuses_a_malformed_id():
    for surface, raw in [("slack", "u0123abcdef"), ("slack", "U01 23ABCDEF"),
                         ("matrix", "kim:home.net"), ("discord", "12ab"), ("telegram", "")]:
        with pytest.raises(contacts.Invalid, match=f"look like a {surface} id"):
            contacts.register("kim", surface, raw)
    assert contacts.all() == {}

def test_slack_first_contact_gets_a_minted_stable_wren_id():
    assert contacts.register("kim", "slack", "U0123ABCDEF") is True
    wid = contacts.all()["kim"]
    assert isinstance(wid, int) and 0 < wid < 2 ** 56
    assert wid == contacts.mint("slack", "U0123ABCDEF")
    assert contacts.wren_id("slack", "U0123ABCDEF") == wid
    assert contacts.surface_id("slack", wid) == "U0123ABCDEF"
    assert contacts.get("kim") == {"alias": "kim", "discord": None, "telegram": None,
                                   "slack": "U0123ABCDEF", "matrix": None}
    assert contacts.surfaces_of(wid) == ["slack"]
    # removed and re-added: same id, so their notes are still theirs
    assert contacts.remove("kim")
    contacts.register("kim", "slack", "U0123ABCDEF")
    assert contacts.all()["kim"] == wid

def test_mint_differs_by_surface_and_id():
    assert contacts.mint("slack", "U0123ABCDEF") != contacts.mint("matrix", "U0123ABCDEF")
    assert contacts.mint("slack", "U0123ABCDEF") != contacts.mint("slack", "U0123ABCDEG")

def test_numeric_surfaces_keep_native_ints():
    contacts.register("hubby", "discord", "222222222222222222")
    contacts.register("hubby", "matrix", "@hubby:home.net")
    assert contacts.all() == {"hubby": 222222222222222222}
    assert contacts.get("hubby")["discord"] == 222222222222222222
    assert contacts.get("hubby")["matrix"] == "@hubby:home.net"
    assert contacts.surface_id("matrix", 222222222222222222) == "@hubby:home.net"
    assert contacts.wren_id("matrix", "@hubby:home.net") == 222222222222222222
    assert contacts.surfaces_of(222222222222222222) == ["discord", "matrix"]

def test_register_strips_the_id_before_checking_it():
    contacts.register("kim", "telegram", " 55 ")
    assert contacts.all() == {"kim": 55}

def test_init_db_adds_the_new_columns_to_an_existing_table(tmp_path, monkeypatch):
    from wren import db
    monkeypatch.setenv("WREN_DB", str(tmp_path / "old.db"))
    with db.conn() as con:
        con.executescript("""
            CREATE TABLE contacts (alias TEXT PRIMARY KEY, discord_id TEXT UNIQUE,
                                   telegram_id TEXT UNIQUE, wren_id TEXT, added_at TEXT NOT NULL);
            INSERT INTO contacts VALUES ('kim', NULL, '55', '55', 'x');
        """)
    contacts.init_db()
    contacts.init_db()                                        # idempotent
    assert contacts.get("kim") == {"alias": "kim", "discord": None, "telegram": 55,
                                   "slack": None, "matrix": None}
    assert contacts.set_id("kim", "slack", "U0123ABCDEF") is True
    assert contacts.surface_id("slack", 55) == "U0123ABCDEF"

def test_a_surface_id_is_unique_across_people():
    contacts.register("kim", "slack", "U0123ABCDEF")
    with pytest.raises(contacts.Taken, match="another name"):
        contacts.register("sam", "slack", "U0123ABCDEF")
```

- [ ] **Step 2: Run them to see them fail**

Run: `venv/bin/python3 -m pytest -q -p no:cacheprovider tests/test_contacts.py`
Expected: the eight new tests fail (`AttributeError: ... has no attribute 'ID_SHAPE'`, `Invalid`, etc.); the older ones pass.

- [ ] **Step 3: Rewrite `wren/contacts.py`**

Replace the module with this (it keeps every existing function and its behaviour for Discord and Telegram):

```python
import hashlib
import re
import sqlite3
from datetime import datetime, timezone

from . import db

# One id column per surface that has ids of its own, in the order the
# sidebar and the chat skill list them. Adding a surface means adding it
# here and to ID_SHAPE; init_db adds the column. The web chat is deliberately
# absent: it authenticates by token, and a token maps straight onto a Wren
# id (config.WREN_TOKENS).
SURFACES = ("discord", "telegram", "slack", "matrix")

# The one place an id's shape is known. The doors relay the refusal; they
# do not check digits themselves.
ID_SHAPE = {
    "discord": re.compile(r"\d+"),
    "telegram": re.compile(r"\d+"),
    "slack": re.compile(r"[UW][A-Z0-9]{8,}"),          # U0123ABCDEF, W for enterprise grid
    "matrix": re.compile(r"@[^:\s]+:[^\s]+"),           # @kim:home.net
}
# Surfaces whose id IS a number. Their id is the person's Wren id when it is
# the first one they are added with; readers hand it back as an int.
NUMERIC = frozenset(s for s, shape in ID_SHAPE.items() if shape.pattern == r"\d+")


def _col(surface: str) -> str:
    if surface not in SURFACES:
        raise ValueError(f"unknown surface {surface!r}; contacts hold ids for {SURFACES}")
    return f"{surface}_id"


def _native(surface: str, value):
    """A stored id in the type its surface uses: int for Discord and
    Telegram, str for the rest. None stays None."""
    if value is None:
        return None
    return int(value) if surface in NUMERIC else str(value)


def init_db() -> None:
    with db.conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                alias       TEXT PRIMARY KEY,
                discord_id  TEXT UNIQUE,
                telegram_id TEXT UNIQUE,
                wren_id     TEXT,
                added_at    TEXT NOT NULL
            )
        """)
        # Before 2026-09-26 the table was (alias, discord_id NOT NULL UNIQUE,
        # added_at). SQLite cannot drop NOT NULL or add a UNIQUE column in
        # place, so an old table is rebuilt; it holds a household, not a
        # customer base, and the copy is one statement.
        info = {row[1]: row for row in con.execute("PRAGMA table_info(contacts)")}
        if "telegram_id" not in info or info["discord_id"][3]:   # [3] is notnull
            con.executescript("""
                CREATE TABLE contacts_new (
                    alias       TEXT PRIMARY KEY,
                    discord_id  TEXT UNIQUE,
                    telegram_id TEXT UNIQUE,
                    wren_id     TEXT,
                    added_at    TEXT NOT NULL
                );
                INSERT INTO contacts_new (alias, discord_id, added_at)
                    SELECT alias, discord_id, added_at FROM contacts;
                DROP TABLE contacts;
                ALTER TABLE contacts_new RENAME TO contacts;
            """)
        # Before 2026-10-14 a person's Wren id was computed, COALESCE(discord_id,
        # telegram_id), so a Telegram-first person who later gained a Discord
        # id changed id and lost their notes. It is stored now: the id they
        # had under the old rule is written once and never moves.
        cols = {row[1] for row in con.execute("PRAGMA table_info(contacts)")}
        if "wren_id" not in cols:
            con.execute("ALTER TABLE contacts ADD COLUMN wren_id TEXT")
        con.execute("UPDATE contacts SET wren_id = COALESCE(discord_id, telegram_id) WHERE wren_id IS NULL")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS contacts_wren_id ON contacts(wren_id)")
        # Surfaces added since (slack, matrix, 2026-10-15): ALTER cannot carry
        # UNIQUE, so the index is separate. Same for any surface added later.
        for surface in SURFACES:
            col = _col(surface)
            if col not in cols:
                con.execute(f"ALTER TABLE contacts ADD COLUMN {col} TEXT")
            con.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS contacts_{col} ON contacts({col})")


def mint(surface: str, surface_id) -> int:
    """The Wren id for a person first added on a surface whose ids are not
    numbers. Deterministic, so removing and re-adding someone keeps their
    notes; 56 bits, so it never collides with a Discord snowflake in
    practice and is well past what a JSON number holds -- the web already
    sends ids as strings."""
    digest = hashlib.sha256(f"{surface}:{surface_id}".encode()).digest()
    return int.from_bytes(digest[:7], "big") or 1


def add(alias: str, surface_id, surface: str = "discord") -> None:
    col = _col(surface)
    ts = datetime.now(timezone.utc).isoformat()
    # the first id a person is added with is their Wren id for good: the id
    # itself when it is a number, a minted one otherwise
    wren = int(surface_id) if surface in NUMERIC else mint(surface, surface_id)
    with db.conn() as con:
        con.execute(
            f"INSERT INTO contacts (alias, {col}, wren_id, added_at) VALUES (?,?,?,?)",
            (alias.strip().lower(), str(surface_id), str(wren), ts),
        )


def set_id(alias: str, surface: str, surface_id) -> bool:
    """Give an existing contact their id on another surface."""
    col = _col(surface)
    with db.conn() as con:
        cur = con.execute(f"UPDATE contacts SET {col}=? WHERE alias=?",
                          (str(surface_id), alias.strip().lower()))
        return cur.rowcount > 0


class Taken(ValueError):
    """A refusal the person can read: the reason is the message."""


class Invalid(ValueError):
    """The id is not the shape that surface uses; the reason is the message."""


def register(alias: str, surface: str, raw_id: str, reserved=()) -> bool:
    """Add a person, or give an existing one their id on another surface:
    the one rule set behind "add 555 as kim on telegram" and the People tab.
    `raw_id` is what the person typed. True when the person is new. Raises
    Invalid when the id is not that surface's shape, and Taken, with the
    reason, when the name is reserved (the owner's) and not already a
    contact, when the person already has an id on that surface, or when
    the id is registered under another name."""
    _col(surface)
    raw_id = str(raw_id).strip()
    if not ID_SHAPE[surface].fullmatch(raw_id):
        raise Invalid(f"That doesn't look like a {surface} id.")
    alias = alias.strip().lower()
    row = get(alias)
    if row is None:
        if alias in reserved:
            raise Taken("That name's already taken.")
    elif row[surface] is not None:
        raise Taken(f"{alias} already has a {surface} id.")
    try:
        if row is None:
            add(alias, raw_id, surface=surface)
        else:
            set_id(alias, surface, raw_id)
    except sqlite3.IntegrityError:
        raise Taken("That user ID is already registered under another name.") from None
    return row is None


def get(alias: str) -> dict | None:
    """One contact's row: alias and the id on each surface (None where
    unset), each in its surface's own type."""
    with db.conn() as con:
        row = con.execute(
            f"SELECT alias, {', '.join(_col(s) for s in SURFACES)} FROM contacts WHERE alias=?",
            (alias.strip().lower(),)).fetchone()
    if not row:
        return None
    return {"alias": row[0], **{s: _native(s, v) for s, v in zip(SURFACES, row[1:])}}


def remove(alias: str) -> bool:
    with db.conn() as con:
        cur = con.execute("DELETE FROM contacts WHERE alias=?", (alias.strip().lower(),))
        return cur.rowcount > 0


# A contact's Wren id -- what every note, reminder and pin is filed under, and
# what core's whitelist gate checks -- is the id they were first added with
# (or minted from it), stored at add() and never changed by a second surface.
# Every chat surface translates its own id to this one on the way in and
# back on the way out.
_WREN_ID = "wren_id"


def all() -> dict[str, int]:
    with db.conn() as con:
        rows = con.execute(f"SELECT alias, {_WREN_ID} FROM contacts").fetchall()
    return {alias: int(wren_id) for alias, wren_id in rows}


def wren_id(surface: str, surface_id) -> int | None:
    """Who is talking, given the id a surface calls them."""
    col = _col(surface)
    with db.conn() as con:
        row = con.execute(f"SELECT {_WREN_ID} FROM contacts WHERE {col}=?",
                          (str(surface_id),)).fetchone()
    return int(row[0]) if row else None


def surface_id(surface: str, wren_id: int):
    """The inverse, for delivery: where a surface should send to reach this
    person, in that surface's own type (int for Discord and Telegram, str
    for Slack and Matrix)."""
    col = _col(surface)
    with db.conn() as con:
        row = con.execute(f"SELECT {col} FROM contacts WHERE {_WREN_ID}=?",
                          (str(wren_id),)).fetchone()
    return _native(surface, row[0]) if row else None


def surfaces_of(wren_id: int) -> list[str]:
    """The surfaces this contact has an id on, in SURFACES order. Empty for
    the owner, who is not a contact."""
    with db.conn() as con:
        row = con.execute(
            f"SELECT {', '.join(_col(s) for s in SURFACES)} FROM contacts WHERE {_WREN_ID}=?",
            (str(wren_id),)).fetchone()
    if not row:
        return []
    return [s for s, v in zip(SURFACES, row) if v is not None]
```

- [ ] **Step 4: Run the contacts tests, then the whole suite**

Run: `venv/bin/python3 -m pytest -q -p no:cacheprovider tests/test_contacts.py`
Expected: all pass.

Run: `venv/bin/python3 -m pytest -q -p no:cacheprovider`
Expected: everything passes except tests that assert the old digit-check reply in `tests/test_contacts_plugin.py` / `tests/test_webchat_plugins.py` -- if any fail there, note them for Task 2 and do not change them here. (Both doors still do their own digit check in this task, so the suite should be green.)

- [ ] **Step 5: Commit**

```bash
git add wren/contacts.py tests/test_contacts.py
git commit -m "feat(contacts): string ids per surface, slack and matrix columns, minted Wren ids"
```

---

### Task 2: Doors relay the store's refusals; the People tab learns the surfaces

**Files:**
- Modify: `wren/skills/contacts_skill.py` (the `add_contact` branch)
- Modify: `wren/communication/webchat.py` (`add_contact`, `get_me`)
- Modify: `wren/communication/chat.html` (`DOOR`, `loadPeople`, the `#padd` form hint)
- Modify: `tests/screens.py:103-104` (seed a Slack contact)
- Test: `tests/test_contacts_plugin.py`, `tests/test_webchat_plugins.py`

**Interfaces:**
- Consumes: `contacts.register`, `contacts.Invalid`, `contacts.Taken`, `contacts.SURFACES` (Task 1).
- Produces: `GET /api/me` body gains `"surfaces": list[str]`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_contacts_plugin.py`, replace `test_owner_add_contact_non_numeric_id` with:

```python
def test_owner_add_contact_non_numeric_id():
    ch = CollectingChannel()
    asyncio.run(contacts_plugin.handle("add_contact", Ctx(user_id=1, channel=ch, content="abc", person="hubby")))
    assert ch.sent == ["That doesn't look like a discord id."]

def test_owner_adds_a_slack_contact(monkeypatch):
    monkeypatch.setattr(config, "COMMUNICATION_PLUGINS", ["discord", "slack"])
    ch = CollectingChannel()
    _add(ch, "U0123ABCDEF", "kim", text="add U0123ABCDEF as kim on slack")
    _assert_flourished(ch.sent[-1], "Added kim on slack.")
    assert contacts.surfaces_of(contacts.all()["kim"]) == ["slack"]

def test_a_slack_shaped_id_on_discord_is_refused_with_the_surface_named(monkeypatch):
    monkeypatch.setattr(config, "COMMUNICATION_PLUGINS", ["discord", "slack"])
    ch = CollectingChannel()
    _add(ch, "U0123ABCDEF", "kim")
    assert ch.sent == ["That doesn't look like a discord id."]
```

(`_add`, `config` and `_assert_flourished` already exist in that file; `test_owner_adds_a_slack_contact` goes after `test_default_surface_is_the_first_configured_one_with_ids`.)

In `tests/test_webchat_plugins.py`, change the parametrised bad-bodies list so the two id-shape cases expect the store's message, and add the `/api/me` test:

```python
@pytest.mark.parametrize("body", [
    {"alias": "", "surface": "discord", "id": "1"},
    {"alias": "owner", "surface": "discord", "id": "1"},
    {"alias": "x", "surface": "sms", "id": "1"},
    {"alias": "x", "surface": "discord"},
    {"alias": 5, "surface": "discord", "id": "1"},
])
def test_add_contact_rejects_bad_bodies(body):
    status, out = call("post", "/api/contacts", token=TOKEN_OWNER, json=body)
    assert status == 400 and "error" in out


@pytest.mark.parametrize("surface,raw", [("discord", "abc"), ("discord", "²"),
                                         ("slack", "u0123abcdef"), ("matrix", "kim:home.net")])
def test_add_contact_relays_the_stores_shape_refusal(surface, raw):
    status, out = call("post", "/api/contacts", token=TOKEN_OWNER,
                       json={"alias": "x", "surface": surface, "id": raw})
    assert status == 400 and out["error"] == f"That doesn't look like a {surface} id."


def test_add_a_slack_contact_over_http():
    status, row = call("post", "/api/contacts", token=TOKEN_OWNER,
                       json={"alias": "kim", "surface": "slack", "id": "U0123ABCDEF"})
    assert status == 201
    assert row["slack"] == "U0123ABCDEF" and row["discord"] is None and row["matrix"] is None
    assert row["wren_id"] == str(contacts.mint("slack", "U0123ABCDEF"))


def test_me_lists_the_configured_surfaces_in_order(monkeypatch):
    monkeypatch.setattr(config, "COMMUNICATION_PLUGINS", ["http", "slack", "discord", "gmail"])
    status, body = call("get", "/api/me", token=TOKEN_OTHER)
    assert status == 200 and body["surfaces"] == ["slack", "discord"]
```

(If `contacts` is not imported at the top of `tests/test_webchat_plugins.py`, add `from wren import contacts`.) Keep the existing `{"alias": "x", "surface": "discord", "id": "abc"}` case out of the first list: it moved to the second test.

- [ ] **Step 2: Run them to see them fail**

Run: `venv/bin/python3 -m pytest -q -p no:cacheprovider tests/test_contacts_plugin.py tests/test_webchat_plugins.py`
Expected: the new and changed tests fail on the old reply/400 message and the missing `surfaces` key.

- [ ] **Step 3: Chat skill relays the store**

In `wren/skills/contacts_skill.py`, the `add_contact` branch becomes:

```python
    if intent == "add_contact":
        alias = (ctx.person or "").strip().lower()
        id_raw = (ctx.content or "").strip()
        if not alias:
            await ctx.channel.send("Who should I add?")
            return
        named = router.named_surface(ctx.text)
        surface = named if named in contacts.SURFACES else _default_surface()
        try:
            new = contacts.register(alias, surface, id_raw, reserved=config.whitelist())
        except (contacts.Invalid, contacts.Taken) as e:
            await ctx.channel.send(str(e))
            return
        await ctx.channel.send(flourish.flourish(
            f"Added {alias} on {surface}." if (named or not new) else f"Added {alias}."))
```

Also update `PROMPT_GUIDELINES`' `add_contact` line so the model does not insist on numbers: replace `content is the raw numeric user ID, person is the alias` with `content is the raw user ID exactly as given (a number, a Slack id like U0123ABCDEF, or a Matrix id like @kim:home.net), person is the alias`.

- [ ] **Step 4: HTTP route relays the store and `/api/me` lists surfaces**

In `wren/communication/webchat.py`, `add_contact` becomes:

```python
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
```

`_contact_row` already stringifies every surface via `contacts.SURFACES`; leave it. In `get_me`, add one key to the response dict:

```python
            # which apps a person can be added on here, for the People tab's
            # form: the configured chat plugins that hold ids of their own
            "surfaces": [s for s in config.COMMUNICATION_PLUGINS if s in contacts.SURFACES],
```

- [ ] **Step 5: The People tab reads the surfaces from `/api/me`**

In `wren/communication/chat.html`:

1. `DOOR` (line ~711) becomes `const DOOR = { telegram: "Telegram", discord: "Discord", slack: "Slack", matrix: "Matrix", voice: "voice" };`
2. Add a module-level `let surfaces = [];` next to `let myName = null;` and set it in `start()`'s `me.then(...)`: `surfaces = m.surfaces || [];`.
3. In `loadPeople()`, replace the hardcoded select fill and the row id list:

```js
  const sel = $("#psurface");
  if (!sel.options.length) {
    for (const s of (surfaces.length ? surfaces : ["discord", "telegram"])) {
      const o = document.createElement("option"); o.value = s; o.textContent = DOOR[s] || s; sel.append(o);
    }
    sel.onchange = () => { $("#pid").placeholder = PLACEHOLDER[sel.value] || "their id in that app"; };
    sel.onchange();
  }
```

and

```js
    row.querySelector(".pstate").textContent =
      Object.keys(DOOR).filter(s => p[s]).map(s => (DOOR[s] || s) + " " + p[s]).join(" · ") || "no app yet";
```

4. Add next to `DOOR`:

```js
// what an id looks like on each app, as the add form's hint
const PLACEHOLDER = { discord: "123456789012345678", telegram: "123456789",
                      slack: "U0123ABCDEF", matrix: "@name:home.net" };
```

5. Remove `inputmode="numeric"` from `#pid` (Slack and Matrix ids have letters). Change the hint paragraph under the form to: `Someone on two apps: add them once, then add the same name again with the other app and their id there. Their notes stay theirs whichever came first.`

6. In `tests/screens.py` after the `mum` seed line add: `_api("/api/contacts", {"alias": "kim", "surface": "slack", "id": "U0123ABCDEF"})`. The scratch instance there runs `COMMUNICATION_PLUGINS=http`, so `surfaces` is empty and the fallback pair fills the select; the People row still shows `Slack U0123ABCDEF`. Add to the People assertions near line 163: `assert "Slack U0123ABCDEF" in people`.

- [ ] **Step 6: Run the tests, the suite, and the screenshot smoke**

Run: `venv/bin/python3 -m pytest -q -p no:cacheprovider`
Expected: all pass.

Run: `python3 tests/screens.py /tmp/shots-doors` (system python3 has Playwright)
Expected: ends with `screens in /tmp/shots-doors`; look at `desktop-people.png` and confirm the row `kim` shows `Slack U0123ABCDEF`.

- [ ] **Step 7: Commit**

```bash
git add wren/skills/contacts_skill.py wren/communication/webchat.py wren/communication/chat.html tests/screens.py tests/test_contacts_plugin.py tests/test_webchat_plugins.py
git commit -m "feat(contacts): both doors relay the store's id shape; People tab lists the configured apps"
```

---

### Task 3: Settings and secrets for both doors

**Files:**
- Modify: `wren/config.py` (after `TELEGRAM_OWNER_ID`, and `SECRET_KEYS`)
- Modify: `manage.sh:19-21` (`SECRET_KEYS`)
- Modify: `.env.example` (communication section)
- Modify: `wren/run.py` (`_warn_if_notifications_go_nowhere`)
- Test: `tests/test_config.py` (append; create the file with the six setdefault lines if it does not exist), `tests/test_manage_sh.py` (append)

**Interfaces:**
- Produces: `config.SLACK_APP_TOKEN: str`, `config.SLACK_BOT_TOKEN: str`, `config.SLACK_OWNER_ID: str`, `config.MATRIX_HOMESERVER: str` (no trailing slash), `config.MATRIX_TOKEN: str`, `config.MATRIX_OWNER_ID: str`; `config.SECRET_KEYS` gains `SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`, `MATRIX_TOKEN`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py` (create it with the standard six `os.environ.setdefault` lines and `from wren import config` if absent):

```python
def test_new_door_settings_exist_and_default_empty():
    for key in ("SLACK_APP_TOKEN", "SLACK_BOT_TOKEN", "SLACK_OWNER_ID",
                "MATRIX_HOMESERVER", "MATRIX_TOKEN", "MATRIX_OWNER_ID"):
        assert isinstance(getattr(config, key), str)

def test_door_tokens_are_secrets_not_settings():
    for key in ("SLACK_APP_TOKEN", "SLACK_BOT_TOKEN", "MATRIX_TOKEN"):
        assert key in config.SECRET_KEYS and key not in config.SETTABLE
    for key in ("SLACK_OWNER_ID", "MATRIX_OWNER_ID", "MATRIX_HOMESERVER"):
        assert key not in config.SECRET_KEYS

def test_matrix_homeserver_loses_its_trailing_slash(monkeypatch):
    assert config._homeserver("https://matrix.example.org/") == "https://matrix.example.org"
    assert config._homeserver("") == ""
```

Append to `tests/test_manage_sh.py`:

```python
def test_manage_sh_secret_keys_match_config():
    import re
    from wren import config
    text = Path(SCRIPT).read_text()
    block = re.search(r"SECRET_KEYS=\((.*?)\)", text, re.S).group(1)
    assert set(block.split()) == set(config.SECRET_KEYS)
```

- [ ] **Step 2: Run them to see them fail**

Run: `venv/bin/python3 -m pytest -q -p no:cacheprovider tests/test_config.py tests/test_manage_sh.py`
Expected: the four new tests fail.

- [ ] **Step 3: config.py**

After the `TELEGRAM_OWNER_ID` line add:

```python
# Slack, Socket Mode: two tokens from the app's settings page. The app-level
# token (xapp-, scope connections:write) opens the websocket; the bot token
# (xoxb-) does everything else. slack_plugin.start() refuses to run without
# both. SLACK_OWNER_ID is your Slack member id (U...), mapped onto
# WREN_OWNER_ID both ways like TELEGRAM_OWNER_ID -- always needed when you
# use Slack, because a Wren id is a number and a Slack id is not.
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_OWNER_ID = os.environ.get("SLACK_OWNER_ID", "").strip()


def _homeserver(raw: str) -> str:
    """https://matrix.example.org, without a trailing slash: every path the
    plugin builds starts with one."""
    return raw.strip().rstrip("/")


# Matrix: the bot account's access token (from a login, or Element's
# Settings > Help & About), the homeserver it lives on, and your own id
# (@you:home.net) mapped onto WREN_OWNER_ID like the others.
MATRIX_HOMESERVER = _homeserver(os.environ.get("MATRIX_HOMESERVER", ""))
MATRIX_TOKEN = os.environ.get("MATRIX_TOKEN", "")
MATRIX_OWNER_ID = os.environ.get("MATRIX_OWNER_ID", "").strip()
```

Change `SECRET_KEYS` to:

```python
SECRET_KEYS = (
    "WREN_TOKENS", "DISCORD_TOKEN", "TELEGRAM_TOKEN", "SLACK_APP_TOKEN",
    "SLACK_BOT_TOKEN", "MATRIX_TOKEN", "IMAP_PASSWORD",
    "GITHUB_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY", "FIRECRAWL_API_KEY", "CALENDAR_URLS",
)
```

- [ ] **Step 4: manage.sh**

```bash
SECRET_KEYS=(DISCORD_TOKEN TELEGRAM_TOKEN SLACK_APP_TOKEN SLACK_BOT_TOKEN
             MATRIX_TOKEN WREN_TOKENS IMAP_PASSWORD
             GITHUB_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY
             OPENROUTER_API_KEY FIRECRAWL_API_KEY CALENDAR_URLS)
```

- [ ] **Step 5: .env.example**

In the `COMMUNICATION_PLUGINS` list add two lines after the telegram one:

```
#   slack   — DM the bot in Slack (needs SLACK_APP_TOKEN + SLACK_BOT_TOKEN) [chat]
#   matrix  — DM the bot on Matrix (needs MATRIX_HOMESERVER + MATRIX_TOKEN) [chat]
```

Change the `NOTIFY_VIA` comment line `#   telegram / discord — always that one, ...` to `#   telegram / discord / slack / matrix — always that one, unless a reminder names another`.

After the Telegram block add:

```
# ── Slack communication plugin (only when COMMUNICATION_PLUGINS includes 'slack') ──
# api.slack.com/apps -> Create New App (from scratch) -> Socket Mode: on, which
# mints an app-level token with connections:write -> OAuth & Permissions, bot
# scopes: chat:write, im:history, im:read, im:write, reactions:write,
# files:read, files:write -> Event Subscriptions: on, bot event message.im
# -> Install to Workspace. Wren dials out to Slack; no public URL needed.
# Your member id is under your profile's "..." menu, "Copy member ID".
#
# SLACK_APP_TOKEN=xapp-1-...
# SLACK_BOT_TOKEN=xoxb-...
# SLACK_OWNER_ID=U0123ABCDEF

# ── Matrix communication plugin (only when COMMUNICATION_PLUGINS includes 'matrix') ──
# Make an account for Wren on your homeserver and log it in once to get an
# access token (Element: Settings > Help & About > Access Token, or
# `curl -XPOST $HS/_matrix/client/v3/login -d '{"type":"m.login.password",
# "identifier":{"type":"m.id.user","user":"wren"},"password":"..."}'`).
# Wren opens an unencrypted direct room with you at startup and invites you;
# accept it there. Encrypted rooms are not read in this version.
#
# MATRIX_HOMESERVER=https://matrix.example.org
# MATRIX_TOKEN=syt_...
# MATRIX_OWNER_ID=@you:example.org
```

- [ ] **Step 6: run.py wording**

In `_warn_if_notifications_go_nowhere`, change `web_only` to end `"Add a chat plugin that can push (discord, telegram, slack, matrix) to COMMUNICATION_PLUGINS."` and the `DISCARDED` message's tail `"Add one that can push, e.g. discord or telegram, to COMMUNICATION_PLUGINS."` to `"Add one that can push (discord, telegram, slack, matrix) to COMMUNICATION_PLUGINS."`. Check `tests/test_run.py` (or wherever `web_only` is asserted: `grep -rn "only appear in the web chat" tests/`) still matches: those tests match on the first sentence, which is unchanged.

- [ ] **Step 7: Run the suite**

Run: `venv/bin/python3 -m pytest -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add wren/config.py manage.sh .env.example wren/run.py tests/test_config.py tests/test_manage_sh.py
git commit -m "feat(config): settings and secrets for the Slack and Matrix doors"
```

---

### Task 4: Slack door, the parts that do not loop

**Files:**
- Create: `wren/communication/slack_plugin.py`
- Test: `tests/test_slack_plugin.py` (create)

**Interfaces:**
- Consumes: `config.SLACK_APP_TOKEN/SLACK_BOT_TOKEN/SLACK_OWNER_ID` (Task 3); `contacts.wren_id("slack", str)`, `contacts.surface_id("slack", int) -> str | None` (Task 1); `chunking.chunks(text, limit)`; `filetypes.ACCEPTED`, `filetypes.MAX_BYTES`; `core.handle_message`.
- Produces: `ROLE`, `PLUGIN_NAME`, `CAN_NOTIFY`, `is_active()`, `inactive_reason()`, `class SlackError(RuntimeError)` with `.error: str`, `async _api(method: str, *, json: dict | None = None) -> dict` (returns the whole body), `_scrub(text) -> str`, `_incoming(event: dict) -> tuple[str, str, list[dict]] | None`, `_wren_user_id(slack_id: str) -> int | None`, `_slack_user_id(wren_id: int) -> str | None`, `class SlackChannel(channel_id: str, ts: str)` with `NAME = "slack"`, `async notify(user_id: int, text: str) -> bool`, `_MAX_MESSAGE_CHARS = 4000`, `_PERMANENT: frozenset[str]`. `start()` is Task 5.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_slack_plugin.py`:

```python
import os, asyncio, logging
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import AsyncMock

import aiohttp
import pytest

from wren import config, contacts, core, router
from wren.communication import slack_plugin

OWNER_SLACK = "U0OWNER0001"


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    router.reset()
    monkeypatch.setenv("WREN_DB", str(tmp_path / "wren.db"))
    contacts.init_db()
    monkeypatch.setattr(config, "SLACK_APP_TOKEN", "xapp-app-token")
    monkeypatch.setattr(config, "SLACK_BOT_TOKEN", "xoxb-bot-token")
    monkeypatch.setattr(config, "SLACK_OWNER_ID", OWNER_SLACK)
    yield
    router.reset()


def _event(**over) -> dict:
    e = {"type": "message", "channel_type": "im", "channel": "D0123CHAN",
         "user": "U0KIM000001", "text": "hello", "ts": "1760000000.000100"}
    e.update(over)
    return e


# ── module shape ───────────────────────────────────────────────────────────────

def test_module_declares_a_chat_door():
    assert slack_plugin.ROLE == "chat" and slack_plugin.CAN_NOTIFY is True
    assert slack_plugin.PLUGIN_NAME == "Slack"
    assert slack_plugin.SlackChannel.NAME == "slack"

def test_is_active_needs_both_tokens(monkeypatch):
    assert slack_plugin.is_active()
    monkeypatch.setattr(config, "SLACK_BOT_TOKEN", "")
    assert not slack_plugin.is_active()
    assert "SLACK_BOT_TOKEN" in slack_plugin.inactive_reason()


# ── _incoming ──────────────────────────────────────────────────────────────────

def test_incoming_dm_text():
    assert slack_plugin._incoming(_event()) == ("U0KIM000001", "hello", [])

@pytest.mark.parametrize("over", [
    {"channel_type": "channel"},
    {"bot_id": "B0123"},
    {"subtype": "message_changed"},
    {"subtype": "message_deleted"},
    {"type": "reaction_added"},
    {"user": None},
])
def test_incoming_ignores_edits_and_bots(over):
    assert slack_plugin._incoming(_event(**over)) is None

def test_incoming_file_share_wants_accepted_files_only():
    e = _event(subtype="file_share", text="look", files=[
        {"id": "F1", "name": "cat.jpg", "mimetype": "image/jpeg", "size": 1000,
         "url_private_download": "https://files.slack.com/cat.jpg"},
        {"id": "F2", "name": "clip.mp4", "mimetype": "video/mp4", "size": 1000,
         "url_private_download": "https://files.slack.com/clip.mp4"},
    ])
    user, text, wanted = slack_plugin._incoming(e)
    assert text == "look"
    assert [w["filename"] for w in wanted] == ["cat.jpg"]
    assert wanted[0]["url"] == "https://files.slack.com/cat.jpg" and wanted[0]["mime"] == "image/jpeg"

def test_incoming_with_nothing_to_say_is_none():
    assert slack_plugin._incoming(_event(text="")) is None


# ── id translation ─────────────────────────────────────────────────────────────

def test_the_owners_slack_id_becomes_their_wren_id():
    assert slack_plugin._wren_user_id(OWNER_SLACK) == 1
    assert slack_plugin._slack_user_id(1) == OWNER_SLACK

def test_a_contact_is_looked_up_and_a_stranger_is_none():
    contacts.register("kim", "slack", "U0KIM000001")
    wid = contacts.all()["kim"]
    assert slack_plugin._wren_user_id("U0KIM000001") == wid
    assert slack_plugin._slack_user_id(wid) == "U0KIM000001"
    assert slack_plugin._wren_user_id("U0STRANGER1") is None
    assert slack_plugin._slack_user_id(424242) is None


# ── _api ───────────────────────────────────────────────────────────────────────

def test_api_raises_slack_error_with_the_error_code(monkeypatch):
    class Resp:
        async def json(self): return {"ok": False, "error": "channel_not_found"}
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    class Session:
        def __init__(self, *a, **k): pass
        def post(self, url, **k):
            assert "xoxb" not in url
            assert k["headers"]["Authorization"] == "Bearer xoxb-bot-token"
            return Resp()
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    with pytest.raises(slack_plugin.SlackError) as info:
        asyncio.run(slack_plugin._api("chat.postMessage", json={"channel": "D1", "text": "x"}))
    assert info.value.error == "channel_not_found"
    assert "xoxb" not in str(info.value)

def test_transport_error_is_scrubbed_of_both_tokens(monkeypatch):
    class Session:
        def __init__(self, *a, **k): pass
        def post(self, url, **k):
            raise aiohttp.ClientError("boom xoxb-bot-token and xapp-app-token")
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    with pytest.raises(aiohttp.ClientError) as info:
        asyncio.run(slack_plugin._api("auth.test"))
    assert "xoxb" not in str(info.value) and "xapp" not in str(info.value)
    assert "<token>" in str(info.value)


# ── channel ────────────────────────────────────────────────────────────────────

def test_channel_send_posts_chunks_to_the_dm(monkeypatch):
    api = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(slack_plugin, "_api", api)
    ch = slack_plugin.SlackChannel("D0123CHAN", "1760000000.000100")
    asyncio.run(ch.send("a" * 4000 + "\n" + "b" * 10))
    assert [c.args[0] for c in api.await_args_list] == ["chat.postMessage", "chat.postMessage"]
    assert api.await_args_list[0].kwargs["json"] == {"channel": "D0123CHAN", "text": "a" * 4000}
    assert api.await_args_list[1].kwargs["json"]["text"] == "b" * 10

def test_channel_ack_adds_a_reaction_and_swallows_failure(monkeypatch, caplog):
    api = AsyncMock(side_effect=[{"ok": True}, slack_plugin.SlackError("reactions.add", "already_reacted")])
    monkeypatch.setattr(slack_plugin, "_api", api)
    ch = slack_plugin.SlackChannel("D0123CHAN", "1760000000.000100")
    asyncio.run(ch.ack("seen"))
    assert api.await_args_list[0].kwargs["json"] == {"channel": "D0123CHAN", "timestamp": "1760000000.000100", "name": "eyes"}
    with caplog.at_level(logging.WARNING):
        asyncio.run(ch.ack("done"))          # the error one
    assert "reactions.add" in caplog.text

def test_channel_send_card_falls_back_to_the_prose(monkeypatch):
    api = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(slack_plugin, "_api", api)
    ch = slack_plugin.SlackChannel("D0123CHAN", "1")
    asyncio.run(ch.send_card("shopping", {"items": []}, "The list is empty.", intent="recall_shopping"))
    assert api.await_args.kwargs["json"]["text"] == "The list is empty."

def test_channel_send_file_uses_the_three_call_upload(monkeypatch):
    calls = []
    async def fake_api(method, *, json=None):
        calls.append((method, json))
        if method == "files.getUploadURLExternal":
            return {"ok": True, "upload_url": "https://files.slack.com/upload/abc", "file_id": "F9"}
        return {"ok": True}
    put = AsyncMock()
    monkeypatch.setattr(slack_plugin, "_api", fake_api)
    monkeypatch.setattr(slack_plugin, "_upload_bytes", put)
    ch = slack_plugin.SlackChannel("D0123CHAN", "1")
    asyncio.run(ch.send_file(b"\x89PNG....", "cat.png"))
    assert calls[0] == ("files.getUploadURLExternal", {"filename": "cat.png", "length": 8})
    put.assert_awaited_once_with("https://files.slack.com/upload/abc", b"\x89PNG....")
    assert calls[1] == ("files.completeUploadExternal",
                        {"files": [{"id": "F9", "title": "cat.png"}], "channel_id": "D0123CHAN"})


# ── notify ─────────────────────────────────────────────────────────────────────

def test_notify_dms_the_owner_by_slack_id(monkeypatch):
    api = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(slack_plugin, "_api", api)
    assert asyncio.run(slack_plugin.notify(1, "dinner")) is True
    assert api.await_args.kwargs["json"] == {"channel": OWNER_SLACK, "text": "dinner"}

def test_notify_to_someone_not_on_slack_is_permanent(monkeypatch):
    api = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(slack_plugin, "_api", api)
    assert asyncio.run(slack_plugin.notify(424242, "dinner")) is False
    api.assert_not_awaited()

@pytest.mark.parametrize("error", ["channel_not_found", "user_not_found", "cannot_dm_bot",
                                   "user_disabled", "account_inactive"])
def test_notify_returns_false_on_permanent_failure(monkeypatch, error):
    monkeypatch.setattr(slack_plugin, "_api", AsyncMock(side_effect=slack_plugin.SlackError("chat.postMessage", error)))
    assert asyncio.run(slack_plugin.notify(1, "dinner")) is False

@pytest.mark.parametrize("exc", [slack_plugin.SlackError("chat.postMessage", "ratelimited"),
                                 slack_plugin.SlackError("chat.postMessage", "invalid_auth"),
                                 aiohttp.ClientError("reset")])
def test_notify_raises_on_transient_failure(monkeypatch, exc):
    monkeypatch.setattr(slack_plugin, "_api", AsyncMock(side_effect=exc))
    with pytest.raises(type(exc)):
        asyncio.run(slack_plugin.notify(1, "dinner"))

def test_notify_splits_too(monkeypatch):
    api = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(slack_plugin, "_api", api)
    asyncio.run(slack_plugin.notify(1, "x" * 4001))
    assert api.await_count == 2
```

- [ ] **Step 2: Run them to see them fail**

Run: `venv/bin/python3 -m pytest -q -p no:cacheprovider tests/test_slack_plugin.py`
Expected: `ModuleNotFoundError: wren.communication.slack_plugin`.

- [ ] **Step 3: Write the module (without `start`)**

Create `wren/communication/slack_plugin.py`:

```python
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
```

- [ ] **Step 4: Run the tests**

Run: `venv/bin/python3 -m pytest -q -p no:cacheprovider tests/test_slack_plugin.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add wren/communication/slack_plugin.py tests/test_slack_plugin.py
git commit -m "feat(slack): Web API client, DM parsing, channel and notify"
```

---

### Task 5: Slack door, the Socket Mode loop

**Files:**
- Modify: `wren/communication/slack_plugin.py` (append)
- Test: `tests/test_slack_plugin.py` (append)

**Interfaces:**
- Consumes: everything in Task 4.
- Produces: `async start()`, `async _handle_event(event: dict) -> None`, `async _run_once() -> None` (one websocket connection, returns on `disconnect`), `_is_stale(event) -> bool`, `_STALE_SECONDS = 30 * 60`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_slack_plugin.py`:

```python
# ── the loop ───────────────────────────────────────────────────────────────────

class FakeWs:
    """An async iterator of frames plus a send_json recorder, standing in for
    aiohttp's ClientWebSocketResponse."""
    def __init__(self, frames):
        self._frames = list(frames)
        self.sent = []
        self.closed = False
    def __aiter__(self): return self
    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        frame = self._frames.pop(0)
        if frame == "hang":
            await asyncio.sleep(3600)
        return aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, __import__("json").dumps(frame), None)
    async def send_json(self, data): self.sent.append(data)
    async def close(self): self.closed = True
    async def __aenter__(self): return self
    async def __aexit__(self, *a): await self.close(); return False


def _socket(monkeypatch, frames):
    ws = FakeWs(frames)
    opened = []
    async def fake_api(method, *, json=None, token=None):
        opened.append((method, token))
        return {"ok": True, "url": "wss://wss.slack.com/link/abc"}
    class Session:
        def __init__(self, *a, **k): pass
        def ws_connect(self, url, **k):
            assert url == "wss://wss.slack.com/link/abc"
            return ws
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(slack_plugin, "_api", fake_api)
    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    return ws, opened


def _envelope(event, envelope_id="env-1"):
    return {"type": "events_api", "envelope_id": envelope_id, "payload": {"event": event}}


def test_connection_is_opened_with_the_app_token(monkeypatch):
    ws, opened = _socket(monkeypatch, [{"type": "hello"}, {"type": "disconnect", "reason": "refresh_requested"}])
    asyncio.run(slack_plugin._run_once())
    assert opened == [("apps.connections.open", "xapp-app-token")]
    assert ws.closed


def test_envelope_is_acked_before_the_message_is_handled(monkeypatch):
    order = []
    ws, _ = _socket(monkeypatch, [_envelope(_event(user=OWNER_SLACK)), {"type": "disconnect"}])
    original_send = ws.send_json
    async def send_json(data):
        order.append(("ack", data["envelope_id"])); await original_send(data)
    ws.send_json = send_json
    async def handle(user_id, text, channel, **kw):
        order.append(("handle", user_id, text, channel.NAME))
    monkeypatch.setattr(core, "handle_message", handle)
    asyncio.run(slack_plugin._run_once())
    assert order == [("ack", "env-1"), ("handle", 1, "hello", "slack")]


def test_a_stranger_is_acked_and_dropped(monkeypatch):
    ws, _ = _socket(monkeypatch, [_envelope(_event(user="U0STRANGER1")), {"type": "disconnect"}])
    handle = AsyncMock()
    monkeypatch.setattr(core, "handle_message", handle)
    asyncio.run(slack_plugin._run_once())
    assert ws.sent == [{"envelope_id": "env-1"}]
    handle.assert_not_awaited()


def test_a_failed_message_does_not_end_the_connection(monkeypatch, caplog):
    ws, _ = _socket(monkeypatch, [_envelope(_event(user=OWNER_SLACK), "env-1"),
                                  _envelope(_event(user=OWNER_SLACK, text="again"), "env-2"),
                                  {"type": "disconnect"}])
    handle = AsyncMock(side_effect=[RuntimeError("brain down"), None])
    monkeypatch.setattr(core, "handle_message", handle)
    with caplog.at_level(logging.ERROR):
        asyncio.run(slack_plugin._run_once())
    assert handle.await_count == 2
    assert "brain down" in caplog.text and "hello" not in caplog.text   # never the body


def test_stale_event_gets_the_offline_notice(monkeypatch):
    old = _event(user=OWNER_SLACK, ts=str(time.time() - 3600))
    ws, _ = _socket(monkeypatch, [_envelope(old), {"type": "disconnect"}])
    handle = AsyncMock()
    sent = AsyncMock()
    monkeypatch.setattr(core, "handle_message", handle)
    monkeypatch.setattr(slack_plugin, "_send_text", sent)
    asyncio.run(slack_plugin._run_once())
    handle.assert_not_awaited()
    assert sent.await_args.args[0] == "D0123CHAN" and "offline" in sent.await_args.args[1]


def test_files_are_downloaded_and_handed_to_core(monkeypatch):
    e = _event(user=OWNER_SLACK, subtype="file_share", text="keep this", files=[
        {"id": "F1", "name": "cat.jpg", "mimetype": "image/jpeg", "size": 10,
         "url_private_download": "https://files.slack.com/cat.jpg"}])
    ws, _ = _socket(monkeypatch, [_envelope(e), {"type": "disconnect"}])
    monkeypatch.setattr(slack_plugin, "_download", AsyncMock(return_value=b"jpegbytes"))
    handle = AsyncMock()
    monkeypatch.setattr(core, "handle_message", handle)
    asyncio.run(slack_plugin._run_once())
    files = handle.await_args.kwargs["files"]
    assert [(f.filename, f.mime, f.data) for f in files] == [("cat.jpg", "image/jpeg", b"jpegbytes")]


def test_an_oversize_file_is_refused_in_the_dm(monkeypatch):
    e = _event(user=OWNER_SLACK, subtype="file_share", text="", files=[
        {"id": "F1", "name": "big.pdf", "mimetype": "application/pdf", "size": 11 * 1024 * 1024,
         "url_private_download": "https://files.slack.com/big.pdf"}])
    ws, _ = _socket(monkeypatch, [_envelope(e), {"type": "disconnect"}])
    sent = AsyncMock()
    handle = AsyncMock()
    monkeypatch.setattr(slack_plugin, "_send_text", sent)
    monkeypatch.setattr(core, "handle_message", handle)
    asyncio.run(slack_plugin._run_once())
    assert "too big" in sent.await_args.args[1]
    handle.assert_not_awaited()


def test_start_raises_when_a_token_is_unset(monkeypatch):
    monkeypatch.setattr(config, "SLACK_APP_TOKEN", "")
    with pytest.raises(RuntimeError, match="SLACK_APP_TOKEN"):
        asyncio.run(slack_plugin.start())


def test_start_registers_itself_and_reconnects(monkeypatch):
    runs = []
    async def run_once():
        runs.append(1)
        await asyncio.sleep(0.01)
    monkeypatch.setattr(slack_plugin, "_run_once", run_once)
    async def _run():
        task = asyncio.create_task(slack_plugin.start())
        await asyncio.sleep(0.05)
        registered = router.registered()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return registered
    assert "slack" in asyncio.run(_run())
    assert len(runs) >= 2                    # a clean disconnect reconnects at once


def test_transient_error_does_not_escape_start(monkeypatch):
    monkeypatch.setattr(slack_plugin, "_RETRY_SECONDS", 0.001)
    run_once = AsyncMock(side_effect=aiohttp.ClientError("reset"))
    monkeypatch.setattr(slack_plugin, "_run_once", run_once)
    async def _run():
        task = asyncio.create_task(slack_plugin.start())
        await asyncio.sleep(0.05)
        alive = not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return alive, run_once.await_count
    alive, attempts = asyncio.run(_run())
    assert alive and attempts >= 2
```

Add `import time` to the test file's imports.

- [ ] **Step 2: Run them to see them fail**

Run: `venv/bin/python3 -m pytest -q -p no:cacheprovider tests/test_slack_plugin.py`
Expected: the new tests fail with `AttributeError: ... '_run_once'` / `'start'`.

- [ ] **Step 3: Append the loop to the module**

```python
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
    # Awaited inline so replies stay in order; see telegram_plugin for why
    # not a task.
    await core.handle_message(user_id, text, SlackChannel(channel_id, event.get("ts", "")), files=files)


async def _run_once() -> None:
    """One websocket connection: open it, serve envelopes until Slack says
    disconnect (it does so routinely to rotate hosts), then return so
    start() opens a fresh one."""
    body = await _api("apps.connections.open", token=config.SLACK_APP_TOKEN)
    url = body["url"]
    timeout = aiohttp.ClientTimeout(total=None)   # the socket lives until Slack closes it
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.ws_connect(url, heartbeat=30) as ws:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        return
                    continue
                frame = msg.json()
                kind = frame.get("type")
                if kind == "disconnect":
                    return
                envelope_id = frame.get("envelope_id")
                if envelope_id:
                    # Acked first, always: Slack retries an envelope it has
                    # not seen acked within about 3 s, and the brain can
                    # take longer than that -- an ack after handling would
                    # answer the same message twice.
                    await ws.send_json({"envelope_id": envelope_id})
                if kind != "events_api":
                    continue                     # hello, slash_commands, interactive: nothing to do
                event = (frame.get("payload") or {}).get("event") or {}
                try:
                    await _handle_event(event)
                except Exception as e:
                    # Never the body: DMs are as private as reminders.
                    logging.error(f"slack: handling event {envelope_id} from "
                                  f"{event.get('user')} failed: {_scrub(str(e))}")


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
```

- [ ] **Step 4: Run the Slack tests and the suite**

Run: `venv/bin/python3 -m pytest -q -p no:cacheprovider tests/test_slack_plugin.py`
Expected: all pass.

Run: `venv/bin/python3 -m pytest -q -p no:cacheprovider`
Expected: all pass. (`tests/test_webchat_plugins.py` lists channels by discovering `*_plugin` modules; a new one appears with `is_active()` false in tests, which is fine.)

- [ ] **Step 5: Commit**

```bash
git add wren/communication/slack_plugin.py tests/test_slack_plugin.py
git commit -m "feat(slack): Socket Mode loop -- ack first, reconnect, stale guard, files"
```

---

### Task 6: Matrix door, the parts that do not loop

**Files:**
- Create: `wren/communication/matrix_plugin.py`
- Test: `tests/test_matrix_plugin.py` (create)

**Interfaces:**
- Consumes: `config.MATRIX_HOMESERVER/MATRIX_TOKEN/MATRIX_OWNER_ID` (Task 3); `contacts.wren_id("matrix", str)`, `contacts.surface_id("matrix", int) -> str | None`, `contacts.all()` (Task 1); `chunking.chunks`; `filetypes.IMAGE_MIMES`.
- Produces: `ROLE`, `PLUGIN_NAME`, `CAN_NOTIFY`, `is_active()`, `inactive_reason()`, `class MatrixError(RuntimeError)` with `.errcode: str` and `.status: int`, `async _api(method, path, *, json=None, params=None, data=None, content_type=None) -> dict`, `_scrub`, `_me: str` (module global, set by `start`), `_rooms: dict[str, set[str]]` (room id → joined members), `_wren_user_id(matrix_id) -> int | None`, `_matrix_user_id(wren_id) -> str | None`, `async _dm_room(matrix_id: str) -> str`, `class MatrixChannel(room_id: str)` with `NAME = "matrix"`, `async notify(user_id, text) -> bool`, `_MAX_MESSAGE_CHARS = 32000`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_matrix_plugin.py`:

```python
import os, asyncio, logging, time
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import AsyncMock

import aiohttp
import pytest

from wren import config, contacts, core, router
from wren.communication import matrix_plugin

ME = "@wren:home.net"
OWNER = "@erin:home.net"
KIM = "@kim:home.net"


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    router.reset()
    monkeypatch.setenv("WREN_DB", str(tmp_path / "wren.db"))
    contacts.init_db()
    monkeypatch.setattr(config, "MATRIX_HOMESERVER", "https://home.net")
    monkeypatch.setattr(config, "MATRIX_TOKEN", "syt_secret_token")
    monkeypatch.setattr(config, "MATRIX_OWNER_ID", OWNER)
    monkeypatch.setattr(matrix_plugin, "_me", ME)
    monkeypatch.setattr(matrix_plugin, "_rooms", {})
    yield
    router.reset()


class Recorder:
    """A fake _api that records calls and answers from a dict keyed by
    (method, path-prefix)."""
    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}
    async def __call__(self, method, path, *, json=None, params=None, data=None, content_type=None):
        self.calls.append((method, path, json, params))
        for (m, prefix), value in self.answers.items():
            if m == method and path.startswith(prefix):
                if isinstance(value, Exception):
                    raise value
                return value
        return {}


# ── module shape ───────────────────────────────────────────────────────────────

def test_module_declares_a_chat_door():
    assert matrix_plugin.ROLE == "chat" and matrix_plugin.CAN_NOTIFY is True
    assert matrix_plugin.PLUGIN_NAME == "Matrix"
    assert matrix_plugin.MatrixChannel.NAME == "matrix"

def test_is_active_needs_homeserver_and_token(monkeypatch):
    assert matrix_plugin.is_active()
    monkeypatch.setattr(config, "MATRIX_TOKEN", "")
    assert not matrix_plugin.is_active()
    assert "MATRIX_TOKEN" in matrix_plugin.inactive_reason()


# ── id translation ─────────────────────────────────────────────────────────────

def test_the_owners_matrix_id_becomes_their_wren_id():
    assert matrix_plugin._wren_user_id(OWNER) == 1
    assert matrix_plugin._matrix_user_id(1) == OWNER

def test_a_contact_is_looked_up_and_a_stranger_is_none():
    contacts.register("kim", "matrix", KIM)
    wid = contacts.all()["kim"]
    assert matrix_plugin._wren_user_id(KIM) == wid
    assert matrix_plugin._matrix_user_id(wid) == KIM
    assert matrix_plugin._wren_user_id("@who:else.org") is None
    assert matrix_plugin._matrix_user_id(424242) is None


# ── _api ───────────────────────────────────────────────────────────────────────

def test_api_raises_matrix_error_with_errcode_and_status(monkeypatch):
    class Resp:
        status = 403
        async def json(self): return {"errcode": "M_FORBIDDEN", "error": "not in room"}
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    class Session:
        def __init__(self, *a, **k): pass
        def request(self, method, url, **k):
            assert url == "https://home.net/_matrix/client/v3/rooms/!r:home.net/send/m.room.message/t1"
            assert "syt_" not in url
            assert k["headers"]["Authorization"] == "Bearer syt_secret_token"
            return Resp()
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    with pytest.raises(matrix_plugin.MatrixError) as info:
        asyncio.run(matrix_plugin._api("PUT", "/_matrix/client/v3/rooms/!r:home.net/send/m.room.message/t1", json={}))
    assert (info.value.errcode, info.value.status) == ("M_FORBIDDEN", 403)
    assert "syt_" not in str(info.value)

def test_transport_error_is_scrubbed_of_the_token(monkeypatch):
    class Session:
        def __init__(self, *a, **k): pass
        def request(self, method, url, **k):
            raise aiohttp.ClientError("boom syt_secret_token")
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    with pytest.raises(aiohttp.ClientError) as info:
        asyncio.run(matrix_plugin._api("GET", "/_matrix/client/v3/account/whoami"))
    assert "syt_" not in str(info.value) and "<token>" in str(info.value)


# ── rooms ──────────────────────────────────────────────────────────────────────

def test_dm_room_reuses_the_two_member_room_with_that_person(monkeypatch):
    matrix_plugin._rooms.update({"!a:home.net": {ME, OWNER}, "!b:home.net": {ME, OWNER, KIM}})
    api = Recorder()
    monkeypatch.setattr(matrix_plugin, "_api", api)
    assert asyncio.run(matrix_plugin._dm_room(OWNER)) == "!a:home.net"
    assert api.calls == []

def test_dm_room_creates_an_unencrypted_direct_room_and_caches_it(monkeypatch):
    api = Recorder({("POST", "/_matrix/client/v3/createRoom"): {"room_id": "!new:home.net"}})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    assert asyncio.run(matrix_plugin._dm_room(KIM)) == "!new:home.net"
    method, path, body, _ = api.calls[0]
    assert body == {"is_direct": True, "invite": [KIM], "preset": "trusted_private_chat"}
    assert matrix_plugin._rooms["!new:home.net"] == {ME, KIM}


# ── channel ────────────────────────────────────────────────────────────────────

def test_channel_send_puts_text_events_in_chunks(monkeypatch):
    api = Recorder()
    monkeypatch.setattr(matrix_plugin, "_api", api)
    ch = matrix_plugin.MatrixChannel("!r:home.net")
    asyncio.run(ch.send("a" * 32000 + "\n" + "b"))
    assert len(api.calls) == 2
    method, path, body, _ = api.calls[0]
    assert method == "PUT" and path.startswith("/_matrix/client/v3/rooms/!r:home.net/send/m.room.message/")
    assert body == {"msgtype": "m.text", "body": "a" * 32000}
    assert api.calls[0][1] != api.calls[1][1]          # a fresh txn id per event

def test_channel_send_file_uploads_then_sends_an_image_or_file_event(monkeypatch):
    api = Recorder({("POST", "/_matrix/media/v3/upload"): {"content_uri": "mxc://home.net/abc"}})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    ch = matrix_plugin.MatrixChannel("!r:home.net")
    asyncio.run(ch.send_file(b"\x89PNG\r\n\x1a\n" + b"0" * 20, "cat.png"))
    assert api.calls[0][3] == {"filename": "cat.png"}
    assert api.calls[1][2] == {"msgtype": "m.image", "body": "cat.png", "url": "mxc://home.net/abc",
                               "info": {"mimetype": "image/png", "size": 28}}
    asyncio.run(ch.send_file(b"%PDF-1.4 ...", "doc.pdf"))
    assert api.calls[3][2]["msgtype"] == "m.file" and api.calls[3][2]["info"]["mimetype"] == "application/pdf"

def test_channel_ack_seen_sets_typing_and_swallows_failure(monkeypatch, caplog):
    api = Recorder({("PUT", "/_matrix/client/v3/rooms/!r:home.net/typing/"): matrix_plugin.MatrixError("M_LIMIT_EXCEEDED", 429)})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    ch = matrix_plugin.MatrixChannel("!r:home.net")
    with caplog.at_level(logging.WARNING):
        asyncio.run(ch.ack("seen"))
    assert api.calls[0][1] == f"/_matrix/client/v3/rooms/!r:home.net/typing/{ME}"
    assert api.calls[0][2] == {"typing": True, "timeout": 30000}
    assert "typing" in caplog.text
    asyncio.run(ch.ack("done"))
    assert len(api.calls) == 1

def test_channel_send_card_falls_back_to_the_prose(monkeypatch):
    api = Recorder()
    monkeypatch.setattr(matrix_plugin, "_api", api)
    asyncio.run(matrix_plugin.MatrixChannel("!r:home.net").send_card("shopping", {}, "The list is empty."))
    assert api.calls[0][2]["body"] == "The list is empty."


# ── notify ─────────────────────────────────────────────────────────────────────

def test_notify_sends_into_the_owners_dm_room(monkeypatch):
    matrix_plugin._rooms["!a:home.net"] = {ME, OWNER}
    api = Recorder()
    monkeypatch.setattr(matrix_plugin, "_api", api)
    assert asyncio.run(matrix_plugin.notify(1, "dinner")) is True
    assert api.calls[0][1].startswith("/_matrix/client/v3/rooms/!a:home.net/send/")
    assert api.calls[0][2]["body"] == "dinner"

def test_notify_reuses_the_room_it_created(monkeypatch):
    contacts.register("kim", "matrix", KIM)
    wid = contacts.all()["kim"]
    api = Recorder({("POST", "/_matrix/client/v3/createRoom"): {"room_id": "!new:home.net"}})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    assert asyncio.run(matrix_plugin.notify(wid, "one")) is True
    assert asyncio.run(matrix_plugin.notify(wid, "two")) is True
    creates = [c for c in api.calls if c[1].endswith("/createRoom")]
    assert len(creates) == 1

def test_notify_to_someone_not_on_matrix_is_permanent(monkeypatch):
    api = Recorder()
    monkeypatch.setattr(matrix_plugin, "_api", api)
    assert asyncio.run(matrix_plugin.notify(424242, "dinner")) is False
    assert api.calls == []

def test_notify_returns_false_when_forbidden_and_raises_otherwise(monkeypatch):
    matrix_plugin._rooms["!a:home.net"] = {ME, OWNER}
    api = Recorder({("PUT", "/_matrix/client/v3/rooms/!a:home.net/send/"): matrix_plugin.MatrixError("M_FORBIDDEN", 403)})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    assert asyncio.run(matrix_plugin.notify(1, "dinner")) is False
    api.answers = {("PUT", "/_matrix/client/v3/rooms/!a:home.net/send/"): matrix_plugin.MatrixError("M_LIMIT_EXCEEDED", 429)}
    with pytest.raises(matrix_plugin.MatrixError):
        asyncio.run(matrix_plugin.notify(1, "dinner"))
```

- [ ] **Step 2: Run them to see them fail**

Run: `venv/bin/python3 -m pytest -q -p no:cacheprovider tests/test_matrix_plugin.py`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write the module (without the loop)**

Create `wren/communication/matrix_plugin.py`:

```python
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
```

- [ ] **Step 4: Run the tests**

Run: `venv/bin/python3 -m pytest -q -p no:cacheprovider tests/test_matrix_plugin.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add wren/communication/matrix_plugin.py tests/test_matrix_plugin.py
git commit -m "feat(matrix): client, direct rooms, channel and notify"
```

---

### Task 7: Matrix door, the sync loop

**Files:**
- Modify: `wren/communication/matrix_plugin.py` (append)
- Test: `tests/test_matrix_plugin.py` (append)

**Interfaces:**
- Consumes: Task 6.
- Produces: `async start()`, `async _sync_once(since: str | None) -> str` (returns `next_batch`; acts on nothing when `since` is None), `_learn_rooms(rooms: dict) -> None`, `_incoming(event: dict) -> tuple[str, str, list[dict]] | None`, `_is_stale(event) -> bool`, `_STALE_SECONDS = 30 * 60`, `_encrypted: set[str]` (rooms warned about once), `_SYNC_FILTER: str`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_matrix_plugin.py`:

```python
# ── the loop ───────────────────────────────────────────────────────────────────

def _member(user, membership="join", sender=None):
    return {"type": "m.room.member", "state_key": user, "sender": sender or user,
            "content": {"membership": membership}}

def _msg(sender, body, ts=None, **content):
    return {"type": "m.room.message", "sender": sender, "event_id": "$" + body[:8],
            "origin_server_ts": int((ts or time.time()) * 1000),
            "content": {"msgtype": "m.text", "body": body, **content}}

def _sync(joined=None, invited=None, next_batch="s2"):
    return {"next_batch": next_batch, "rooms": {"join": joined or {}, "invite": invited or {}}}

def _joined(room, members, events):
    return {room: {"state": {"events": [_member(m) for m in members]}, "timeline": {"events": events}}}


def test_incoming_text_and_media():
    assert matrix_plugin._incoming(_msg(OWNER, "hi")) == (OWNER, "hi", [])
    img = _msg(OWNER, "cat.jpg", msgtype="m.image", url="mxc://home.net/abc",
               info={"mimetype": "image/jpeg", "size": 10})
    assert matrix_plugin._incoming(img) == (OWNER, "", [{"url": "mxc://home.net/abc", "filename": "cat.jpg",
                                                         "mime": "image/jpeg", "size": 10}])
    cap = _msg(OWNER, "look at this", msgtype="m.image", url="mxc://home.net/abc", filename="cat.jpg",
               info={"mimetype": "image/jpeg", "size": 10})
    assert matrix_plugin._incoming(cap)[1] == "look at this"

@pytest.mark.parametrize("event", [
    _msg(ME, "my own echo"),
    {"type": "m.room.encrypted", "sender": OWNER, "content": {}},
    {"type": "m.reaction", "sender": OWNER, "content": {}},
    _msg(OWNER, "clip", msgtype="m.video", url="mxc://home.net/v", info={"mimetype": "video/mp4"}),
])
def test_incoming_ignores_what_it_cannot_act_on(event):
    assert matrix_plugin._incoming(event) is None


def test_initial_sync_acts_on_nothing_but_learns_rooms(monkeypatch):
    api = Recorder({("GET", f"{matrix_plugin.V3}/sync"): _sync(
        _joined("!a:home.net", [ME, OWNER], [_msg(OWNER, "old news")]), next_batch="s1")})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    handle = AsyncMock()
    monkeypatch.setattr(core, "handle_message", handle)
    assert asyncio.run(matrix_plugin._sync_once(None)) == "s1"
    handle.assert_not_awaited()
    assert matrix_plugin._rooms == {"!a:home.net": {ME, OWNER}}
    assert "since" not in (api.calls[0][3] or {})


def test_a_dm_message_reaches_core_with_the_room_channel(monkeypatch):
    matrix_plugin._rooms["!a:home.net"] = {ME, OWNER}
    api = Recorder({("GET", f"{matrix_plugin.V3}/sync"): _sync(
        _joined("!a:home.net", [ME, OWNER], [_msg(OWNER, "add milk")]))})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    handle = AsyncMock()
    monkeypatch.setattr(core, "handle_message", handle)
    assert asyncio.run(matrix_plugin._sync_once("s1")) == "s2"
    assert api.calls[0][3]["since"] == "s1" and api.calls[0][3]["timeout"] == 30000
    args = handle.await_args
    assert args.args[0] == 1 and args.args[1] == "add milk" and args.args[2].NAME == "matrix"
    assert args.args[2]._room == "!a:home.net"


def test_a_group_room_and_a_stranger_are_ignored(monkeypatch):
    api = Recorder({("GET", f"{matrix_plugin.V3}/sync"): _sync({
        **_joined("!g:home.net", [ME, OWNER, KIM], [_msg(OWNER, "group chat")]),
        **_joined("!s:home.net", [ME, "@who:else.org"], [_msg("@who:else.org", "hey")]),
    })})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    handle = AsyncMock()
    monkeypatch.setattr(core, "handle_message", handle)
    asyncio.run(matrix_plugin._sync_once("s1"))
    handle.assert_not_awaited()


def test_invites_from_known_people_are_joined_and_others_ignored(monkeypatch):
    contacts.register("kim", "matrix", KIM)
    invited = {
        "!k:home.net": {"invite_state": {"events": [_member(ME, "invite", sender=KIM)]}},
        "!x:home.net": {"invite_state": {"events": [_member(ME, "invite", sender="@who:else.org")]}},
    }
    api = Recorder({("GET", f"{matrix_plugin.V3}/sync"): _sync(invited=invited)})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    asyncio.run(matrix_plugin._sync_once("s1"))
    joins = [c[1] for c in api.calls if c[0] == "POST" and "/join/" in c[1]]
    assert joins == [f"{matrix_plugin.V3}/rooms/!k:home.net/join"]


def test_an_encrypted_room_is_skipped_and_logged_once(monkeypatch, caplog):
    matrix_plugin._encrypted.clear()
    room = _joined("!e:home.net", [ME, OWNER], [
        {"type": "m.room.encrypted", "sender": OWNER, "content": {}},
        {"type": "m.room.encrypted", "sender": OWNER, "content": {}}])
    api = Recorder({("GET", f"{matrix_plugin.V3}/sync"): _sync(room)})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    handle = AsyncMock()
    monkeypatch.setattr(core, "handle_message", handle)
    with caplog.at_level(logging.WARNING):
        asyncio.run(matrix_plugin._sync_once("s1"))
        asyncio.run(matrix_plugin._sync_once("s2"))
    handle.assert_not_awaited()
    assert caplog.text.count("encrypted") == 1


def test_stale_message_gets_the_offline_notice(monkeypatch):
    matrix_plugin._rooms["!a:home.net"] = {ME, OWNER}
    api = Recorder({("GET", f"{matrix_plugin.V3}/sync"): _sync(
        _joined("!a:home.net", [ME, OWNER], [_msg(OWNER, "ages ago", ts=time.time() - 3600)]))})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    handle = AsyncMock(); sent = AsyncMock()
    monkeypatch.setattr(core, "handle_message", handle)
    monkeypatch.setattr(matrix_plugin, "_send_text", sent)
    asyncio.run(matrix_plugin._sync_once("s1"))
    handle.assert_not_awaited()
    assert sent.await_args.args[0] == "!a:home.net" and "offline" in sent.await_args.args[1]


def test_media_is_downloaded_and_handed_to_core(monkeypatch):
    matrix_plugin._rooms["!a:home.net"] = {ME, OWNER}
    img = _msg(OWNER, "cat.jpg", msgtype="m.image", url="mxc://home.net/abc", info={"mimetype": "image/jpeg", "size": 10})
    api = Recorder({("GET", f"{matrix_plugin.V3}/sync"): _sync(_joined("!a:home.net", [ME, OWNER], [img]))})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    monkeypatch.setattr(matrix_plugin, "_download", AsyncMock(return_value=b"jpegbytes"))
    handle = AsyncMock()
    monkeypatch.setattr(core, "handle_message", handle)
    asyncio.run(matrix_plugin._sync_once("s1"))
    files = handle.await_args.kwargs["files"]
    assert [(f.filename, f.mime, f.data) for f in files] == [("cat.jpg", "image/jpeg", b"jpegbytes")]


def test_a_failed_message_does_not_stall_the_batch(monkeypatch, caplog):
    matrix_plugin._rooms["!a:home.net"] = {ME, OWNER}
    api = Recorder({("GET", f"{matrix_plugin.V3}/sync"): _sync(
        _joined("!a:home.net", [ME, OWNER], [_msg(OWNER, "first"), _msg(OWNER, "second")]))})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    handle = AsyncMock(side_effect=[RuntimeError("brain down"), None])
    monkeypatch.setattr(core, "handle_message", handle)
    with caplog.at_level(logging.ERROR):
        assert asyncio.run(matrix_plugin._sync_once("s1")) == "s2"
    assert handle.await_count == 2
    assert "brain down" in caplog.text and "first" not in caplog.text


def test_start_raises_without_settings(monkeypatch):
    monkeypatch.setattr(config, "MATRIX_TOKEN", "")
    with pytest.raises(RuntimeError, match="MATRIX_TOKEN"):
        asyncio.run(matrix_plugin.start())


def test_start_learns_who_it_is_opens_the_owners_room_and_registers(monkeypatch):
    monkeypatch.setattr(matrix_plugin, "_me", "")
    api = Recorder({("GET", f"{matrix_plugin.V3}/account/whoami"): {"user_id": ME},
                    ("POST", f"{matrix_plugin.V3}/createRoom"): {"room_id": "!o:home.net"}})
    monkeypatch.setattr(matrix_plugin, "_api", api)
    async def idle(since):
        await asyncio.sleep(0.01)
        return since or "s1"
    monkeypatch.setattr(matrix_plugin, "_sync_once", idle)
    async def _run():
        task = asyncio.create_task(matrix_plugin.start())
        await asyncio.sleep(0.05)
        registered = router.registered()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return registered
    assert "matrix" in asyncio.run(_run())
    assert matrix_plugin._me == ME
    assert matrix_plugin._rooms == {"!o:home.net": {ME, OWNER}}


def test_start_feeds_each_since_into_the_next_sync(monkeypatch):
    monkeypatch.setattr(matrix_plugin, "_rooms", {"!o:home.net": {ME, OWNER}})
    seen = []
    second = asyncio.Event()
    async def fake_sync(since):
        seen.append(since)
        if len(seen) == 2:
            second.set()
            await asyncio.sleep(3600)
        return f"s{len(seen)}"
    monkeypatch.setattr(matrix_plugin, "_sync_once", fake_sync)
    monkeypatch.setattr(matrix_plugin, "_api", Recorder({("GET", f"{matrix_plugin.V3}/account/whoami"): {"user_id": ME}}))
    async def _run():
        task = asyncio.create_task(matrix_plugin.start())
        await asyncio.wait_for(second.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(_run())
    assert seen == [None, "s1"]


def test_transient_error_does_not_escape_start(monkeypatch):
    monkeypatch.setattr(matrix_plugin, "_RETRY_SECONDS", 0.001)
    monkeypatch.setattr(matrix_plugin, "_api", Recorder({("GET", f"{matrix_plugin.V3}/account/whoami"): {"user_id": ME}}))
    monkeypatch.setattr(matrix_plugin, "_rooms", {"!o:home.net": {ME, OWNER}})
    sync = AsyncMock(side_effect=aiohttp.ClientError("reset"))
    monkeypatch.setattr(matrix_plugin, "_sync_once", sync)
    async def _run():
        task = asyncio.create_task(matrix_plugin.start())
        await asyncio.sleep(0.05)
        alive = not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return alive, sync.await_count
    alive, attempts = asyncio.run(_run())
    assert alive and attempts >= 2
```

- [ ] **Step 2: Run them to see them fail**

Run: `venv/bin/python3 -m pytest -q -p no:cacheprovider tests/test_matrix_plugin.py`
Expected: the new tests fail with `AttributeError` on `_incoming`, `_sync_once`, `start`, `_encrypted`.

- [ ] **Step 3: Append the loop to the module**

```python
# What /sync should carry: timeline and member state, lazily loaded so a
# room's members arrive without every presence and receipt in the server.
_SYNC_FILTER = _json.dumps({
    "room": {"timeline": {"limit": 50}, "state": {"lazy_load_members": True},
             "ephemeral": {"types": []}, "account_data": {"types": []}},
    "presence": {"types": []}, "account_data": {"types": []},
})
# A restart replays whatever the initial sync carries and anything missed
# since the last token (process-local, as telegram_plugin's offset is).
# Acting on a message hours late is worse than not acting.
_STALE_SECONDS = 30 * 60
_encrypted: set[str] = set()          # rooms already warned about


def _is_stale(event: dict) -> bool:
    ts = event.get("origin_server_ts")
    return isinstance(ts, (int, float)) and time.time() - ts / 1000 > _STALE_SECONDS


def _learn_rooms(rooms: dict) -> None:
    """Fold a sync's membership into _rooms. State events carry the members
    who were already there; timeline events carry joins and leaves since."""
    for room_id, room in (rooms.get("join") or {}).items():
        members = _rooms.setdefault(room_id, set())
        events = list((room.get("state") or {}).get("events") or []) + \
                 list((room.get("timeline") or {}).get("events") or [])
        for ev in events:
            if ev.get("type") != "m.room.member":
                continue
            who = ev.get("state_key")
            if not who:
                continue
            if (ev.get("content") or {}).get("membership") == "join":
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
    if since is None:
        return body["next_batch"]
    for room_id, room in (rooms.get("invite") or {}).items():
        inviter = next((ev.get("sender") for ev in ((room.get("invite_state") or {}).get("events") or [])
                        if ev.get("type") == "m.room.member" and ev.get("state_key") == _me), None)
        if inviter and _known(inviter):
            try:
                await _api("POST", f"{V3}/rooms/{room_id}/join")
            except Exception as e:
                logging.warning(f"matrix: join {room_id} failed: {_scrub(str(e))}")
    for room_id, room in (rooms.get("join") or {}).items():
        events = (room.get("timeline") or {}).get("events") or []
        if any(ev.get("type") == "m.room.encrypted" for ev in events):
            if room_id not in _encrypted:
                _encrypted.add(room_id)
                logging.warning(f"matrix: room {room_id} is encrypted and Wren cannot read it; "
                                "let Wren open the direct room instead (see README)")
            continue
        if len(_rooms.get(room_id, ())) != 2:
            continue                             # a group room, or membership unknown yet
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
            if config.MATRIX_OWNER_ID and not any(m == {_me, config.MATRIX_OWNER_ID} for m in _rooms.values()):
                # The owner's door is opened from this side, unencrypted, so
                # their first step is accepting an invite rather than finding
                # the encryption toggle. Runs once the initial sync has shown
                # whether such a room already exists (since is None before).
                if since is not None:
                    await _dm_room(config.MATRIX_OWNER_ID)
            since = await _sync_once(since)
            backoff = _RETRY_SECONDS
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.warning(f"matrix sync failed ({_scrub(str(e))}); retrying in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_RETRY_SECONDS)
```

Note for the implementer: `test_start_learns_who_it_is_opens_the_owners_room_and_registers` expects the owner room to be created once the loop has an initial sync behind it; with the idle `_sync_once` returning `"s1"` on the first call, the second iteration creates it. If the test's 0.05 s window proves tight, raise it to 0.1 s rather than changing the order.

- [ ] **Step 4: Run the Matrix tests and the suite**

Run: `venv/bin/python3 -m pytest -q -p no:cacheprovider tests/test_matrix_plugin.py`
Expected: all pass.

Run: `venv/bin/python3 -m pytest -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add wren/communication/matrix_plugin.py tests/test_matrix_plugin.py
git commit -m "feat(matrix): sync loop -- initial sync is history, invites from known people, encrypted rooms skipped"
```

---

### Task 8: Documentation

**Files:**
- Modify: `README.md` (Communication plugins: new `### slack` and `### matrix` sections after `### telegram`; the contacts paragraph at ~line 97; "The People tab" ~line 349; "Where notifications go" ~line 364; Architecture module list ~line 697; the intro line 18)
- Modify: `CLAUDE.md` (rule 3's NAME list; the "User ids are per-surface" loose end)
- Modify: `PROJECT_PLAN.md` (a Phase 10 paragraph after the Phase 9 / one-timeline entry; if no Phase 9 entry exists, add it after the latest landed phase)
- Test: none (docs); run the suite once to be sure nothing moved.

- [ ] **Step 1: README door sections**

After the `### telegram — role: chat` section add:

````markdown
### `slack` — role: chat

DM the bot in Slack. Wren uses Socket Mode: it asks Slack for a websocket and
dials out to it, so there is no public URL, no inbound port and no request
signing, the same posture as Telegram. Needs `SLACK_APP_TOKEN`,
`SLACK_BOT_TOKEN` and `SLACK_OWNER_ID` in `.env`.

Setting up the app, once, at [api.slack.com/apps](https://api.slack.com/apps):

1. **Create New App → From scratch**, name it Wren, pick your workspace.
2. **Socket Mode → Enable**. Slack mints an app-level token with
   `connections:write`; that is `SLACK_APP_TOKEN` (`xapp-…`).
3. **OAuth & Permissions → Bot Token Scopes**: `chat:write`, `im:history`,
   `im:read`, `im:write`, `reactions:write`, `files:read`, `files:write`.
4. **Event Subscriptions → Enable**, and under *Subscribe to bot events* add
   `message.im`.
5. **Install to Workspace**. The *Bot User OAuth Token* (`xoxb-…`) is
   `SLACK_BOT_TOKEN`.
6. Your member id: open your profile, the **⋯** menu, **Copy member ID**
   (`U…`). That is `SLACK_OWNER_ID`.

Add `slack` to `COMMUNICATION_PLUGINS`, restart, and open a DM with the app
under *Apps* in the sidebar. Wren reacts 👀 when it hears you and ✅ when it
has acted, as on Discord. Photos and PDFs sent in the DM are read the same
way as everywhere else. Replies longer than 4000 characters are split.

Other people: "add U0123ABCDEF as kim on slack", or the People tab. Their
member id is under their profile the same way.

### `matrix` — role: chat

DM the bot on Matrix, from Element or any client. Needs `MATRIX_HOMESERVER`,
`MATRIX_TOKEN` and `MATRIX_OWNER_ID`.

1. Make an account for Wren on your homeserver (a normal user, e.g.
   `@wren:home.net`).
2. Get its access token: sign in as that account in Element, then *Settings →
   Help & About → Access Token*; or
   `curl -XPOST https://home.net/_matrix/client/v3/login -d '{"type":"m.login.password","identifier":{"type":"m.id.user","user":"wren"},"password":"…"}'`.
   That is `MATRIX_TOKEN`. `MATRIX_HOMESERVER` is the server's base URL.
3. `MATRIX_OWNER_ID` is your own id (`@you:home.net`).

Add `matrix` to `COMMUNICATION_PLUGINS` and restart. **Wren opens the room**:
at startup it creates an unencrypted direct room and invites you; accept it
and talk there. Wren cannot read encrypted rooms in this version, so do not
start the chat from your side with encryption on (Element's default); if you
already did, leave that room and use the one Wren made. The typing indicator
is Wren's "heard you". Media is read through the homeserver's authenticated
download endpoint (Matrix 1.11, Synapse 1.111 or newer).

Other people: "add @kim:home.net as kim on matrix", or the People tab. Wren
invites them to a direct room the first time it has something to tell them,
and accepts a direct-room invite from anyone it knows.
````

- [ ] **Step 2: README contacts, People tab, notifications, architecture, intro**

- Line ~18: `(Discord, Telegram, HTTP/web chat, Gmail, GitHub)` → `(Discord, Telegram, Slack, Matrix, HTTP/web chat, Gmail, GitHub)`.
- The paragraph starting `Everyone else is a contact, and a contact holds one id per surface`: after the Telegram sentence add `Slack ids look like `U0123ABCDEF` and Matrix ids like `@kim:home.net`; a person first added on one of those gets a Wren id minted from it, so removing and re-adding them keeps their notes.`
- "The People tab" section: the sentence about Discord-then-Telegram order is gone since 2026-10-14; make sure it reads that the form offers the apps in `COMMUNICATION_PLUGINS` and that ids are shown as the app spells them.
- "Where notifications go": where it says `a surface that can push (Discord, Telegram)` → `(Discord, Telegram, Slack, Matrix)`.
- Architecture module list: after the telegram bullet add
  `- `wren/communication/slack_plugin.py` — Slack over Socket Mode, `SlackChannel` (reactions implement `ack`). `ROLE = "chat"`. Translates ids both ways (`SLACK_OWNER_ID`, contacts).`
  and
  `- `wren/communication/matrix_plugin.py` — Matrix Client-Server API over aiohttp, `MatrixChannel` (typing implements `ack`), direct rooms only, unencrypted. `ROLE = "chat"`.`

- [ ] **Step 3: CLAUDE.md**

Rule 3: `"web"`, `"voice"`, `"discord"`, `"telegram"` → `"web"`, `"voice"`, `"discord"`, `"telegram"`, `"slack"`, `"matrix"`. The loose-end paragraph "User ids are per-surface": after `discord_plugin._wren_user_id` translates the same way` add `, as do the Slack and Matrix plugins; a per-surface id is a string in `contacts` (`ID_SHAPE` is the one place its shape lives) and the Wren id stays an int, minted (`contacts.mint`) when the first door's id is not a number.`

- [ ] **Step 4: PROJECT_PLAN.md**

Add after the most recent landed phase paragraph:

```markdown
**Phase 10 — More doors** (2026-10-15). Slack (Socket Mode, no public URL)
and Matrix (Client-Server API, direct rooms, unencrypted) are chat plugins in
Telegram's shape, with no new dependency. To get there, a contact's
per-surface id became a string with a shape per surface (`contacts.ID_SHAPE`)
while the Wren id stayed an int, minted from the first id when that is not a
number. Still cards: Mattermost/Rocket.Chat, email as a two-way door, Signal.
```

- [ ] **Step 5: Run the suite and commit**

Run: `venv/bin/python3 -m pytest -q -p no:cacheprovider`
Expected: all pass.

```bash
git add README.md CLAUDE.md PROJECT_PLAN.md
git commit -m "docs: Slack and Matrix doors, string contact ids"
```

---

## Self-review notes

- Spec §1 → Task 1 (store), Task 2 (doors, tab, `/api/me`). §2 → Tasks 4, 5. §3 → Tasks 6, 7. §4 → Tasks 3, 8. §5 is spread over each task's tests. §6 has no code.
- Names used across tasks: `contacts.register(alias, surface, raw_id, reserved)`, `contacts.Invalid`, `contacts.Taken`, `contacts.mint`, `contacts.ID_SHAPE`, `contacts.SURFACES`, `config.SLACK_*`, `config.MATRIX_*`, `slack_plugin._api(method, *, json, token)`, `slack_plugin._run_once`, `matrix_plugin._api(method, path, *, json, params, data, content_type)`, `matrix_plugin._sync_once(since)`, `matrix_plugin._dm_room`, `matrix_plugin._me`, `matrix_plugin._rooms`. Each is defined in the task that produces it before the task that consumes it.
- Review Focus items 1–5 are pinned by the named tests in Tasks 4, 5, 7, 1 and 6.
