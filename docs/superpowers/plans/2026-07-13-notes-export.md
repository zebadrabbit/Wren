# Notes/Ideas Markdown Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** "send my notes as a markdown file" / "export my ideas" sends a `.md` file attachment (via Discord's `ATTACH_FILES` permission) covering everything the user has saved via `notes.py` — plain notes and ideas, in two sections — so it can be pulled into another tool instead of only read inline in Discord.

**Architecture:** A single new intent, `export_notes`, added to the existing `wren/notes_plugin.py` (it already owns the `notes.py` data this reads). A new `_build_export(user_id)` helper builds the markdown string in-process; the handler wraps it in `io.BytesIO` and sends it via `discord.File` — no temp files on disk, no new modules.

**Tech Stack:** Python 3.11+, `discord.py` (already a dependency, `discord.File`/`io.BytesIO` only — no new dependency), `pytest`.

## Global Constraints

- Export always covers everything (no tag/date filtering) — matches the "pull it all into another tool" use case, not a report-builder.
- Notes and ideas ship as one combined `.md` file with `## Notes` / `## Ideas` sections, not two separate files.
- Empty case (no notes, no ideas): plain-text "Nothing to export yet." reply, no file sent.
- Export date uses `config.TIMEZONE` (via `ZoneInfo`), matching the pattern already used in `brain.py`'s `_now()` — not server UTC.
- Scope is notes+ideas only — no shopping list, no reminders, no CSV/JSON formats.
- No new dependencies.

---

### Task 1: `export_notes` intent in `wren/notes_plugin.py`

**Files:**
- Modify: `wren/notes_plugin.py`
- Modify: `tests/test_notes_plugin.py`

**Interfaces:**
- Consumes: `notes.search(user_id: int) -> list[dict]` (existing, unfiltered call already used elsewhere in this file) — each dict has `id`, `owner_id`, `content`, `tags` (comma-joined string), `created_at` (ISO string).
- Produces: `notes_plugin.INTENTS` gains `"export_notes"`; `notes_plugin.handle(...)` gains an `export_notes` branch; new private helper `notes_plugin._build_export(user_id: int) -> str | None`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_notes_plugin.py`, add these imports at the top (alongside the existing ones):

```python
import io
import discord
```

Then add these test functions at the end of the file:

```python
def test_export_notes_empty():
    message = _message()
    asyncio.run(notes_plugin.handle("export_notes", message, None, 1, "", [], None, None))
    message.channel.send.assert_awaited_once_with("Nothing to export yet.")
    assert "file" not in message.channel.send.call_args.kwargs

def _sent_file_text(message):
    call = message.channel.send.call_args
    file_obj = call.kwargs["file"]
    file_obj.fp.seek(0)
    return file_obj.fp.read().decode("utf-8"), file_obj.filename

def test_export_notes_notes_only():
    notes.save(1, "buy milk", ["grocery"])
    message = _message()
    asyncio.run(notes_plugin.handle("export_notes", message, None, 1, "", [], None, None))
    message.channel.send.assert_awaited_once()
    text, filename = _sent_file_text(message)
    assert filename.startswith("notes-export-") and filename.endswith(".md")
    assert "## Notes" in text
    assert "buy milk" in text
    assert "(tags: grocery)" in text
    assert "## Ideas\n_None._" in text

def test_export_notes_ideas_only():
    notes.save(1, "build a treehouse", ["idea"])
    message = _message()
    asyncio.run(notes_plugin.handle("export_notes", message, None, 1, "", [], None, None))
    text, _ = _sent_file_text(message)
    assert "## Notes\n_None._" in text
    assert "## Ideas" in text
    assert "- build a treehouse" in text

def test_export_notes_both():
    notes.save(1, "buy milk", ["grocery"])
    notes.save(1, "build a treehouse", ["idea"])
    message = _message()
    asyncio.run(notes_plugin.handle("export_notes", message, None, 1, "", [], None, None))
    text, _ = _sent_file_text(message)
    assert "buy milk" in text
    assert "- build a treehouse" in text
    assert "_None._" not in text

def test_export_notes_owner_isolation():
    notes.save(1, "my note", ["personal"])
    notes.save(2, "their note", ["personal"])
    message = _message()
    asyncio.run(notes_plugin.handle("export_notes", message, None, 1, "", [], None, None))
    text, _ = _sent_file_text(message)
    assert "my note" in text
    assert "their note" not in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_notes_plugin.py -k export_notes -v`
Expected: FAIL — `AttributeError` or the handler falling through to no branch matching `"export_notes"` (no `elif` exists yet), so `message.channel.send` is never awaited and the `assert_awaited_once...` calls fail.

- [ ] **Step 3: Implement `_build_export` and the `export_notes` branch**

In `wren/notes_plugin.py`, change the imports at the top from:

```python
from . import notes
from . import brain
```

to:

```python
import io
from datetime import datetime
from zoneinfo import ZoneInfo
import discord
from . import notes
from . import brain
from . import config
```

Add `"export_notes"` to `INTENTS`:

```python
INTENTS = ["save_note", "recall_notes", "save_idea", "recall_ideas", "discard_idea", "expand_idea", "export_notes"]
```

Add a bullet to `PROMPT_GUIDELINES` (append to the existing triple-quoted string, keeping the same `- name: description` style as the others):

```python
PROMPT_GUIDELINES = """- save_note: user is capturing something for later (grocery item, plan, reminder)
- recall_notes: user wants to retrieve or search past notes; only set tags if the user explicitly names a category to filter by (e.g. "show my grocery notes") — otherwise leave tags empty to see everything, since you don't know what tags were used when notes were saved
- save_idea: user explicitly wants to remember/capture an idea to revisit or expand later (e.g. "remember this idea...", "idea:..."), distinct from save_note's reminders/grocery items/plans
- recall_ideas: user wants to see all their saved ideas
- discard_idea: user wants to delete a previously saved idea; content is a short phrase identifying which idea, not the full idea text
- expand_idea: user wants Wren to elaborate/brainstorm further on a previously saved idea; content is a short phrase identifying which idea, not the full idea text
- export_notes: user wants their notes/ideas as a downloadable file to use elsewhere (e.g. "send my notes as a file", "export my ideas so I can paste them into X")"""
```

Add the `_build_export` helper, above `async def handle(...)`:

```python
def _build_export(user_id: int) -> str | None:
    all_notes = notes.search(user_id)
    plain = [n for n in all_notes if "idea" not in n["tags"].split(",")]
    ideas = [n for n in all_notes if "idea" in n["tags"].split(",")]
    if not plain and not ideas:
        return None

    today = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%Y-%m-%d")
    lines = [f"# Wren Notes Export — {today}", "", "## Notes"]
    if plain:
        for n in plain:
            tag_suffix = f" (tags: {n['tags']})" if n["tags"] else ""
            lines.append(f"- [{n['created_at'][:10]}] {n['content']}{tag_suffix}")
    else:
        lines.append("_None._")
    lines.append("")
    lines.append("## Ideas")
    if ideas:
        for i in ideas:
            lines.append(f"- {i['content']}")
    else:
        lines.append("_None._")
    return "\n".join(lines) + "\n"
```

Add the `export_notes` branch to `handle`, after the `expand_idea` branch (last `elif`):

```python
    elif intent == "export_notes":
        export_text = _build_export(user_id)
        if export_text is None:
            await message.channel.send("Nothing to export yet.")
        else:
            today = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%Y-%m-%d")
            buf = io.BytesIO(export_text.encode("utf-8"))
            await message.channel.send(file=discord.File(buf, filename=f"notes-export-{today}.md"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_notes_plugin.py -v`
Expected: PASS (all tests in the file, including the 5 new `export_notes` tests)

- [ ] **Step 5: Run the full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: PASS (no regressions in other files)

- [ ] **Step 6: Commit**

```bash
git add wren/notes_plugin.py tests/test_notes_plugin.py
git commit -m "feat: add export_notes intent for markdown file export"
```

---

### Task 2: Update `HELP_TEXT` in `wren/bot.py`

**Files:**
- Modify: `wren/bot.py`

**Interfaces:**
- Consumes: nothing new — this is a documentation-only change to the existing `HELP_TEXT` string constant.

- [ ] **Step 1: Add an export example to `HELP_TEXT`**

In `wren/bot.py`, find the `**Ideas**` section of `HELP_TEXT`:

```
**Ideas** (separate from notes — for things to revisit or expand later)
- "remember this idea: build a treehouse"
- "what ideas have I saved"
- "discard the idea about the treehouse"
- "expand on the treehouse idea"
```

Add one line after it:

```
**Ideas** (separate from notes — for things to revisit or expand later)
- "remember this idea: build a treehouse"
- "what ideas have I saved"
- "discard the idea about the treehouse"
- "expand on the treehouse idea"
- "send my notes as a markdown file" — exports all notes and ideas as a downloadable file
```

- [ ] **Step 2: Run the full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: PASS (this is a plain string change in `bot.py`, which has no automated test coverage in this repo — pre-existing condition, module-level `client.run()` makes it unimportable without a real token — so this step just confirms nothing else broke)

- [ ] **Step 3: Commit**

```bash
git add wren/bot.py
git commit -m "docs: mention notes/ideas export in help text"
```

## Post-plan manual check

`bot.py` has no automated test coverage in this repo (pre-existing condition, unrelated to this feature). After both tasks land, manually verify against a running bot instance:
1. With no notes/ideas saved, DM: "export my notes" → confirms "Nothing to export yet."
2. Save a note and an idea, then DM: "send my notes as a markdown file" → confirms a `.md` file attachment arrives with both sections populated, and that it opens/renders correctly as markdown in another tool.
