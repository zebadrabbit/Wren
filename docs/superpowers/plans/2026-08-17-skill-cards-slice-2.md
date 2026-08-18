# Skill Cards — Slice 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Notes, ideas and reminders answer with live interactive cards, the way shopping already does.

**Architecture:** No new architecture. Slice 1 built `Channel.send_card`, the `messages.card` column, `POST /api/dispatch` and the live re-render loop; this slice is three repetitions of that shape plus the renderers. The one genuinely new piece is extracting the card scaffolding in `chat.html` so four cards share it instead of copying it four times.

**Tech Stack:** Python 3.12, aiohttp, SQLite (stdlib `sqlite3`), pytest, vanilla JS in `wren/communication/chat.html` (no build step, no framework).

**Spec:** `docs/superpowers/specs/2026-08-17-skill-cards-and-spaces-design.md`

## Global Constraints

- **A skill never imports a transport.** Skills touch only `ctx.channel`.
- **A communication plugin never contains domain logic.** `webchat.py` must not
  call a `*_store` and must not know what any card kind means.
- **`messages.content` and `WebChannel.history()` do not change.** Cards stay
  invisible to the LLM.
- **A card stores what to re-fetch, never rows.** `{"kind","intent","params"}`.
- **`recall_notes` must only emit a card on its empty-content branch.** With
  non-empty content it calls `brain.recall`, an LLM round trip. A card refreshes
  by re-dispatching its own intent, so a card on that branch would mean an LLM
  call on every render and every page reload. The spec calls this out as
  load-bearing; do not "tidy it up".
- **Prose fallback is now ONE combined message per recall**, not one per row.
  This is a deliberate, user-approved behaviour change: Discord and Telegram
  currently get five messages for five reminders and will get one. Existing
  tests asserting on N separate messages must be updated — and when you update
  one, say so explicitly in your report. Silently rewriting a test to match new
  behaviour is the one thing that must never pass unremarked.
- **Never run anything against the real `wren.db`.** `tests/conftest.py` handles
  pytest; a manual check needs `export WREN_DB=$(mktemp -d)/scratch.db` first.
- **Comments explain *why*, not *what*.**
- **Run the full suite before each commit.** Baseline: **734 passed**.
- **Run pytest with `/home/winter/work/Wren/venv/bin/python -m pytest -q`.**

---

### Task 1: exact-match wins when resolving an item by phrase

A prerequisite, not a card feature. Card buttons dispatch `discard_idea` and
`cancel_reminder` with the row's own text. Both resolvers substring-search and
refuse when more than one row matches — so discarding "build a treehouse" fails
whenever "build a treehouse with a rope ladder" also exists. The button would
report "Found more than one match, be more specific", which is nonsense when you
clicked a specific row.

**Files:**
- Modify: `wren/skills/notes_store.py` (`find`)
- Modify: `wren/skills/reminders_store.py` (`find_pending`)
- Test: `tests/test_notes.py`, `tests/test_reminders.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `find` and `find_pending` return a single-element list when exactly
  one row's content equals the phrase case-insensitively, regardless of how many
  rows contain it as a substring. Otherwise unchanged.

- [ ] **Step 1: Write the failing tests**

In `tests/test_notes.py`:

```python
def test_find_prefers_an_exact_match_over_a_longer_substring_match():
    # A card's discard button sends the row's own text. Without this, clicking
    # "build a treehouse" while "build a treehouse with a rope ladder" also
    # exists matches both, and the skill refuses as ambiguous.
    notes.save(1, "build a treehouse", ["idea"])
    notes.save(1, "build a treehouse with a rope ladder", ["idea"])

    found = notes.find(1, "build a treehouse", tags=["idea"])
    assert [n["content"] for n in found] == ["build a treehouse"]


def test_find_is_still_a_substring_search_when_nothing_matches_exactly():
    notes.save(1, "build a treehouse with a rope ladder", ["idea"])
    found = notes.find(1, "treehouse", tags=["idea"])
    assert [n["content"] for n in found] == ["build a treehouse with a rope ladder"]


def test_find_exact_match_ignores_case():
    notes.save(1, "Build A Treehouse", ["idea"])
    notes.save(1, "build a treehouse with a rope ladder", ["idea"])
    found = notes.find(1, "build a treehouse", tags=["idea"])
    assert [n["content"] for n in found] == ["Build A Treehouse"]
```

In `tests/test_reminders.py`:

```python
def test_find_pending_prefers_an_exact_match():
    reminders.save(1, "call mum", "2030-01-01T09:00:00+00:00")
    reminders.save(1, "call mum about the car", "2030-01-01T10:00:00+00:00")

    found = reminders.find_pending(1, "call mum")
    assert [r["content"] for r in found] == ["call mum"]


def test_find_pending_still_substring_matches_when_no_exact_match():
    reminders.save(1, "call mum about the car", "2030-01-01T10:00:00+00:00")
    found = reminders.find_pending(1, "the car")
    assert [r["content"] for r in found] == ["call mum about the car"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `/home/winter/work/Wren/venv/bin/python -m pytest tests/test_notes.py tests/test_reminders.py -q`
Expected: the exact-match tests FAIL, returning both rows instead of one.

- [ ] **Step 3: Implement in both stores**

`wren/skills/notes_store.py` — replace the whole function:

```python
def find(owner_id: int, substring: str, tags: list[str] | None = None) -> list[dict]:
    results = search(owner_id, tags=tags)
    needle = substring.lower()
    hits = [r for r in results if needle in r["content"].lower()]
    # An exact match wins outright. The phrase usually comes from the model, but
    # it also comes from a card button sending a row's own text -- and there,
    # "build a treehouse" must not read as ambiguous just because "build a
    # treehouse with a rope ladder" also exists. Substring stays the fallback.
    exact = [r for r in hits if r["content"].strip().lower() == substring.strip().lower()]
    return exact if len(exact) == 1 else hits
```

`wren/skills/reminders_store.py` — replace the whole function:

```python
def find_pending(owner_id: int, substring: str) -> list[dict]:
    needle = substring.lower()
    hits = [r for r in pending(owner_id) if needle in r["content"].lower()]
    # exact wins, same reason as notes_store.find
    exact = [r for r in hits if r["content"].strip().lower() == substring.strip().lower()]
    return exact if len(exact) == 1 else hits
```

- [ ] **Step 4: Run the tests**

Run: `/home/winter/work/Wren/venv/bin/python -m pytest tests/test_notes.py tests/test_reminders.py -q`
Expected: PASS

- [ ] **Step 5: Run the whole suite**

Run: `/home/winter/work/Wren/venv/bin/python -m pytest -q`
Expected: PASS. If an existing test breaks here, an exact match changed a
behaviour something relied on — report it, do not silently adjust the old test.

- [ ] **Step 6: Commit**

```bash
git add wren/skills/notes_store.py wren/skills/reminders_store.py tests/test_notes.py tests/test_reminders.py
git commit -m "fix(stores): an exact content match wins over a longer substring match"
```

---

### Task 2: notes and ideas cards

**Files:**
- Modify: `wren/skills/notes_skill.py` (`recall_notes` empty branch, `recall_ideas`)
- Test: `tests/test_notes_plugin.py`

**Interfaces:**
- Consumes: `ctx.channel.send_card(kind, data, text, *, intent, params)`.
- Produces: a `notes` card, `{"notes": [{"content", "tags": [str], "created_at"}]}`,
  with `intent="recall_notes"` and `params={"content": "", "tags": <the tags used>}`.
  An `ideas` card, `{"ideas": [{"content", "created_at"}]}`, with
  `intent="recall_ideas"` and `params={"content": ""}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_notes_plugin.py`:

```python
def test_recall_notes_without_a_question_emits_one_card_and_one_message():
    notes.save(1, "call the plumber", ["house"])
    notes.save(1, "school run is 8:15", ["kids"])
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle(
        "recall_notes", Ctx(user_id=1, channel=ch, content="", tags=[])))

    card = ch.cards[0]
    assert card["kind"] == "notes"
    assert [n["content"] for n in card["data"]["notes"]] == [
        "call the plumber", "school run is 8:15"]
    assert card["data"]["notes"][0]["tags"] == ["house"]
    assert card["intent"] == "recall_notes"
    # content MUST stay empty: with a question this intent calls the LLM, and a
    # card re-dispatches its own intent on every render
    assert card["params"]["content"] == ""
    # one combined message, not one per note
    assert len(ch.sent) == 1
    assert "call the plumber" in ch.sent[0] and "school run is 8:15" in ch.sent[0]


def test_a_filtered_notes_card_remembers_its_filter():
    # params is what the card replays an hour later; drop the tags and a
    # filtered card silently comes back unfiltered
    notes.save(1, "call the plumber", ["house"])
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle(
        "recall_notes", Ctx(user_id=1, channel=ch, content="", tags=["house"])))

    assert ch.cards[0]["params"] == {"content": "", "tags": ["house"]}


def test_recall_notes_with_a_question_still_answers_in_prose_and_emits_no_card():
    # the LLM-answering branch must never become a card: a card re-dispatches
    # its intent to refresh, which would mean an LLM call per render
    notes.save(1, "call the plumber", ["house"])
    ch = CollectingChannel()
    with patch.object(brain, "recall", return_value="You need to call the plumber."):
        asyncio.run(notes_plugin.handle(
            "recall_notes", Ctx(user_id=1, channel=ch, content="what do I need to do?", tags=[])))

    assert ch.cards == []
    assert ch.sent == ["You need to call the plumber."]


def test_recall_ideas_emits_a_card():
    notes.save(1, "build a treehouse", ["idea"])
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("recall_ideas", Ctx(user_id=1, channel=ch)))

    card = ch.cards[0]
    assert card["kind"] == "ideas"
    assert [i["content"] for i in card["data"]["ideas"]] == ["build a treehouse"]
    assert card["intent"] == "recall_ideas"
    assert len(ch.sent) == 1


def test_no_notes_and_no_ideas_still_emit_cards():
    # the card is how the page knows to draw an empty state rather than falling
    # back to a prose bubble
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle(
        "recall_notes", Ctx(user_id=1, channel=ch, content="", tags=[])))
    assert ch.cards[0]["data"]["notes"] == []
    assert ch.sent == ["No notes found."]

    ch2 = CollectingChannel()
    asyncio.run(notes_plugin.handle("recall_ideas", Ctx(user_id=1, channel=ch2)))
    assert ch2.cards[0]["data"]["ideas"] == []
    assert ch2.sent == ["No ideas saved."]
```

`patch` and `brain` need importing at the top of the module if absent:
`from unittest.mock import patch` and `from wren import brain`.

- [ ] **Step 2: Run them to verify they fail**

Run: `/home/winter/work/Wren/venv/bin/python -m pytest tests/test_notes_plugin.py -q`
Expected: FAIL — `IndexError` on `ch.cards[0]`.

- [ ] **Step 3: Emit the notes card**

In `wren/skills/notes_skill.py`, replace the `recall_notes` branch's *listing*
path (the `else` that loops sending one message per note). Keep the LLM branch
and the tag-fallback exactly as they are:

```python
    elif intent == "recall_notes":
        matches = notes.search(ctx.user_id, tags=ctx.tags if ctx.tags else None)
        if not matches and ctx.tags:
            # the model's guessed tag may not match what was actually used
            # when the note was saved — fall back to an unfiltered search
            # rather than falsely reporting no notes at all
            matches = notes.search(ctx.user_id, tags=None)
        if ctx.content.strip():
            # a specific question — let the LLM answer using the notes. NOT a
            # card: a card re-dispatches this intent to refresh itself, which
            # would put an LLM call behind every render and every page reload.
            if not matches:
                await ctx.channel.send("No notes found.")
            else:
                summary = brain.recall(matches, ctx.content)
                await ctx.channel.send(summary)
        else:
            rows = [{"content": n["content"],
                     "tags": [t for t in n["tags"].split(",") if t],
                     "created_at": n["created_at"]}
                    for n in matches]
            if rows:
                text = "\n".join(
                    f"[{n['created_at'][:10]}] {n['content']}"
                    + (f" (tags: {n['tags']})" if n["tags"] else "")
                    for n in matches)
            else:
                text = "No notes found."
            # One message, not one per note: a wall of separate messages is what
            # the card replaces, and Discord/Telegram get the same relief.
            await ctx.channel.send_card(
                "notes", {"notes": rows}, text,
                intent="recall_notes",
                # the tags ride along so a filtered card stays filtered when it
                # re-renders later; content stays empty for the reason above
                params={"content": "", "tags": list(ctx.tags or [])},
            )
```

- [ ] **Step 4: Emit the ideas card**

Replace the `recall_ideas` branch:

```python
    elif intent == "recall_ideas":
        ideas = notes.search(ctx.user_id, tags=["idea"])
        rows = [{"content": i["content"], "created_at": i["created_at"]} for i in ideas]
        text = "\n".join(f"- {i['content']}" for i in ideas) if ideas else "No ideas saved."
        await ctx.channel.send_card(
            "ideas", {"ideas": rows}, text,
            intent="recall_ideas", params={"content": ""},
        )
```

- [ ] **Step 5: Update the existing tests that assert one message per row**

Run the module and read each failure. Any test asserting `ch.sent` has one entry
per note or idea now sees a single combined message. Update those assertions to
match — and **list every test you changed in your report**, with the old and new
expectation. Do not change a test's *intent*, only its shape.

- [ ] **Step 6: Run the tests**

Run: `/home/winter/work/Wren/venv/bin/python -m pytest tests/test_notes_plugin.py -q`
Expected: PASS

- [ ] **Step 7: Run the whole suite, then commit**

```bash
git add wren/skills/notes_skill.py tests/test_notes_plugin.py
git commit -m "feat(notes): notes and ideas answer with cards, prose in one message"
```

---

### Task 3: reminders card

**Files:**
- Modify: `wren/skills/reminder_skill.py` (`recall_reminders`)
- Test: `tests/test_reminder_plugin.py`

**Interfaces:**
- Consumes: `send_card`.
- Produces: a `reminders` card, `{"reminders": [{"content", "fire_at", "local"}]}`,
  `intent="recall_reminders"`, `params={"content": ""}`. `local` is
  `_format_local(fire_at)` — formatted server-side so the card does no timezone
  maths.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_reminder_plugin.py`:

```python
def test_recall_reminders_emits_a_card_with_local_times():
    reminders.save(1, "call mum", "2030-01-01T09:00:00+00:00")
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("recall_reminders", Ctx(user_id=1, channel=ch)))

    card = ch.cards[0]
    assert card["kind"] == "reminders"
    row = card["data"]["reminders"][0]
    assert row["content"] == "call mum"
    assert row["fire_at"] == "2030-01-01T09:00:00+00:00"
    # formatted server-side, so the card never does timezone maths
    assert row["local"] == reminder_plugin._format_local("2030-01-01T09:00:00+00:00")
    assert card["intent"] == "recall_reminders"
    assert len(ch.sent) == 1


def test_no_reminders_still_emits_a_card():
    ch = CollectingChannel()
    asyncio.run(reminder_plugin.handle("recall_reminders", Ctx(user_id=1, channel=ch)))
    assert ch.cards[0]["data"]["reminders"] == []
    assert ch.sent == ["No reminders set."]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `/home/winter/work/Wren/venv/bin/python -m pytest tests/test_reminder_plugin.py -q`
Expected: FAIL — `IndexError` on `ch.cards[0]`.

- [ ] **Step 3: Implement**

Replace the `recall_reminders` branch in `wren/skills/reminder_skill.py`:

```python
    elif intent == "recall_reminders":
        items = reminders.pending(ctx.user_id)
        rows = [{"content": r["content"], "fire_at": r["fire_at"],
                 # formatted here, not in the page: the skill already owns the
                 # timezone and the card should never have to
                 "local": _format_local(r["fire_at"])}
                for r in items]
        text = "\n".join(f"[{_format_local(r['fire_at'])}] {r['content']}"
                         for r in items) if items else "No reminders set."
        await ctx.channel.send_card(
            "reminders", {"reminders": rows}, text,
            intent="recall_reminders", params={"content": ""},
        )
```

- [ ] **Step 4: Update existing per-row assertions**

Same as Task 2 Step 5 — read each failure, update the shape not the intent, and
list every changed test in your report.

- [ ] **Step 5: Run the tests, then the suite, then commit**

```bash
git add wren/skills/reminder_skill.py tests/test_reminder_plugin.py
git commit -m "feat(reminders): recall_reminders answers with a card"
```

---

### Task 4: the three renderers, on shared card scaffolding

**Files:**
- Modify: `wren/communication/chat.html`
- Test: `tests/test_chat_renderer.py`

**Interfaces:**
- Consumes: the three card shapes above, and `POST /api/dispatch`.
- Produces: `CARDS` gains `notes`, `ideas`, `reminders`.

`shoppingCard` currently owns its own busy flag, status line, `guarded` helper
and `refresh` — all four cards need those, so extract them first rather than
copying them three more times.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_chat_renderer.py`:

```python
def test_every_card_kind_has_a_renderer():
    src = PAGE.read_text(encoding="utf-8")
    block = src[src.index("const CARDS = {"):src.index("}", src.index("const CARDS = {"))]
    for kind in ("shopping", "notes", "ideas", "reminders"):
        assert kind in block, f"no renderer registered for {kind}"


def test_the_card_scaffolding_is_shared_not_copied():
    # four cards needing the same busy flag, status line and refresh is three
    # copies waiting to drift apart
    src = PAGE.read_text(encoding="utf-8")
    assert "function cardShell(" in src
    assert src.count("mount._busy = true") <= 2, (
        "busy handling looks copied per card rather than shared in cardShell")


def test_the_notes_card_filters_tags_without_a_round_trip():
    # the tags are already in the payload, so narrowing the list is a DOM pass.
    # notesCard is read-only: if it dispatches at all, filtering has become a
    # server round trip.
    src = PAGE.read_text(encoding="utf-8")
    start = src.index("function notesCard(")
    block = src[start:src.index("\nfunction ", start + 1)]
    assert "dispatch(" not in block
    assert "ctag" in block and "li.hidden" in block


def test_card_actions_dispatch_the_right_intents():
    src = PAGE.read_text(encoding="utf-8")
    assert "discard_idea" in src
    assert "cancel_reminder" in src
```

- [ ] **Step 2: Run them to verify they fail**

Run: `/home/winter/work/Wren/venv/bin/python -m pytest tests/test_chat_renderer.py -q`
Expected: FAIL — `function cardShell(` is not found.

- [ ] **Step 3: Extract the shared scaffolding**

In `chat.html`, above `shoppingCard`, add:

```javascript
// Every card needs the same three things: a serialised dispatch (so two quick
// clicks cannot redraw out of order), a status line for the skill's own reply,
// and a refresh that re-reads from the server. Four cards sharing one copy.
function cardShell(mount, refreshIntent, refreshParams) {
  async function guarded(fn) {
    if (mount._busy) return;
    mount._busy = true;
    try { await fn(); }
    catch (e) { cardStatus(mount, e.message || "Something went wrong."); }
    finally { mount._busy = false; }
  }
  // `r` is the mutation's own response: its `replies` carry the skill's text
  // ("No idea found matching that.") even on a 200, and that must reach the
  // card or a no-op looks identical to a success.
  async function refresh(r) {
    const fresh = await dispatch(refreshIntent, refreshParams || {});
    if (fresh.cards && fresh.cards.length) {
      renderCard(fresh.cards[0], mount);
      cardStatus(mount, ((r && r.replies) || []).join(" "));
    } else {
      cardStatus(mount, ((r && r.replies) || []).join(" ") || "Could not refresh.");
    }
  }
  // Dispatch an action, then redraw from a fresh read. Used by every row
  // control on every card.
  function action(intent, content) {
    return guarded(async () => refresh(await dispatch(intent, { content })));
  }
  return { guarded, refresh, action };
}
```

Then rewrite `shoppingCard` to use it. Inside `shoppingCard`, delete its two
local definitions — the `async function refresh(r) { … }` block and the
`async function guarded(fn) { … }` block — and put this single line where
`refresh` was:

```javascript
  const { guarded, refresh } = cardShell(mount, "recall_shopping", {});
```

Everything else in `shoppingCard` stays byte-for-byte as it is, including the
undo timer, the `cgone` row marking and the add form. The names `guarded` and
`refresh` are unchanged, so no call site inside the function needs touching.
**Do not change the undo behaviour in this task** — if the suite's undo tests
fail, you have altered something you should not have.

- [ ] **Step 4: Write the three renderers**

```javascript
function notesCard(data, mount) {
  const notes = data.notes || [];
  const allTags = [...new Set(notes.flatMap(n => n.tags || []))].sort();
  const chips = allTags.map(t =>
    '<button class="ctag" data-tag="' + esc(t) + '">' + esc(t) + "</button>").join("");
  const rows = notes.map(n =>
    '<li data-tags="' + esc((n.tags || []).join(",")) + '">' +
      '<span class="citem">' + esc(n.content) + "</span>" +
      '<span class="cwhen">' + esc((n.created_at || "").slice(0, 10)) + "</span>" +
    "</li>").join("");
  mount.innerHTML =
    '<div class="card notes">' +
      "<header>Notes · " + notes.length + "</header>" +
      (allTags.length ? '<div class="ctags">' + chips + "</div>" : "") +
      (notes.length ? "<ul>" + rows + "</ul>"
                    : '<p class="cempty">Nothing saved yet.</p>') +
      '<p class="cstatus" hidden></p>' +
    "</div>";

  // Filtering is local: the tags are already in the payload, so narrowing the
  // list is a DOM pass, not a round trip.
  for (const chip of mount.querySelectorAll(".ctag")) {
    chip.onclick = () => {
      const on = chip.classList.toggle("on");
      for (const other of mount.querySelectorAll(".ctag")) {
        if (other !== chip) other.classList.remove("on");
      }
      const want = on ? chip.dataset.tag : null;
      for (const li of mount.querySelectorAll("li")) {
        const tags = (li.dataset.tags || "").split(",").filter(Boolean);
        li.hidden = want !== null && !tags.includes(want);
      }
    };
  }
}

function ideasCard(data, mount) {
  const ideas = data.ideas || [];
  const { action } = cardShell(mount, "recall_ideas", {});
  const rows = ideas.map(i =>
    '<li><span class="citem">' + esc(i.content) + "</span>" +
    '<button class="cx" data-item="' + esc(i.content) + '" title="discard">✕</button></li>'
  ).join("");
  mount.innerHTML =
    '<div class="card ideas">' +
      "<header>Ideas · " + ideas.length + "</header>" +
      (ideas.length ? "<ul>" + rows + "</ul>"
                    : '<p class="cempty">No ideas saved.</p>') +
      '<p class="cstatus" hidden></p>' +
    "</div>";
  for (const b of mount.querySelectorAll(".cx")) {
    b.onclick = () => action("discard_idea", b.dataset.item);
  }
}

function remindersCard(data, mount) {
  const items = data.reminders || [];
  const { action } = cardShell(mount, "recall_reminders", {});
  const rows = items.map(r =>
    '<li><span class="cwhen">' + esc(r.local) + "</span>" +
    '<span class="citem">' + esc(r.content) + "</span>" +
    '<button class="cx" data-item="' + esc(r.content) + '" title="cancel">✕</button></li>'
  ).join("");
  mount.innerHTML =
    '<div class="card reminders">' +
      "<header>Reminders · " + items.length + "</header>" +
      (items.length ? "<ul>" + rows + "</ul>"
                    : '<p class="cempty">Nothing scheduled.</p>') +
      '<p class="cstatus" hidden></p>' +
    "</div>";
  for (const b of mount.querySelectorAll(".cx")) {
    b.onclick = () => action("cancel_reminder", b.dataset.item);
  }
}
```

Register them:

```javascript
const CARDS = { shopping: shoppingCard, notes: notesCard,
                ideas: ideasCard, reminders: remindersCard };
```

- [ ] **Step 5: Add the CSS**

Extend the existing card block, reusing its tokens. Introduce no raw colour
literals — a test asserts the card CSS contains no `#`.

```css
.card .cwhen { color: var(--muted); font-size: 12px; white-space: nowrap; }
.card .ctags { display: flex; flex-wrap: wrap; gap: 6px; padding: 8px 14px;
               border-bottom: 1px solid var(--line); }
.card .ctag { border: 1px solid var(--line); background: transparent;
              color: var(--muted); border-radius: 999px; padding: 2px 10px;
              font-size: 12px; cursor: pointer; }
.card .ctag:hover { background: var(--wash); }
.card .ctag.on { background: var(--accent); color: var(--accent-fg);
                 border-color: var(--accent); }
.card li[hidden] { display: none; }
```

- [ ] **Step 6: Run the tests, the suite, then commit**

The JS parse check added in slice 1 (`test_the_page_javascript_actually_parses`)
must pass — it is the only test here that can tell working code from a broken
page.

```bash
git add wren/communication/chat.html tests/test_chat_renderer.py
git commit -m "feat(chat): notes, ideas and reminders cards on shared scaffolding"
```

---

## Done when

- Asking for notes, ideas or reminders in the web chat renders a card.
- Ideas discard and reminders cancel work inline, no LLM round trip.
- The notes card filters by tag with no round trip.
- Asking a *question* about notes still answers in prose with no card.
- Discord and Telegram get one combined message per recall.
- The suite passes and the page JS parses.
