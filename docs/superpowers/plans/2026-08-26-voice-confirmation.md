# Voice Confirmation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A message that arrived by voice never deletes or cancels anything
without a spoken "yes" first; typed messages are unchanged.

**Architecture:** `core.handle_message` gains a keyword-only `source`
("text" | "voice") that `Ctx` carries. Each skill declares which of its
intents are destructive (`DESTRUCTIVE`) and how to phrase the question
(`CONFIRM`); `registry` unions them. When a voice turn resolves to a
destructive intent, core parks it in an in-memory `_pending` dict and asks;
the user's next turn is checked against yes/no regexes *before* the
classifier runs. Only `/voice` in `http_plugin` passes `source="voice"`.

**Tech Stack:** Python 3.12, asyncio, `re`, `time`, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-26-voice-confirmation-design.md`

## Global Constraints

- **Every surface enters through `core.handle_message`**; the guard lives
  there and nowhere else. Communication plugins carry no domain logic
  (CLAUDE.md rule 2) — `http_plugin` only passes `source="voice"`.
- **Core never names a skill.** Which intents are destructive and how to
  phrase the question come from the skill modules via `registry`.
- **The classifier prompt does not grow.** Yes/no is a regex in core ahead of
  `brain.detect_intent`; there is no `confirm` intent.
- **`_pending` is process-local, keyed by user id, one entry per user,
  120 s TTL** (`_CONFIRM_TTL = 120`, module constant, not a setting).
- **A "yes" runs the stored intent with the stored `Ctx` but the *current*
  channel** — the reply goes where the answer came from.
- **The `is_enabled` dispatch guard applies at confirmation time too.**
- **Every new/changed test module keeps the env preamble** (DISCORD_TOKEN,
  WREN_OWNER_ID, LLM_PROVIDERS, LMSTUDIO_BASE_URL, LMSTUDIO_MODEL, TIMEZONE)
  and never calls the model — patch `brain.detect_intent`/`brain.chat`.
- **Never touch the real `wren.db`** (conftest isolates pytest; manual checks
  set `WREN_DB`).
- Test invocations say `venv/bin/python3`; in a worktree use the absolute
  `/home/winter/work/Wren/venv/bin/python3`.

---

### Task 1: Skills declare what is destructive

**Files:**
- Modify: `wren/skills/shopping_skill.py`, `wren/skills/notes_skill.py`,
  `wren/skills/reminder_skill.py`, `wren/skills/pins_skill.py`,
  `wren/skills/contacts_skill.py`, `wren/skills/memory_skill.py` (if present on
  the branch — see note), `wren/registry.py`
- Test: `tests/test_plugins.py` (the registry tests live here), plus one
  assertion in each skill's existing test module

**Interfaces:**
- Produces: per skill, `DESTRUCTIVE: list[str]` (⊆ `INTENTS`) and
  `CONFIRM: dict[str, str]` (keys == `set(DESTRUCTIVE)`, values are the
  verb phrase for the question); `registry.destructive_intents() -> set[str]`
  over *enabled* plugins; `registry.confirm_phrase(intent: str) -> str`.

Note on `memory_skill`: it lands in the memory branch with `forget_memory`.
If it is present when this task runs, add `DESTRUCTIVE = ["forget_memory"]`,
`CONFIRM = {"forget_memory": "forget"}` beside its `INTENTS`; if not, skip it
and say so in the report — the memory branch's final review will add it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_plugins.py` (it already imports `registry`; add the
skill imports it lacks):

```python
from wren.skills import shopping_skill, notes_skill, reminder_skill, pins_skill, contacts_skill


@pytest.mark.parametrize("skill", [shopping_skill, notes_skill, reminder_skill, pins_skill, contacts_skill])
def test_destructive_is_a_subset_of_intents_with_a_phrase_each(skill):
    assert set(skill.DESTRUCTIVE) <= set(skill.INTENTS)
    assert set(skill.CONFIRM) == set(skill.DESTRUCTIVE)
    assert all(v and v == v.strip() for v in skill.CONFIRM.values())


def test_destructive_intents_unions_enabled_skills():
    from wren import settings
    settings.init_db()
    assert {"remove_shopping_item", "clear_shopping", "discard_idea",
            "cancel_reminder", "unpin_note", "remove_contact"} <= registry.destructive_intents()
    registry.set_enabled(pins_skill, False)
    try:
        assert "unpin_note" not in registry.destructive_intents()
    finally:
        registry.set_enabled(pins_skill, True)


def test_confirm_phrase_falls_back_to_the_intent_name():
    assert registry.confirm_phrase("discard_idea") == "discard the idea"
    assert registry.confirm_phrase("no_such_intent") == "no such intent"
```

- [ ] **Step 2: Run and watch them fail**

Run: `venv/bin/python3 -m pytest -q tests/test_plugins.py -k "destructive or confirm"`
Expected: FAIL — `AttributeError: module 'wren.skills.shopping_skill' has no attribute 'DESTRUCTIVE'`

- [ ] **Step 3: Declare, per skill**

Directly under each skill's `INTENTS = [...]` line:

`shopping_skill.py`:
```python
DESTRUCTIVE = ["remove_shopping_item", "clear_shopping"]
CONFIRM = {"remove_shopping_item": "remove from the shopping list",
           "clear_shopping": "clear the whole shopping list"}
```

`notes_skill.py`:
```python
DESTRUCTIVE = ["discard_idea"]
CONFIRM = {"discard_idea": "discard the idea"}
```

`reminder_skill.py`:
```python
DESTRUCTIVE = ["cancel_reminder"]
CONFIRM = {"cancel_reminder": "cancel the reminder"}
```

`pins_skill.py`:
```python
DESTRUCTIVE = ["unpin_note"]
CONFIRM = {"unpin_note": "unpin"}
```

`contacts_skill.py`:
```python
DESTRUCTIVE = ["remove_contact"]
CONFIRM = {"remove_contact": "remove the contact"}
```

`wren/registry.py`, after `all_guidelines()`:

```python
def destructive_intents() -> set[str]:
    """Intents that delete or irreversibly change something, declared by the
    skills that own them. Core asks before running one that came in by voice."""
    return {intent for plugin in enabled_plugins()
            for intent in getattr(plugin, "DESTRUCTIVE", [])}


def confirm_phrase(intent: str) -> str:
    """How to word "do you want me to …?" for this intent, from the skill."""
    for plugin in PLUGINS:
        phrase = getattr(plugin, "CONFIRM", {}).get(intent)
        if phrase:
            return phrase
    return intent.replace("_", " ")
```

- [ ] **Step 4: Run the tests**

Run: `venv/bin/python3 -m pytest -q tests/test_plugins.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add wren/skills wren/registry.py tests/test_plugins.py
git commit -m "feat(skills): declare destructive intents and how to ask about them"
```

---

### Task 2: `source` on Ctx and handle_message; the confirmation guard

**Files:**
- Modify: `wren/channel.py` (`Ctx`), `wren/core.py`
- Test: `tests/test_core.py`

**Interfaces:**
- Consumes: `registry.destructive_intents()`, `registry.confirm_phrase()` (Task 1).
- Produces: `Ctx.source: str = "text"`;
  `core.handle_message(user_id, text, channel, *, source="text")`;
  `core._pending: dict[int, tuple[str, Ctx, float]]`, `core._CONFIRM_TTL = 120`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_core.py` (it has `OWNER`, `CollectingChannel`, the
`authorized` fixture and a `detected` fixture that patches `detect_intent`;
add `import time` and `from wren.skills import shopping_skill, shopping_store`
to its imports):

```python
@pytest.fixture(autouse=True)
def no_pending():
    core._pending.clear()
    yield
    core._pending.clear()


def _voice(text, detected_intent, content=""):
    """One voice turn with the classifier pinned to `detected_intent`."""
    ch = CollectingChannel()
    with patch.object(brain, "detect_intent", return_value={"intent": detected_intent, "content": content, "tags": []}), \
         patch.object(brain, "chat", return_value="chatty"):
        asyncio.run(core.handle_message(OWNER, text, ch, source="voice"))
    return ch


def test_voice_destructive_intent_asks_instead_of_acting():
    from wren import settings
    settings.init_db(); shopping_store.init_db()
    shopping_store.add("milk", "owner")
    ch = _voice("remove milk", "remove_shopping_item", "milk")
    assert ch.sent == ['Confirm: remove from the shopping list "milk"? Say yes or no.']
    assert [i["item"] for i in shopping_store.active_items()] == ["milk"]
    assert core._pending[OWNER][0] == "remove_shopping_item"


def test_voice_non_destructive_intent_acts_immediately():
    from wren import settings
    settings.init_db(); shopping_store.init_db()
    ch = _voice("add milk", "add_shopping_item", "milk")
    assert [i["item"] for i in shopping_store.active_items()] == ["milk"]
    assert OWNER not in core._pending


def test_text_destructive_intent_is_unchanged():
    from wren import settings
    settings.init_db(); shopping_store.init_db()
    shopping_store.add("milk", "owner")
    ch = CollectingChannel()
    with patch.object(brain, "detect_intent", return_value={"intent": "remove_shopping_item", "content": "milk", "tags": []}):
        asyncio.run(core.handle_message(OWNER, "remove milk", ch))
    assert shopping_store.active_items() == []
    assert OWNER not in core._pending


def test_yes_runs_the_pending_intent_on_the_current_channel():
    from wren import settings
    settings.init_db(); shopping_store.init_db()
    shopping_store.add("milk", "owner")
    _voice("remove milk", "remove_shopping_item", "milk")
    ch = CollectingChannel()
    with patch.object(brain, "detect_intent", side_effect=AssertionError("classifier must not run on a yes")):
        asyncio.run(core.handle_message(OWNER, "Yes.", ch))
    assert shopping_store.active_items() == []
    assert ch.sent and "milk" in ch.sent[0]
    assert OWNER not in core._pending


def test_no_drops_it():
    from wren import settings
    settings.init_db(); shopping_store.init_db()
    shopping_store.add("milk", "owner")
    _voice("remove milk", "remove_shopping_item", "milk")
    ch = CollectingChannel()
    with patch.object(brain, "detect_intent", side_effect=AssertionError("classifier must not run on a no")):
        asyncio.run(core.handle_message(OWNER, "no thanks", ch))
    assert ch.sent == ["Okay, left it alone."]
    assert [i["item"] for i in shopping_store.active_items()] == ["milk"]
    assert OWNER not in core._pending


def test_unrelated_text_clears_the_question_and_proceeds():
    from wren import settings
    settings.init_db(); shopping_store.init_db()
    shopping_store.add("milk", "owner")
    _voice("remove milk", "remove_shopping_item", "milk")
    ch = CollectingChannel()
    with patch.object(brain, "detect_intent", return_value={"intent": "chat", "content": "", "tags": []}), \
         patch.object(brain, "chat", return_value="It's sunny."):
        asyncio.run(core.handle_message(OWNER, "what's the weather", ch))
    assert ch.sent == ["It's sunny."]
    assert [i["item"] for i in shopping_store.active_items()] == ["milk"]
    assert OWNER not in core._pending


def test_expired_question_is_forgotten(monkeypatch):
    from wren import settings
    settings.init_db(); shopping_store.init_db()
    shopping_store.add("milk", "owner")
    _voice("remove milk", "remove_shopping_item", "milk")
    intent, ctx, expires = core._pending[OWNER]
    core._pending[OWNER] = (intent, ctx, time.monotonic() - 1)
    ch = CollectingChannel()
    with patch.object(brain, "detect_intent", return_value={"intent": "chat", "content": "", "tags": []}), \
         patch.object(brain, "chat", return_value="chatty"):
        asyncio.run(core.handle_message(OWNER, "yes", ch))
    assert ch.sent == ["chatty"]                     # a bare yes with nothing pending is just a turn
    assert [i["item"] for i in shopping_store.active_items()] == ["milk"]


def test_newer_question_replaces_older():
    from wren import settings
    settings.init_db(); shopping_store.init_db()
    _voice("remove milk", "remove_shopping_item", "milk")
    _voice("clear the list", "clear_shopping", "")
    assert core._pending[OWNER][0] == "clear_shopping"


def test_yes_respects_a_skill_switched_off_meanwhile():
    from wren import settings, registry
    settings.init_db(); shopping_store.init_db()
    shopping_store.add("milk", "owner")
    _voice("remove milk", "remove_shopping_item", "milk")
    registry.set_enabled(shopping_skill, False)
    try:
        ch = CollectingChannel()
        asyncio.run(core.handle_message(OWNER, "yes", ch))
    finally:
        registry.set_enabled(shopping_skill, True)
    assert ch.sent == ["That skill is switched off."]
    assert [i["item"] for i in shopping_store.active_items()] == ["milk"]


def test_ctx_carries_source():
    seen = {}
    async def fake_handle(intent, ctx):
        seen["source"] = ctx.source
    # INTENT_HANDLERS maps to the module; core looks `handle` up at call time
    with patch.object(brain, "detect_intent", return_value={"intent": "add_shopping_item", "content": "milk", "tags": []}), \
         patch.object(shopping_skill, "handle", new=fake_handle):
        asyncio.run(core.handle_message(OWNER, "add milk", CollectingChannel(), source="voice"))
    assert seen["source"] == "voice"
```

- [ ] **Step 2: Run and watch them fail**

Run: `venv/bin/python3 -m pytest -q tests/test_core.py -k "voice or yes or no_drops or unrelated or expired or newer or source"`
Expected: FAIL — `TypeError: handle_message() got an unexpected keyword argument 'source'`

- [ ] **Step 3: `Ctx.source`**

In `wren/channel.py`, add the last field of `Ctx`:

```python
    # "text" | "voice" -- how the words arrived. Voice is transcribed and
    # therefore mis-heard sometimes; core asks before acting on anything
    # destructive when this is "voice". Skills may read it; none must.
    source: str = "text"
```

- [ ] **Step 4: The guard in core**

In `wren/core.py`, module level (add `import re`, `import time`):

```python
# Questions core has asked and not yet had answered: user_id -> (intent, ctx,
# expires_at). One per user; a newer destructive request replaces the older.
# ponytail: process-local, so a restart forgets the question -- the safe
# direction. Persist only if a multi-process deployment ever exists.
_pending: dict[int, tuple[str, Ctx, float]] = {}
_CONFIRM_TTL = 120.0
_YES = re.compile(r"^(yes|yeah|yep|yup|do it|confirm|go ahead|sure)\b", re.I)
_NO = re.compile(r"^(no|nope|nah|cancel|never mind|nevermind|stop|don't)\b", re.I)


def _take_pending(user_id: int):
    """The unexpired question for this user, removed. Answered or not, one
    turn is all it gets: asking again is nagging."""
    entry = _pending.pop(user_id, None)
    if entry and entry[2] > time.monotonic():
        return entry
    return None
```

Then change the signature and the top of the function:

```python
async def handle_message(user_id: int, text: str, channel: Channel, *, source: str = "text") -> None:
    ...
    if user_id not in config.id_to_name():
        return

    text = (text or "").strip()
    if not text:
        return

    await channel.ack("seen")

    # Answering a question core asked last turn happens BEFORE the classifier:
    # "yes" is not an intent, and a 7B model handed a bare "yes" with history
    # will confidently pick something. Anything that is not a yes or a no
    # drops the question and is handled as the new turn it is.
    pending = _take_pending(user_id)
    if pending:
        intent, ctx, _ = pending
        if _YES.match(text):
            ctx.channel = channel     # reply where the answer came from
            try:
                plugin = registry.INTENT_HANDLERS.get(intent)
                if plugin is None or not registry.is_enabled(plugin):
                    await channel.send("That skill is switched off.")
                else:
                    await plugin.handle(intent, ctx)
                await channel.ack("done")
            except Exception as e:
                logging.error(f"Error handling confirmation from {user_id}: {e}")
                await channel.send("Something went wrong, try again.")
                await channel.ack("error")
            return
        if _NO.match(text):
            await channel.send("Okay, left it alone.")
            await channel.ack("done")
            return
```

And in the existing `try:` block, build `ctx` with `source=source` and put the
guard in front of the skill dispatch:

```python
        ctx = Ctx(
            user_id=user_id,
            channel=channel,
            content=result.get("content", text),
            text=text,
            tags=result.get("tags", []),
            person=result.get("person"),
            when=result.get("when"),
            source=source,
        )

        if intent in registry.INTENT_HANDLERS and registry.is_enabled(registry.INTENT_HANDLERS[intent]):
            if source == "voice" and intent in registry.destructive_intents():
                # Transcription mis-hears and the skills fuzzy-match; between
                # them "remove milk" can become "clear the list". Ask first.
                _pending[user_id] = (intent, ctx, time.monotonic() + _CONFIRM_TTL)
                what = f' "{ctx.content.strip()}"' if ctx.content.strip() else ""
                await channel.send(f"Confirm: {registry.confirm_phrase(intent)}{what}? Say yes or no.")
            else:
                await registry.INTENT_HANDLERS[intent].handle(intent, ctx)
```

- [ ] **Step 5: Run the tests**

Run: `venv/bin/python3 -m pytest -q tests/test_core.py`
Expected: PASS

- [ ] **Step 6: Run the whole suite**

Run: `venv/bin/python3 -m pytest -q`
Expected: PASS — `Ctx` and `handle_message` changed shape.

- [ ] **Step 7: Commit**

```bash
git add wren/channel.py wren/core.py tests/test_core.py
git commit -m "feat(core): ask before a destructive intent that arrived by voice"
```

---

### Task 3: `/voice` says so; docs

**Files:**
- Modify: `wren/communication/http_plugin.py` (`_dispatch`, `voice`), `README.md`
  ("## Voice (optional)" section)
- Test: `tests/test_http_surface.py`

**Interfaces:**
- Consumes: `core.handle_message(..., source=...)` (Task 2).

- [ ] **Step 1: Write the failing tests**

`tests/test_http_surface.py` has a `call(method, path, token=..., json=... | data=...)`
helper that returns `(status, body)`, and a token `"good-token"` mapped to
user 1. First, its existing fake in `test_voice_transcribes_then_dispatches`
(and the `_replies()` helper's inner `fake`) has the signature
`async def fake(user_id, text, channel)`; once `_dispatch` passes `source=`,
that raises `TypeError`. Change both to
`async def fake(user_id, text, channel, *, source="text")`.

Then add:

```python
def test_voice_dispatches_with_source_voice(monkeypatch):
    from wren import stt
    seen = {}

    async def fake(user_id, text, channel, *, source="text"):
        seen["source"] = source
        await channel.send("ok")

    monkeypatch.setattr(stt, "transcribe", lambda _audio: "remove milk")
    monkeypatch.setattr(core, "handle_message", fake)
    status, body = call("post", "/voice", token="good-token", data=b"RIFFfake")
    assert status == 200
    assert seen["source"] == "voice"


def test_message_dispatches_with_source_text(monkeypatch):
    seen = {}

    async def fake(user_id, text, channel, *, source="text"):
        seen["source"] = source
        await channel.send("ok")

    monkeypatch.setattr(core, "handle_message", fake)
    status, body = call("post", "/message", token="good-token", json={"text": "remove milk"})
    assert status == 200
    assert seen["source"] == "text"
```

- [ ] **Step 2: Run and watch them fail**

Run: `venv/bin/python3 -m pytest -q tests/test_http_surface.py -k "source_voice or source_text"`
Expected: FAIL — `assert 'text' == 'voice'` for the voice case

- [ ] **Step 3: Pass the source through**

In `http_plugin.py`:

```python
async def _dispatch(user_id: int, text: str, *, source: str = "text", **extra) -> web.Response:
    channel = CollectingChannel()
    await core.handle_message(user_id, text, channel, source=source)
    ...
```

and in `voice()`:

```python
    return await _dispatch(user_id, transcript, source="voice", transcript=transcript)
```

- [ ] **Step 4: Document**

`README.md`, in "## Voice (optional)", add a paragraph:

```
Anything that arrived by voice and would delete or cancel something — remove
an item, clear the list, discard an idea, cancel a reminder, unpin, remove a
contact — is confirmed first: Wren replies `Confirm: remove from the shopping
list "milk"? Say yes or no.` and acts only on a "yes" in the next message
(spoken or typed, within two minutes). Anything else drops the question.
Typed messages are unchanged. Transcription mis-hears; this is the seatbelt.
```

- [ ] **Step 5: Run the whole suite**

Run: `venv/bin/python3 -m pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add wren/communication/http_plugin.py tests/ README.md
git commit -m "feat(voice): /voice marks its turns so core confirms destructive ones"
```

---

## After the plan

With the service on a scratch DB (`WREN_DB`, `COMMUNICATION_PLUGINS=http`, a
temp port — see the smoke-test card on the Trello board for the exact env):

```bash
curl -s -X POST localhost:8799/message -H "Authorization: Bearer $T" -d '{"text":"add milk to shopping"}'
curl -s -X POST localhost:8799/voice   -H "Authorization: Bearer $T" --data-binary @remove-milk.wav
# -> {"transcript": "remove milk", "replies": ["Confirm: remove from the shopping list \"milk\"? Say yes or no."]}
curl -s -X POST localhost:8799/message -H "Authorization: Bearer $T" -d '{"text":"yes"}'
# -> {"replies": ["Removed milk. 🐦"]}
```

## Deliberately not in this plan

- A confirmation card in the web chat.
- Undo on Discord/Telegram for typed deletes.
- Any skill reading `ctx.source`.
- A setting for the TTL or the yes/no vocabulary.
