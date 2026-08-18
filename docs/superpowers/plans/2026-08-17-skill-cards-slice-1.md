# Skill Cards — Slice 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the web chat one live, interactive shopping card, and build the
card machinery every later card reuses.

**Architecture:** Skills gain one new `Channel` capability, `send_card(kind,
data, text)`. Surfaces that cannot draw a card send `text` — the exact prose
they send today. The web chat persists *what to re-fetch* (never the rows) in a
new `messages.card` column, and re-fetches through a new `POST /api/dispatch`
that runs one intent directly with no LLM. That endpoint serves card rendering,
card refresh, and every card button.

**Tech Stack:** Python 3.12, aiohttp, SQLite (stdlib `sqlite3`), pytest, and
hand-written vanilla JS in `wren/communication/chat.html` (no build step, no
framework, no dependencies).

**Spec:** `docs/superpowers/specs/2026-08-17-skill-cards-and-spaces-design.md`

## Global Constraints

Copied from the spec and from `CLAUDE.md`. Every task's requirements implicitly
include these.

- **A skill never imports a transport.** `wren/skills/*_skill.py` touches only
  `ctx.channel`. If it needs something `Channel` cannot do, the capability goes
  on the `Channel` protocol — that is exactly what this slice does.
- **A communication plugin never contains domain logic.** `webchat.py` must not
  call `shopping_store` (or any `*_store`) directly. It dispatches intents.
- **`messages.content` and `WebChannel.history()` do not change.** The model must
  see the same transcript after this slice as before it. Cards are invisible to
  the LLM.
- **A card stores what to fetch, never the rows.** `messages.card` holds
  `{"kind", "intent", "params"}` and nothing resembling data.
- **`params` carries `Ctx` fields only** — in practice `content` and `tags`.
  `content` is `""` for every card kind in this slice.
- **Never run anything against the real `wren.db`.** `tests/conftest.py` points
  `WREN_DB` at a tmp file per test, so `pytest` is safe. Any manual check must
  `export WREN_DB=$(mktemp -d)/scratch.db` first.
- **Every test module starts with the env preamble** used by its neighbours:
  ```python
  import os
  os.environ.setdefault("DISCORD_TOKEN", "test")
  os.environ.setdefault("WREN_OWNER_ID", "1")
  os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
  os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
  os.environ.setdefault("LMSTUDIO_MODEL", "test-model")
  ```
  `tests/test_channel.py` is the exception — it imports no config and needs none.
- **Run the whole suite before each commit**, not just the new test. Baseline at
  the time of writing: `687 passed`.
- **The working tree is production.** `wren.service` runs this checkout in place.
  Anything broken on disk is live at the next restart.

---

### Task 1: `send_card` on the protocol and the three prose surfaces

The capability itself, plus every surface that answers it with prose. Nothing
renders a card yet — this task's deliverable is that a skill *could* call
`send_card` and no surface would break.

**Files:**
- Modify: `wren/channel.py` (the `Channel` protocol, and `CollectingChannel`)
- Modify: `wren/communication/discord_plugin.py` (`DiscordChannel`)
- Modify: `wren/communication/telegram_plugin.py` (`TelegramChannel`)
- Test: `tests/test_channel.py`, `tests/test_discord_surface.py`,
  `tests/test_telegram_plugin.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `async Channel.send_card(kind: str, data: dict, text: str) -> None`,
  implemented by every channel in the codebase. `CollectingChannel.cards` is a
  `list[tuple[str, dict]]` of `(kind, data)` in call order; `CollectingChannel`
  also appends `text` to `.sent`, so tests written against `.sent` keep working.

- [ ] **Step 1: Write the failing tests**

In `tests/test_channel.py`:

```python
def test_send_card_records_the_card_and_the_prose():
    ch = CollectingChannel()

    async def go():
        await ch.send_card("shopping", {"items": [{"text": "milk"}]}, "milk")

    asyncio.run(go())
    assert ch.cards == [("shopping", {"items": [{"text": "milk"}]})]
    # also in .sent, so every existing skill test that asserts on prose keeps
    # working when a skill starts emitting a card alongside it
    assert ch.sent == ["milk"]


def test_cards_start_empty():
    assert CollectingChannel().cards == []
```

In `tests/test_discord_surface.py`:

```python
def test_discord_send_card_falls_back_to_the_prose():
    # The degradation IS the contract: a skill emitting a card must never be a
    # regression on a surface that cannot draw one.
    from wren.communication.discord_plugin import DiscordChannel

    message = MagicMock()
    message.channel.send = AsyncMock()
    channel = DiscordChannel(message, MagicMock())

    asyncio.run(channel.send_card("shopping", {"items": []}, "Shopping list is empty."))

    message.channel.send.assert_awaited_once_with("Shopping list is empty.")
```

In `tests/test_telegram_plugin.py`:

```python
def test_telegram_send_card_falls_back_to_the_prose():
    api = AsyncMock(return_value={})
    with patch.object(telegram_plugin, "_api", new=api):
        asyncio.run(telegram_plugin.TelegramChannel(42).send_card(
            "shopping", {"items": []}, "Shopping list is empty."))

    assert api.await_args.args[0] == "sendMessage"
    assert api.await_args.kwargs["data"] == {"chat_id": 42, "text": "Shopping list is empty."}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `source venv/bin/activate && python -m pytest tests/test_channel.py tests/test_discord_surface.py tests/test_telegram_plugin.py -q`
Expected: FAIL — `AttributeError: 'CollectingChannel' object has no attribute 'send_card'` and the same for the other two.

- [ ] **Step 3: Add the capability to the protocol**

In `wren/channel.py`, inside `class Channel(Protocol)`, after `send_file`:

```python
    async def send_card(self, kind: str, data: dict, text: str) -> None:
        """Structured data a surface may draw as an interactive card.

        `text` is the prose the skill would otherwise have sent, verbatim.
        Surfaces that cannot draw a card send exactly that, so a skill adopting
        a card is never a regression on Discord or Telegram. `data` is the rows
        to draw; it is NOT persisted anywhere — see the design doc on why cards
        are live.
        """
        ...
```

- [ ] **Step 4: Implement it on `CollectingChannel`**

In `wren/channel.py`, in `CollectingChannel.__init__`, alongside `self.sent`:

```python
        self.cards: list[tuple[str, dict]] = []
```

and the method, after `send_file`:

```python
    async def send_card(self, kind: str, data: dict, text: str) -> None:
        # both, deliberately: a test may assert on the structure, and every
        # existing test that asserts on prose keeps passing unchanged
        self.cards.append((kind, data))
        await self.send(text)
```

- [ ] **Step 5: Implement it on the two chat surfaces**

In `wren/communication/discord_plugin.py`, in `DiscordChannel`, after `send_file`:

```python
    async def send_card(self, kind: str, data: dict, text: str) -> None:
        # Discord could draw an embed, but a card is interactive and an embed is
        # not; prose is the honest degradation rather than a half-card.
        await self.send(text)
```

In `wren/communication/telegram_plugin.py`, in `TelegramChannel`, after `send_file`:

```python
    async def send_card(self, kind: str, data: dict, text: str) -> None:
        # Inline keyboards would mean a callback route and a per-surface
        # interaction model. Prose is what send_card's signature exists for.
        await self.send(text)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `source venv/bin/activate && python -m pytest tests/test_channel.py tests/test_discord_surface.py tests/test_telegram_plugin.py -q`
Expected: PASS

- [ ] **Step 7: Run the whole suite**

Run: `source venv/bin/activate && python -m pytest -q`
Expected: PASS, count is 687 + the 4 new tests = 691.

- [ ] **Step 8: Commit**

```bash
git add wren/channel.py wren/communication/discord_plugin.py wren/communication/telegram_plugin.py \
        tests/test_channel.py tests/test_discord_surface.py tests/test_telegram_plugin.py
git commit -m "feat(channel): add send_card, with prose fallback on every surface"
```

---

### Task 2: the `messages.card` column

The repo's first schema migration. Additive, nullable, idempotent.

**Files:**
- Modify: `wren/conversations.py` (`init_db`, `add_message`, `messages`)
- Test: `tests/test_conversations.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `add_message(conversation_id: int, role: str, content: str, card:
  dict | None = None) -> int`. `messages(...)` rows gain a `card` key holding the
  parsed `dict` or `None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_conversations.py`:

```python
def test_add_message_stores_and_returns_a_card():
    convo = conversations.create(1)
    conversations.add_message(convo, "assistant", "milk, eggs",
                              card={"kind": "shopping", "intent": "recall_shopping",
                                    "params": {"content": ""}})
    row = conversations.messages(convo)[0]
    assert row["content"] == "milk, eggs"
    assert row["card"] == {"kind": "shopping", "intent": "recall_shopping",
                           "params": {"content": ""}}


def test_a_message_without_a_card_reads_back_as_none():
    # every message written before this column existed takes this path
    convo = conversations.create(1)
    conversations.add_message(convo, "assistant", "hello")
    assert conversations.messages(convo)[0]["card"] is None


def test_init_db_adds_the_card_column_to_a_table_that_predates_it(tmp_path, monkeypatch):
    monkeypatch.setenv("WREN_DB", str(tmp_path / "old.db"))
    with db.conn() as con:
        con.execute("""
            CREATE TABLE messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role            TEXT NOT NULL,
                content         TEXT NOT NULL,
                created_at      TEXT NOT NULL
            )
        """)
        con.execute("INSERT INTO messages (conversation_id, role, content, created_at) "
                    "VALUES (1, 'user', 'existing row', '2026-01-01T00:00:00+00:00')")

    conversations.init_db()

    with db.conn() as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
        kept = con.execute("SELECT content FROM messages").fetchone()
    assert "card" in cols
    assert kept[0] == "existing row"      # the migration must not drop anything


def test_init_db_is_idempotent():
    conversations.init_db()
    conversations.init_db()               # must not raise "duplicate column name"
```

The last three need `db` and `json` imported at the top of the module if they are
not already there:

```python
import json
from wren import db
```

- [ ] **Step 2: Run them to verify they fail**

Run: `source venv/bin/activate && python -m pytest tests/test_conversations.py -q`
Expected: FAIL — `TypeError: add_message() got an unexpected keyword argument 'card'`, and `KeyError: 'card'`.

- [ ] **Step 3: Add the migration to `init_db`**

In `wren/conversations.py`, in `init_db()`, after the two `CREATE TABLE` calls
and before the `CREATE INDEX` calls:

```python
        # The repo's first migration. Everything else here is CREATE TABLE IF
        # NOT EXISTS, which cannot add a column to a table that already exists —
        # and this one does exist, with real conversations in it. Additive and
        # nullable, so every row written before today reads back as card=None.
        cols = {row[1] for row in con.execute("PRAGMA table_info(messages)")}
        if "card" not in cols:
            con.execute("ALTER TABLE messages ADD COLUMN card TEXT")
```

- [ ] **Step 4: Thread `card` through `add_message` and `messages`**

Add `import json` at the top of `wren/conversations.py`.

Replace `add_message`:

```python
def add_message(conversation_id: int, role: str, content: str,
                card: dict | None = None) -> int:
    if role not in ("user", "assistant"):
        raise ValueError(f"role must be 'user' or 'assistant', got {role!r}")
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at, card) "
            "VALUES (?,?,?,?,?)",
            (conversation_id, role, content, _now(),
             json.dumps(card) if card is not None else None),
        )
        return cur.lastrowid
```

In `messages()`, the SQL string is built once at the top of the function.
Change:

```python
    sql = "SELECT id, role, content, created_at FROM messages WHERE conversation_id=?"
```

to:

```python
    sql = "SELECT id, role, content, created_at, card FROM messages WHERE conversation_id=?"
```

and replace the return at the bottom of the function:

```python
    return [
        {"id": r[0], "role": r[1], "content": r[2], "created_at": r[3],
         # stored as JSON text; callers want the object. A row written before
         # this column existed is NULL, which must read back as None and not
         # as the string "null".
         "card": json.loads(r[4]) if r[4] else None}
        for r in rows
    ]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `source venv/bin/activate && python -m pytest tests/test_conversations.py -q`
Expected: PASS

- [ ] **Step 6: Run the whole suite**

Run: `source venv/bin/activate && python -m pytest -q`
Expected: PASS. `WebChannel.history()` builds `{"role", "content"}` explicitly,
so the extra key changes nothing for the LLM — if a brain or webchat test fails
here, that assumption is wrong and it must be understood, not patched over.

- [ ] **Step 7: Commit**

```bash
git add wren/conversations.py tests/test_conversations.py
git commit -m "feat(conversations): add a nullable card column to messages"
```

---

### Task 3: `WebChannel.send_card`, and cards on the way out

The web surface stores the card reference and returns it, so a page reload
re-renders it.

**Files:**
- Modify: `wren/communication/webchat.py` (`WebChannel`, `post_message`,
  `get_conversation`)
- Test: `tests/test_webchat.py`

**Interfaces:**
- Consumes: `Channel.send_card` (Task 1), `add_message(..., card=...)` and the
  `card` key on message rows (Task 2).
- Produces: `WebChannel.cards: list[dict]`, each
  `{"kind": str, "data": dict, "intent": str, "params": dict, "text": str}`.
  `POST /api/conversations/{id}/message` gains a `cards` key in its response.
  `GET /api/conversations/{id}` message objects gain a `card` key.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_webchat.py`:

```python
def test_a_card_comes_back_in_the_message_response():
    async def fake_handle(user_id, text, channel):
        await channel.send_card(
            "shopping", {"items": [{"text": "milk", "added_by": "ann"}]},
            "milk", intent="recall_shopping", params={"content": ""})

    _, convo = call("post", "/api/conversations", token=TOKEN_A)
    with patch.object(core, "handle_message", new=fake_handle):
        status, body = call("post", f"/api/conversations/{convo['id']}/message",
                            token=TOKEN_A, json={"text": "what's on shopping"})
    assert status == 200
    assert body["replies"] == ["milk"]
    assert body["cards"] == [{
        "kind": "shopping",
        "data": {"items": [{"text": "milk", "added_by": "ann"}]},
        "intent": "recall_shopping",
        "params": {"content": ""},
        "text": "milk",
    }]


def test_a_card_survives_a_reload_but_its_rows_do_not():
    # what is persisted is how to re-fetch, never the rows -- that is what makes
    # a card live when you scroll back to it an hour later
    async def fake_handle(user_id, text, channel):
        await channel.send_card("shopping", {"items": [{"text": "milk"}]}, "milk",
                                intent="recall_shopping", params={"content": ""})

    _, convo = call("post", "/api/conversations", token=TOKEN_A)
    with patch.object(core, "handle_message", new=fake_handle):
        call("post", f"/api/conversations/{convo['id']}/message",
             token=TOKEN_A, json={"text": "what's on shopping"})

    _, reloaded = call("get", f"/api/conversations/{convo['id']}", token=TOKEN_A)
    assistant = [m for m in reloaded["messages"] if m["role"] == "assistant"][0]
    assert assistant["content"] == "milk"
    assert assistant["card"] == {"kind": "shopping", "intent": "recall_shopping",
                                 "params": {"content": ""}}
    assert "data" not in assistant["card"]        # rows are never stored


def test_the_model_does_not_see_cards():
    # history() feeds brain.detect_intent; a card must be invisible there
    async def fake_handle(user_id, text, channel):
        await channel.send_card("shopping", {"items": [{"text": "milk"}]}, "milk",
                                intent="recall_shopping", params={"content": ""})

    _, convo = call("post", "/api/conversations", token=TOKEN_A)
    with patch.object(core, "handle_message", new=fake_handle):
        call("post", f"/api/conversations/{convo['id']}/message",
             token=TOKEN_A, json={"text": "what's on shopping"})

    channel = webchat.WebChannel(convo["id"], USER_A)
    history = asyncio.get_event_loop().run_until_complete(channel.history())
    assert all(set(m) == {"role", "content"} for m in history)
```

`patch` needs importing at the top of the module if it is not already there:
`from unittest.mock import patch`.

- [ ] **Step 2: Run them to verify they fail**

Run: `source venv/bin/activate && python -m pytest tests/test_webchat.py -q`
Expected: FAIL — `AttributeError: 'WebChannel' object has no attribute 'send_card'`.

- [ ] **Step 3: Implement `WebChannel.send_card`**

In `wren/communication/webchat.py`, add `import json` if absent. In
`WebChannel.__init__`, alongside `self.files`:

```python
        self.cards: list[dict] = []
```

and after `send_file`:

```python
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
        conversations.add_message(self._conversation_id, "assistant", text, card=ref)
```

Note this replaces the `add_message` call `send()` would have made — a card is
one message row, not two. `send()` is untouched.

- [ ] **Step 4: Return cards from the two endpoints**

In `post_message`, add to the response dict:

```python
            "cards": channel.cards,
```

In `get_conversation`, replace the message mapping:

```python
        convo["messages"] = [
            {"role": m["role"], "content": m["content"], "created_at": m["created_at"],
             "card": m["card"]}
            for m in conversations.messages(convo["id"])
        ]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `source venv/bin/activate && python -m pytest tests/test_webchat.py -q`
Expected: PASS

- [ ] **Step 6: Run the whole suite**

Run: `source venv/bin/activate && python -m pytest -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add wren/communication/webchat.py tests/test_webchat.py
git commit -m "feat(webchat): persist and return card references, never card rows"
```

---

### Task 4: `POST /api/dispatch`

Run one intent with no LLM. This is what every card button and every card
refresh calls.

**Files:**
- Modify: `wren/communication/webchat.py` (new handler + route)
- Test: `tests/test_webchat.py`

**Interfaces:**
- Consumes: `CollectingChannel` (Task 1), `registry.INTENT_HANDLERS`,
  `registry.is_enabled`.
- Produces: `POST /api/dispatch` taking
  `{"intent": str, "content": str = "", "tags": list[str] = []}` and returning
  `{"cards": [...], "replies": [...]}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_webchat.py`:

```python
def test_dispatch_runs_an_intent_without_the_llm():
    from wren import brain
    calls = []

    async def fake_handle(intent, ctx):
        calls.append((intent, ctx.content))
        await ctx.channel.send_card("shopping", {"items": []}, "Shopping list is empty.")

    plugin = MagicMock()
    plugin.handle = fake_handle
    with patch.dict(registry.INTENT_HANDLERS, {"recall_shopping": plugin}), \
         patch.object(brain, "detect_intent", side_effect=AssertionError("no LLM")):
        status, body = call("post", "/api/dispatch", token=TOKEN_A,
                            json={"intent": "recall_shopping"})

    assert status == 200
    assert calls == [("recall_shopping", "")]
    assert body["cards"][0]["kind"] == "shopping"
    assert body["replies"] == ["Shopping list is empty."]


def test_dispatch_requires_a_token():
    status, _ = call("post", "/api/dispatch", json={"intent": "recall_shopping"})
    assert status == 401


def test_dispatch_rejects_a_missing_intent():
    status, _ = call("post", "/api/dispatch", token=TOKEN_A, json={})
    assert status == 400


def test_dispatch_404s_an_unknown_intent():
    status, _ = call("post", "/api/dispatch", token=TOKEN_A,
                     json={"intent": "no_such_intent"})
    assert status == 404


def test_dispatch_404s_an_intent_whose_skill_is_disabled():
    # a skill switched off in the plugins panel must not be reachable through
    # this door either -- the same check core.handle_message makes
    plugin = MagicMock()
    plugin.handle = AsyncMock()
    with patch.dict(registry.INTENT_HANDLERS, {"recall_shopping": plugin}), \
         patch.object(registry, "is_enabled", return_value=False):
        status, _ = call("post", "/api/dispatch", token=TOKEN_A,
                         json={"intent": "recall_shopping"})
    assert status == 404
    plugin.handle.assert_not_awaited()


def test_dispatch_writes_nothing_to_any_conversation():
    # clicking a button must not manufacture a fake user message
    async def fake_handle(intent, ctx):
        await ctx.channel.send("Removed milk.")

    _, convo = call("post", "/api/conversations", token=TOKEN_A)
    plugin = MagicMock()
    plugin.handle = fake_handle
    with patch.dict(registry.INTENT_HANDLERS, {"remove_shopping_item": plugin}):
        call("post", "/api/dispatch", token=TOKEN_A,
             json={"intent": "remove_shopping_item", "content": "milk"})

    _, reloaded = call("get", f"/api/conversations/{convo['id']}", token=TOKEN_A)
    assert reloaded["messages"] == []
```

Imports these tests need at the top of the module, if absent:
`from unittest.mock import AsyncMock, MagicMock, patch` and `from wren import registry`.

- [ ] **Step 2: Run them to verify they fail**

Run: `source venv/bin/activate && python -m pytest tests/test_webchat.py -q`
Expected: FAIL with 404s — the route does not exist yet.

- [ ] **Step 3: Implement the handler**

In `wren/communication/webchat.py`, next to the other handlers inside
`build_app` (match the surrounding style — the other handlers are nested
functions there):

```python
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
            "cards": [{"kind": k, "data": d} for k, d in channel.cards],
            "replies": channel.sent,
        })
```

`CollectingChannel` is reused as-is rather than subclassed: it already
accumulates instead of sending, already persists nothing, and already returns
`None` from `history()`, which is correct for a one-shot with no conversation.

Add the imports at the top of the module if absent:

```python
from ..channel import CollectingChannel, Ctx
```

- [ ] **Step 4: Register the route**

In `build_app`, with the other `app.router.add_*` calls:

```python
    app.router.add_post("/api/dispatch", dispatch)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `source venv/bin/activate && python -m pytest tests/test_webchat.py -q`
Expected: PASS

- [ ] **Step 6: Run the whole suite**

Run: `source venv/bin/activate && python -m pytest -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add wren/communication/webchat.py tests/test_webchat.py
git commit -m "feat(webchat): POST /api/dispatch runs one intent without the LLM"
```

---

### Task 5: `recall_shopping` emits a card

The first skill to use the capability. Note the prose is unchanged — that is the
whole point of `send_card`'s third argument.

**Files:**
- Modify: `wren/skills/shopping_skill.py` (the `recall_shopping` branch)
- Test: `tests/test_shopping_plugin.py`

**Interfaces:**
- Consumes: `Channel.send_card` (Task 1).
- Produces: a card of kind `"shopping"` with data
  `{"items": [{"text": str, "added_by": str}], "suggestions": [str]}`.

  `suggestions` is not in the design doc's shape. It is included because
  `recall_shopping` already computes "You often get: …" and dropping it from the
  card would lose behaviour the prose has today.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_shopping_plugin.py`:

```python
def test_recall_shopping_emits_a_card_alongside_the_prose():
    shopping.add("milk", "ann")
    shopping.add("eggs", "ann")
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle(
        "recall_shopping", Ctx(user_id=1, channel=ch, content="")))

    kind, data = ch.cards[0]
    assert kind == "shopping"
    assert [i["text"] for i in data["items"]] == ["milk", "eggs"]
    assert all(i["added_by"] == "ann" for i in data["items"])
    # the prose is untouched -- Discord and Telegram still get exactly this
    assert "milk, eggs" in ch.sent[0]


def test_an_empty_shopping_list_still_emits_a_card():
    # the card is how the page knows to draw an empty list with an add box,
    # rather than falling back to a prose bubble
    ch = CollectingChannel()
    asyncio.run(shopping_plugin.handle(
        "recall_shopping", Ctx(user_id=1, channel=ch, content="")))

    kind, data = ch.cards[0]
    assert kind == "shopping"
    assert data["items"] == []
    assert ch.sent == ["Shopping list is empty."]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `source venv/bin/activate && python -m pytest tests/test_shopping_plugin.py -q`
Expected: FAIL — `IndexError: list index out of range` on `ch.cards[0]`.

- [ ] **Step 3: Emit the card**

In `wren/skills/shopping_skill.py`, replace the `recall_shopping` branch. The
existing prose construction is kept verbatim; only the send changes.

```python
    elif intent == "recall_shopping":
        active = shopping.active_items()
        common = shopping.common_items()
        active_normalized = {i["item"] for i in active}
        suggestions = [c["item"] for c in common if c["item"] not in active_normalized]
        lines = []
        if active:
            lines.append(", ".join(i["original_text"] for i in active))
        if suggestions:
            lines.append("You often get: " + ", ".join(suggestions) + ".")
        text = "\n".join(lines) if lines else "Shopping list is empty."
        # One send, not two: a card IS the message. The prose is the fallback
        # every surface that cannot draw one receives instead.
        await ctx.channel.send_card(
            "shopping",
            {
                "items": [{"text": i["original_text"], "added_by": i["added_by"]}
                          for i in active],
                "suggestions": suggestions,
            },
            text,
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `source venv/bin/activate && python -m pytest tests/test_shopping_plugin.py -q`
Expected: PASS — including the pre-existing `recall_shopping` prose tests, which
must not have changed. If any of them fail, the prose was altered and must be
put back.

- [ ] **Step 5: Run the whole suite**

Run: `source venv/bin/activate && python -m pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add wren/skills/shopping_skill.py tests/test_shopping_plugin.py
git commit -m "feat(shopping): recall_shopping emits a card, prose unchanged"
```

---

### Task 6: the shopping card in the page

**Files:**
- Modify: `wren/communication/chat.html` (renderer, `send()`, `openConvo()`)
- Test: `tests/test_chat_renderer.py`

**Interfaces:**
- Consumes: the `cards` key on the message response and the `card` key on
  reloaded messages (Task 3), `POST /api/dispatch` (Task 4), the `shopping` card
  shape (Task 5).
- Produces: nothing further tasks depend on. This is the end of slice 1.

`tests/test_chat_renderer.py` asserts on `chat.html`'s *source text*, not a
rendered DOM — there is no JS test runner in this repo and adding one is out of
scope. Follow the existing substring style exactly.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_chat_renderer.py`:

```python
def test_card_renderer_escapes_every_value_it_draws():
    # card data is user-typed ("add <script> to shopping"), so every
    # interpolation into innerHTML must go through esc()
    src = PAGE.read_text(encoding="utf-8")
    start = src.index("function shoppingCard(")
    block = src[start:src.index("\nfunction ", start + 1)]
    assert "esc(" in block
    # the item text must never reach innerHTML raw
    assert "+ i.text +" not in block


def test_card_controls_dispatch_intents_not_sentences():
    # the whole point of /api/dispatch: a button sends an intent, not English
    # for the classifier to re-interpret
    src = PAGE.read_text(encoding="utf-8")
    assert "/api/dispatch" in src
    assert "remove_shopping_item" in src
    assert "add_shopping_item" in src


def test_a_card_message_renders_a_card_and_not_just_prose():
    src = PAGE.read_text(encoding="utf-8")
    # both paths must honour m.card: the live reply and a reloaded transcript
    assert src.count("m.card") >= 1
    assert "cards" in src
```

- [ ] **Step 2: Run them to verify they fail**

Run: `source venv/bin/activate && python -m pytest tests/test_chat_renderer.py -q`
Expected: FAIL — `ValueError: substring not found` on `function shoppingCard(`.

- [ ] **Step 3: Write the renderer**

In `wren/communication/chat.html`, in the rendering section after `fileLink`:

```javascript
/* ── cards ─────────────────────────────────────────────────────────────── */
// A card is live: it holds no rows of its own, it re-fetches through
// /api/dispatch. That is why every control ends by re-rendering from the
// server's response rather than mutating the DOM it already drew.
async function dispatch(intent, content) {
  return api("/api/dispatch", {
    method: "POST",
    body: JSON.stringify({ intent, content: content || "" }),
  });
}

function shoppingCard(data, mount) {
  const items = data.items || [];
  const rows = items.map(i =>
    '<li><span class="citem">' + esc(i.text) + '</span>' +
    '<button class="cx" data-item="' + esc(i.text) + '" title="remove">✕</button></li>'
  ).join("");
  const sugg = (data.suggestions || []).length
    ? '<p class="csugg">You often get: ' +
      data.suggestions.map(esc).join(", ") + "</p>"
    : "";
  mount.innerHTML =
    '<div class="card shopping">' +
      '<header>Shopping · ' + items.length + "</header>" +
      (items.length ? "<ul>" + rows + "</ul>"
                    : '<p class="cempty">Nothing on the list.</p>') +
      sugg +
      '<form class="cadd"><input placeholder="add item…" aria-label="add item">' +
      "<button>+</button></form>" +
    "</div>";

  for (const b of mount.querySelectorAll(".cx")) {
    b.onclick = async () => {
      b.disabled = true;
      const r = await dispatch("remove_shopping_item", b.dataset.item);
      renderCard({ kind: "shopping", data: r.cards[0].data }, mount);
    };
  }
  mount.querySelector(".cadd").onsubmit = async e => {
    e.preventDefault();
    const input = e.target.querySelector("input");
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    const r = await dispatch("add_shopping_item", text);
    // add_shopping_item answers with prose, not a card, so ask for the list
    const fresh = await dispatch("recall_shopping", "");
    renderCard({ kind: "shopping", data: fresh.cards[0].data }, mount);
  };
}

const CARDS = { shopping: shoppingCard };

function renderCard(card, mount) {
  const draw = CARDS[card.kind];
  if (!draw) return false;      // unknown kind: leave the prose bubble alone
  draw(card.data || {}, mount);
  return true;
}

// A stored card carries no rows, so re-fetch before drawing. Failure falls back
// to the prose that is already in the bubble.
async function mountStoredCard(card, mount) {
  try {
    const r = await dispatch(card.intent, (card.params || {}).content || "");
    if (r.cards && r.cards.length) renderCard(r.cards[0], mount);
  } catch (e) { /* prose stays */ }
}
```

- [ ] **Step 4: Mount cards in the two places messages appear**

In `send()`, replace the reply loop. A card and its prose are the *same*
message — `WebChannel.send_card` records the text in `replies` and the card in
`cards` — so drawing both would double every answer. Cards win when present:

```javascript
    if ((data.cards || []).length) {
      // one bubble per card; its prose is the fallback if the kind is unknown
      for (const c of data.cards) {
        const b = bubble("assistant", md(c.text || ""));
        renderCard(c, b.querySelector(".body"));
      }
    } else {
      for (const r of data.replies) bubble("assistant", md(r));
    }
```

`renderCard` returns `false` for a kind the page does not know, leaving the
prose bubble it was given — which is why the bubble is drawn with `c.text`
first and overwritten second, rather than created empty.

In `openConvo()`, replace the message loop:

```javascript
  for (const m of convo.messages) {
    const b = bubble(m.role, m.role === "user" ? esc(m.content) : md(m.content));
    if (m.card) mountStoredCard(m.card, b.querySelector(".body"));
  }
```

- [ ] **Step 5: Add the CSS**

In the `<style>` block, following the existing custom-property naming (read the
top of the stylesheet and reuse the same variables — do not introduce new raw
colour literals):

```css
.card { border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
.card header { padding: .5rem .75rem; font-weight: 600; border-bottom: 1px solid var(--line); }
.card ul { list-style: none; margin: 0; padding: 0; }
.card li { display: flex; align-items: center; gap: .5rem; padding: .4rem .75rem; }
.card li + li { border-top: 1px solid var(--line); }
.card .citem { flex: 1; }
.card .cx { background: none; border: 0; cursor: pointer; opacity: .6; }
.card .cx:hover { opacity: 1; }
.card .cx:disabled { opacity: .3; cursor: default; }
.card .cempty, .card .csugg { margin: 0; padding: .5rem .75rem; opacity: .7; }
.card .cadd { display: flex; gap: .5rem; padding: .5rem .75rem; border-top: 1px solid var(--line); }
.card .cadd input { flex: 1; }
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `source venv/bin/activate && python -m pytest tests/test_chat_renderer.py -q`
Expected: PASS

- [ ] **Step 7: Run the whole suite**

Run: `source venv/bin/activate && python -m pytest -q`
Expected: PASS

- [ ] **Step 8: Verify it in the real app**

`chat.html` is read lazily from disk, so no restart is needed for the page — but
`webchat.py`, `channel.py` and `shopping_skill.py` all changed, so the service
does need one:

```bash
sudo systemctl restart wren.service
```

Then open the web chat and check, in order:

1. Ask "what's on the shopping list" — a card renders, not a prose bubble.
2. Click ✕ on an item — it disappears without a page reload and without a new
   message in the transcript.
3. Type an item into the card's add box — it appears in the same card.
4. Reload the page and scroll to that card — it renders again, showing *current*
   state, and its buttons still work.
5. Ask the same thing on Telegram — plain prose, unchanged.

- [ ] **Step 9: Commit**

```bash
git add wren/communication/chat.html tests/test_chat_renderer.py
git commit -m "feat(chat): render the shopping card, live and interactive"
```

---

## Done when

- A shopping card renders in the web chat, in a fresh reply and in a reloaded
  transcript, showing current state in both.
- ✕ and add work inline with no LLM round trip and no new transcript messages.
- Discord and Telegram answer `recall_shopping` with exactly the prose they
  answered with before this slice.
- The full suite passes.

Slices 2 (notes, ideas, reminders cards) and 3 (spaces) build on this without
changing any of it.
