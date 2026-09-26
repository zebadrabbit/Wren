# Inbound Images and Files Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A photo or PDF sent on Telegram or pasted into the web chat becomes a note with the caption as its text, and comes back out through recall.

**Architecture:** One new keyword argument `files` on `core.handle_message` carries `Inbound` records from a surface to the notes skill; bytes are stored as BLOBs in a new `attachments` table beside `notes`; a shared `read_message` helper gives the web chat and the machine API multipart parsing; a tiny `filetypes` module sniffs bytes so the store and the Telegram plugin agree on what an image is without either importing the other.

**Tech Stack:** Python 3.11, sqlite3, aiohttp (server and Telegram client), pytest, vanilla JS in `chat.html`. Playwright in a scratch venv for one manual pass.

**Spec:** `docs/superpowers/specs/2026-09-26-inbound-images-design.md`

## Global Constraints

- Hard rules in `CLAUDE.md`: a skill never imports transport code; a communication plugin never contains domain logic; every test runs against `WREN_DB` from `tests/conftest.py`, never the real `wren.db`.
- Accepted types by sniff only: JPEG `FF D8 FF`, PNG `89 50 4E 47 0D 0A 1A 0A`, WebP `RIFF....WEBP`, GIF `GIF8`, PDF `%PDF`.
- Limits: `MAX_BYTES = 10 * 1024 * 1024` per file, `MAX_FILES_PER_REPLY = 5`, `_FILES_TTL = 300.0` seconds. No env knobs.
- Replies, verbatim: `That's too big, 10 MB max.` / `I can keep images and PDFs, not that.` / `Couldn't fetch that photo, try again.` / `That photo timed out, send it again.` / `What is this?`
- Run tests with `source venv/bin/activate && pytest -q`. Suite is 1004 passed at the start of this plan; every task ends green.
- The systemd service runs this working tree in place. Do not restart it mid-plan; do the restart in Task 8.
- Never `git stash`. Commit after every task with the `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` trailer.

## File structure

| File | Responsibility |
|---|---|
| `wren/filetypes.py` (new) | `sniff(data) -> str | None`, `IMAGE_MIMES`, `MAX_BYTES`. No other imports from `wren`. |
| `wren/channel.py` | `Inbound` dataclass; `Ctx.files`. |
| `wren/skills/notes_store.py` | `attachments` table; `attach`, `attachments`, `attachment`; `delete` cascades. |
| `wren/core.py` | `files=` on `handle_message`; forced `save_note`; `_pending_files` park. |
| `wren/skills/notes_skill.py` | save with files, recall sends files with marker, export marker. |
| `wren/communication/telegram_plugin.py` | `_incoming` returns wanted files; `_fetch_file`; `sendPhoto` for images. |
| `wren/communication/http_plugin.py` | `read_message(request)` shared helper; `_payload` adds `mime`. |
| `wren/communication/webchat.py` | `post_message` uses `read_message`; response files carry `mime`. |
| `wren/communication/chat.html` | paste/drop, chips, multipart send, inline images, card thumbnails. |
| `tests/test_filetypes.py` (new), `tests/test_notes.py`, `tests/test_core.py`, `tests/test_notes_plugin.py`, `tests/test_telegram_plugin.py`, `tests/test_http_surface.py`, `tests/test_webchat.py` | tests per task |
| `README.md`, `PROJECT_PLAN.md` | docs in Task 8 |

---

### Task 1: `filetypes.sniff` and the attachments store

**Files:**
- Create: `wren/filetypes.py`
- Create: `tests/test_filetypes.py`
- Modify: `wren/skills/notes_store.py` (whole file shown below where changed)
- Test: `tests/test_notes.py` (append)

**Interfaces:**
- Produces: `filetypes.sniff(data: bytes) -> str | None`, `filetypes.IMAGE_MIMES: frozenset[str]`, `filetypes.MAX_BYTES: int`.
- Produces: `notes_store.attach(note_id: int, filename: str, mime: str, data: bytes) -> int` (raises `ValueError("too big")` / `ValueError("unsupported type")`), `notes_store.attachments(note_id: int) -> list[dict]` (keys `id, note_id, filename, mime, size, created_at`), `notes_store.attachment(attachment_id: int) -> dict | None` (same keys plus `data: bytes`), `notes_store.MAX_FILES_PER_REPLY = 5`. `notes_store.delete(note_id)` also deletes that note's attachments.

- [ ] **Step 1: Write the failing sniff tests**

Create `tests/test_filetypes.py`:

```python
import pytest

from wren import filetypes

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 16
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\0" * 16
GIF = b"GIF89a" + b"\0" * 16
PDF = b"%PDF-1.7\n" + b"\0" * 16


@pytest.mark.parametrize("data,mime", [
    (JPEG, "image/jpeg"), (PNG, "image/png"), (WEBP, "image/webp"),
    (GIF, "image/gif"), (PDF, "application/pdf"),
])
def test_sniff_knows_the_five_accepted_types(data, mime):
    assert filetypes.sniff(data) == mime


def test_sniff_rejects_a_renamed_executable_and_empty_bytes():
    assert filetypes.sniff(b"MZ\x90\x00" + b"\0" * 16) is None
    assert filetypes.sniff(b"") is None
    # RIFF alone is not WebP: a WAV file starts RIFF....WAVE
    assert filetypes.sniff(b"RIFF\x10\x00\x00\x00WAVEfmt ") is None


def test_image_mimes_are_exactly_the_four_image_types():
    assert filetypes.IMAGE_MIMES == {"image/jpeg", "image/png", "image/webp", "image/gif"}


def test_max_bytes_is_ten_megabytes():
    assert filetypes.MAX_BYTES == 10 * 1024 * 1024
```

- [ ] **Step 2: Run the sniff tests to verify they fail**

Run: `source venv/bin/activate && pytest -q tests/test_filetypes.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'wren.filetypes'`

- [ ] **Step 3: Create `wren/filetypes.py`**

```python
"""What a file is, decided from its first bytes.

Shared by the notes store (what it will keep) and the Telegram plugin (how to
send it back), so neither imports the other. Filenames and declared mimes
are never trusted: a renamed executable sniffs as nothing and is refused.
"""

MAX_BYTES = 10 * 1024 * 1024

IMAGE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
ACCEPTED = IMAGE_MIMES | {"application/pdf"}


def sniff(data: bytes) -> str | None:
    """The accepted mime type these bytes are, or None."""
    head = data[:12]
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"GIF8"):
        return "image/gif"
    if head.startswith(b"%PDF"):
        return "application/pdf"
    return None
```

- [ ] **Step 4: Run the sniff tests to verify they pass**

Run: `source venv/bin/activate && pytest -q tests/test_filetypes.py`
Expected: `8 passed`

- [ ] **Step 5: Write the failing store tests**

Append to `tests/test_notes.py` (it already imports `from wren.skills import notes_store as notes`, and conftest gives every test its own `WREN_DB`):

```python


# ── attachments ───────────────────────────────────────────────────────────

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 32
PDF = b"%PDF-1.7\n" + b"\0" * 32


def test_attach_stores_bytes_and_the_sniffed_mime_wins_over_the_declared_one():
    notes.init_db()
    note_id = notes.save(1, "tyre receipt", [])
    att_id = notes.attach(note_id, "receipt.png", "image/png", JPEG)   # lied: it is a JPEG
    rows = notes.attachments(note_id)
    assert [r["id"] for r in rows] == [att_id]
    assert rows[0]["mime"] == "image/jpeg"
    assert rows[0]["size"] == len(JPEG)
    assert "data" not in rows[0]                      # metadata only
    assert notes.attachment(att_id)["data"] == JPEG
    assert notes.attachment(att_id + 100) is None


def test_attach_refuses_oversize_and_unknown_types_without_writing():
    import pytest
    from wren import filetypes
    notes.init_db()
    note_id = notes.save(1, "n", [])
    with pytest.raises(ValueError, match="too big"):
        notes.attach(note_id, "big.jpg", "image/jpeg", JPEG + b"\0" * filetypes.MAX_BYTES)
    with pytest.raises(ValueError, match="unsupported type"):
        notes.attach(note_id, "evil.jpg", "image/jpeg", b"MZ\x90\x00" + b"\0" * 32)
    assert notes.attachments(note_id) == []


def test_delete_removes_the_notes_attachments_too():
    notes.init_db()
    keep = notes.save(1, "keep", [])
    gone = notes.save(1, "gone", [])
    keep_att = notes.attach(keep, "a.pdf", "application/pdf", PDF)
    notes.attach(gone, "b.pdf", "application/pdf", PDF)
    assert notes.delete(gone) is True
    assert notes.attachments(gone) == []
    assert notes.attachment(keep_att) is not None


def test_attachments_come_back_in_insertion_order():
    notes.init_db()
    note_id = notes.save(1, "n", [])
    first = notes.attach(note_id, "1.pdf", "application/pdf", PDF)
    second = notes.attach(note_id, "2.pdf", "application/pdf", PDF)
    assert [r["id"] for r in notes.attachments(note_id)] == [first, second]


def test_max_files_per_reply_is_five():
    assert notes.MAX_FILES_PER_REPLY == 5
```

- [ ] **Step 6: Run the store tests to verify they fail**

Run: `source venv/bin/activate && pytest -q tests/test_notes.py -k "attach or delete_removes or max_files"`
Expected: FAIL with `AttributeError: module 'wren.skills.notes_store' has no attribute 'attach'` (and `MAX_FILES_PER_REPLY`)

- [ ] **Step 7: Implement the store**

In `wren/skills/notes_store.py`, change the imports and `init_db`, add the three functions, and make `delete` cascade:

```python
import sqlite3
from datetime import datetime, timezone

from .. import db
from .. import filetypes

# How many attachments one reply will send back. A broad "show my notes"
# must not dump a gallery into the chat.
MAX_FILES_PER_REPLY = 5

def init_db() -> None:
    with db.conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id   TEXT NOT NULL,
                content    TEXT NOT NULL,
                tags       TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        # Bytes live in SQLite on purpose: one file to back up, and per-test
        # isolation and db.dry_run() cover them with no extra work (a file on
        # disk would leak out of a dry run).
        # ponytail: move blobs to disk if the database passes a few hundred MB
        # -- db.dry_run() copies the whole file each time.
        con.execute("""
            CREATE TABLE IF NOT EXISTS attachments (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                note_id    INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
                filename   TEXT NOT NULL,
                mime       TEXT NOT NULL,
                size       INTEGER NOT NULL,
                data       BLOB NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
```

Replace `delete`:

```python
def delete(note_id: int) -> bool:
    with db.conn() as con:
        # Explicit, not via the REFERENCES cascade: SQLite only honours ON
        # DELETE CASCADE when PRAGMA foreign_keys=ON is set per connection,
        # and db.conn() does not set it. Same transaction either way.
        con.execute("DELETE FROM attachments WHERE note_id=?", (note_id,))
        cur = con.execute("DELETE FROM notes WHERE id=?", (note_id,))
        return cur.rowcount > 0
```

Append at the end of the file:

```python

def attach(note_id: int, filename: str, mime: str, data: bytes) -> int:
    """Store one file against a note. `mime` is what the surface declared; the
    sniffed type is what gets stored, and a file that sniffs as nothing is
    refused -- a renamed executable must not become a "PDF"."""
    if len(data) > filetypes.MAX_BYTES:
        raise ValueError("too big")
    sniffed = filetypes.sniff(data)
    if sniffed is None:
        raise ValueError("unsupported type")
    ts = datetime.now(timezone.utc).isoformat()
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO attachments (note_id, filename, mime, size, data, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (note_id, filename, sniffed, len(data), data, ts),
        )
        return cur.lastrowid

def attachments(note_id: int) -> list[dict]:
    """Metadata only -- never the bytes, so listing a note is cheap."""
    with db.conn() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, note_id, filename, mime, size, created_at FROM attachments "
            "WHERE note_id=? ORDER BY id",
            (note_id,),
        ).fetchall()
    return [dict(r) for r in rows]

def attachment(attachment_id: int) -> dict | None:
    with db.conn() as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
    return dict(row) if row else None
```

- [ ] **Step 8: Run the whole suite**

Run: `source venv/bin/activate && pytest -q`
Expected: all pass (1004 + 13 new).

- [ ] **Step 9: Commit**

```bash
git add wren/filetypes.py tests/test_filetypes.py wren/skills/notes_store.py tests/test_notes.py
git commit -m "feat(notes): attachments table, sniffed types, cascade on delete

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `Inbound` on the channel and `files=` through core

**Files:**
- Modify: `wren/channel.py` (add `Inbound`, add `Ctx.files`)
- Modify: `wren/core.py` (`_pending_files`, `_FILES_TTL`, `_take_parked`, `handle_message`)
- Test: `tests/test_core.py` (append)

**Interfaces:**
- Consumes: nothing from Task 1 at runtime (core does not touch the store).
- Produces: `channel.Inbound(filename: str, mime: str, data: bytes)`; `Ctx.files: list[Inbound]`; `core.handle_message(user_id, text, channel, *, source="text", files=None)`; module constants `core._FILES_TTL = 300.0`, `core._pending_files: dict[int, tuple[list[Inbound], float]]`.

- [ ] **Step 1: Write the failing core tests**

Append to `tests/test_core.py`:

```python


# --- inbound files -------------------------------------------------------

from wren.channel import Inbound

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 32


def _photo(name="a.jpg"):
    return Inbound(filename=name, mime="image/jpeg", data=JPEG)


@pytest.fixture
def notes_handler(monkeypatch):
    """A fake notes skill that records the Ctx it was handed."""
    seen = []

    class FakeNotes:
        INTENTS = ["save_note"]

        @staticmethod
        async def handle(intent, ctx):
            seen.append((intent, ctx))
            await ctx.channel.send("saved")
    monkeypatch.setattr(registry, "INTENT_HANDLERS", {"save_note": FakeNotes, "chat": None})
    monkeypatch.setattr(registry, "is_enabled", lambda p: True)
    monkeypatch.setattr(core, "_pending_files", {})
    return seen


def test_files_with_a_caption_force_save_note_and_keep_the_classifier_tags(detected, notes_handler):
    detected(intent="get_weather", content="whatever the model thought", tags=["receipt"])
    ch = CollectingChannel()
    asyncio.run(core.handle_message(OWNER, "tyre place receipt", ch, files=[_photo()]))
    (intent, ctx), = notes_handler
    assert intent == "save_note"
    assert ctx.content == "tyre place receipt"       # the caption, not the model's extraction
    assert ctx.tags == ["receipt"]
    assert [f.filename for f in ctx.files] == ["a.jpg"]
    assert ch.sent == ["saved"]


def test_files_without_a_caption_are_parked_and_wren_asks(detected, notes_handler):
    ch = CollectingChannel()
    asyncio.run(core.handle_message(OWNER, "", ch, files=[_photo()]))
    assert ch.sent == ["What is this?"]
    assert notes_handler == []
    assert detected.calls == []                       # no LLM call for a bare photo
    assert OWNER in core._pending_files


def test_the_next_message_becomes_the_caption_for_parked_files(detected, notes_handler):
    ch = CollectingChannel()
    asyncio.run(core.handle_message(OWNER, "", ch, files=[_photo("park.jpg")]))
    asyncio.run(core.handle_message(OWNER, "what's the weather", ch, files=None))
    (intent, ctx), = notes_handler
    assert intent == "save_note"
    assert ctx.content == "what's the weather"        # whatever it says, it is the caption
    assert [f.filename for f in ctx.files] == ["park.jpg"]
    assert OWNER not in core._pending_files


def test_a_second_bare_photo_replaces_the_parked_one(detected, notes_handler):
    ch = CollectingChannel()
    asyncio.run(core.handle_message(OWNER, "", ch, files=[_photo("first.jpg")]))
    asyncio.run(core.handle_message(OWNER, "", ch, files=[_photo("second.jpg")]))
    assert ch.sent == ["What is this?", "What is this?"]
    files, _deadline = core._pending_files[OWNER]
    assert [f.filename for f in files] == ["second.jpg"]


def test_expired_parked_files_are_reported_then_the_text_is_handled_normally(detected, notes_handler, monkeypatch):
    memory.init_db()                                   # the chat branch reads memories
    ch = CollectingChannel()
    asyncio.run(core.handle_message(OWNER, "", ch, files=[_photo()]))
    files, _ = core._pending_files[OWNER]
    core._pending_files[OWNER] = (files, time.monotonic() - 1)      # already expired
    detected(intent="chat", content="", tags=[])
    monkeypatch.setattr(brain, "chat", lambda *a, **k: "hello back")
    asyncio.run(core.handle_message(OWNER, "hello", ch))
    assert ch.sent[1] == "That photo timed out, send it again."
    assert ch.sent[2] == "hello back"
    assert notes_handler == []
    assert OWNER not in core._pending_files


def test_a_strangers_files_are_dropped_with_their_text(detected, notes_handler):
    ch = CollectingChannel()
    asyncio.run(core.handle_message(999, "mine", ch, files=[_photo()]))
    assert ch.sent == [] and notes_handler == [] and 999 not in core._pending_files


def test_a_pending_yes_no_is_answered_before_parked_files_are_used(detected, notes_handler, monkeypatch):
    ch = CollectingChannel()
    asyncio.run(core.handle_message(OWNER, "", ch, files=[_photo()]))
    fired = []

    class FakeDestructive:
        @staticmethod
        async def handle(intent, ctx):
            fired.append(intent)
    monkeypatch.setitem(registry.INTENT_HANDLERS, "clear_shopping", FakeDestructive)
    core._pending[OWNER] = ("clear_shopping", Ctx(user_id=OWNER, channel=ch), time.monotonic() + 60)
    asyncio.run(core.handle_message(OWNER, "yes", ch))
    assert fired == ["clear_shopping"]
    assert OWNER in core._pending_files                # still parked for the next message


def test_files_when_notes_is_switched_off_say_so(detected, notes_handler, monkeypatch):
    monkeypatch.setattr(registry, "is_enabled", lambda p: False)
    ch = CollectingChannel()
    asyncio.run(core.handle_message(OWNER, "receipt", ch, files=[_photo()]))
    assert ch.sent == ["Notes is switched off, so I can't keep that."]
    assert notes_handler == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `source venv/bin/activate && pytest -q tests/test_core.py -k "files or parked or caption or strangers_files or notes_is_switched"`
Expected: FAIL with `ImportError: cannot import name 'Inbound' from 'wren.channel'`

- [ ] **Step 3: Add `Inbound` and `Ctx.files` to `wren/channel.py`**

Directly above `@dataclass\nclass Ctx:` add:

```python
@dataclass
class Inbound:
    """A file a surface received with a message. The mirror of send_file.

    `mime` is whatever the surface declared; the notes store re-sniffs the
    bytes and stores what they actually are."""

    filename: str
    mime: str
    data: bytes


```

Inside `Ctx`, after the `source: str = "text"` field, add:

```python
    # Files that arrived with the words. Only the notes skill reads these.
    files: list[Inbound] = field(default_factory=list)
```

- [ ] **Step 4: Add the park and the flow to `wren/core.py`**

Change the import line `from .channel import Channel, Ctx` (find it near the top) to:

```python
from .channel import Channel, Ctx, Inbound
```

Directly below the `_NO = re.compile(...)` block add:

```python
# Files that arrived without a caption, waiting for one: user_id ->
# (files, expires_at). The next message from that user is the caption,
# whatever it says -- a wrong caption is one note to delete, while guessing
# whether "what's the weather" was meant as one is worse. Process-local for
# the same reason as _pending.
_pending_files: dict[int, tuple[list[Inbound], float]] = {}
_FILES_TTL = 300.0


def _take_parked(user_id: int) -> tuple[list[Inbound], bool] | None:
    """(files, still_fresh) for this user's parked files, removed; None if
    nothing was parked. Expired entries come back with False so the caller
    can say so instead of silently dropping a photo."""
    entry = _pending_files.pop(user_id, None)
    if entry is None:
        return None
    files, deadline = entry
    return files, deadline > time.monotonic()
```

In `handle_message`, change the signature and the empty-text guard:

```python
async def handle_message(user_id: int, text: str, channel: Channel, *, source: str = "text",
                         files: list[Inbound] | None = None) -> None:
    """Transport-free dispatch. Surfaces authenticate the caller, build a
    Channel, and call this. Nothing below here knows what a Discord is.

    `source` is "text" or "voice" — voice gets a yes/no confirmation before
    running anything destructive, since transcription mis-hears; text is
    unaffected and always acts immediately.

    `files` are attachments that arrived with the words. With a caption they
    become a note; without one Wren asks what the photo is and the next
    message answers."""
    # authorization gate — surfaces do authn (who are you), this does authz.
    # Kept here rather than per-surface so a new surface cannot forget it.
    if user_id not in config.id_to_name():
        return

    text = (text or "").strip()
    files = list(files or [])
    if not text and not files:
        return
```

Directly after the whole `pending = _take_pending(user_id)` / `if pending:` block (i.e. after its `return` inside the `_YES` branch, at the same indentation as `pending = ...`), and before `try:`, add:

```python
    # A caption arriving for parked files, or a bare photo to park. After the
    # yes/no check on purpose: an answer to a question core asked is still an
    # answer, and the photo keeps waiting.
    if text and not files:
        parked = _take_parked(user_id)
        if parked is not None:
            parked_files, fresh = parked
            if fresh:
                files = parked_files
            else:
                await channel.send("That photo timed out, send it again.")
    if files and not text:
        _pending_files[user_id] = (files, time.monotonic() + _FILES_TTL)
        await channel.send("What is this?")
        await channel.ack("done")
        return
```

Inside the `try:` block, replace the two lines

```python
        result = await asyncio.to_thread(brain.detect_intent, user_id, text, history)
        intent = result.get("intent", "chat")
```

with:

```python
        result = await asyncio.to_thread(brain.detect_intent, user_id, text, history)
        intent = result.get("intent", "chat")
        if files:
            # Without vision there is nothing else Wren can do with a picture,
            # and "receipt from the tyre place" must not gamble on a small
            # model's guess. The classifier still ran: its tags are kept.
            intent = "save_note"
            result["content"] = text
            notes_plugin = registry.INTENT_HANDLERS.get("save_note")
            if notes_plugin is None or not registry.is_enabled(notes_plugin):
                await channel.send("Notes is switched off, so I can't keep that.")
                await channel.ack("done")
                return
```

And in the `Ctx(...)` construction that follows, add `files=files,` after `source=source,`.

- [ ] **Step 5: Run the core tests**

Run: `source venv/bin/activate && pytest -q tests/test_core.py`
Expected: all pass, including the 8 new ones.

- [ ] **Step 6: Run the whole suite**

Run: `source venv/bin/activate && pytest -q`
Expected: all pass. (The `registry.INTENT_HANDLERS` patch in the fixture includes a `"chat": None` entry only so `.get` works; core never dispatches `chat` through the table.)

- [ ] **Step 7: Commit**

```bash
git add wren/channel.py wren/core.py tests/test_core.py
git commit -m "feat(core): files ride handle_message; captioned -> save_note, bare -> ask once

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Notes skill saves, recalls and exports attachments

**Files:**
- Modify: `wren/skills/notes_skill.py`
- Test: `tests/test_notes_plugin.py` (append)

**Interfaces:**
- Consumes: `notes_store.attach/attachments/attachment/MAX_FILES_PER_REPLY` (Task 1), `Ctx.files` / `Inbound` (Task 2).
- Produces: `save_note` replies `Saved, 1 image.` / `Saved, 2 files.`; card rows carry `"files": [{"id", "filename", "mime"}]`; recall and export lines end with ` (1 image)` / ` (2 files)`; files sent via `ctx.channel.send_file`.

- [ ] **Step 1: Write the failing skill tests**

Append to `tests/test_notes_plugin.py`:

```python


# ── attachments ───────────────────────────────────────────────────────────

from wren.channel import Inbound

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 32
PDF = b"%PDF-1.7\n" + b"\0" * 32


def _img(name="a.jpg"):
    return Inbound(filename=name, mime="image/jpeg", data=JPEG)


def test_save_note_with_one_image_reports_it_and_stores_it():
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("save_note", Ctx(user_id=1, channel=ch, content="tyre receipt", files=[_img()])))
    assert ch.sent == ["Saved, 1 image."]
    note = notes.list_recent(1)[0]
    assert [a["filename"] for a in notes.attachments(note["id"])] == ["a.jpg"]


def test_save_note_counts_mixed_files_as_files():
    ch = CollectingChannel()
    files = [_img(), Inbound(filename="manual.pdf", mime="application/pdf", data=PDF)]
    asyncio.run(notes_plugin.handle("save_note", Ctx(user_id=1, channel=ch, content="dishwasher", files=files)))
    assert ch.sent == ["Saved, 2 files."]


def test_save_note_keeps_the_good_files_and_names_the_refused_one():
    ch = CollectingChannel()
    files = [_img(), Inbound(filename="evil.jpg", mime="image/jpeg", data=b"MZ\x90\x00" + b"\0" * 32)]
    asyncio.run(notes_plugin.handle("save_note", Ctx(user_id=1, channel=ch, content="hm", files=files)))
    assert ch.sent == ["Saved, 1 image. Skipped evil.jpg: I can keep images and PDFs, not that."]
    note = notes.list_recent(1)[0]
    assert len(notes.attachments(note["id"])) == 1


def test_save_note_with_only_refused_files_and_no_caption_saves_nothing():
    ch = CollectingChannel()
    files = [Inbound(filename="evil.jpg", mime="image/jpeg", data=b"MZ\x90\x00" + b"\0" * 32)]
    asyncio.run(notes_plugin.handle("save_note", Ctx(user_id=1, channel=ch, content="", files=files)))
    assert ch.sent == ["Skipped evil.jpg: I can keep images and PDFs, not that."]
    assert notes.list_recent(1) == []


def test_recall_card_marks_attached_notes_and_sends_the_files():
    note_id = notes.save(1, "tyre receipt", [])
    notes.attach(note_id, "receipt.jpg", "image/jpeg", JPEG)
    notes.save(1, "plain note", [])
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("recall_notes", Ctx(user_id=1, channel=ch, content="")))
    card = ch.cards[0]
    assert card["kind"] == "notes"
    by_content = {r["content"]: r for r in card["data"]["notes"]}
    assert by_content["tyre receipt"]["files"] == [{"id": 1, "filename": "receipt.jpg", "mime": "image/jpeg"}]
    assert by_content["plain note"]["files"] == []
    assert "tyre receipt (1 image)" in ch.sent[0]
    assert "plain note" in ch.sent[0] and "plain note (" not in ch.sent[0]
    assert ch.files == [(JPEG, "receipt.jpg")]


def test_recall_sends_at_most_five_files_across_the_reply():
    for i in range(7):
        note_id = notes.save(1, f"n{i}", [])
        notes.attach(note_id, f"{i}.jpg", "image/jpeg", JPEG)
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("recall_notes", Ctx(user_id=1, channel=ch, content="")))
    assert len(ch.files) == 5


def test_recall_with_a_question_sends_the_matching_notes_files_too():
    note_id = notes.save(1, "tyre receipt", [])
    notes.attach(note_id, "receipt.jpg", "image/jpeg", JPEG)
    ch = CollectingChannel()
    with patch.object(brain, "recall", return_value="You paid the tyre place."):
        asyncio.run(notes_plugin.handle("recall_notes", Ctx(user_id=1, channel=ch, content="what did I pay")))
    assert ch.sent == ["You paid the tyre place."]
    assert ch.files == [(JPEG, "receipt.jpg")]


def test_export_marks_attached_notes():
    note_id = notes.save(1, "tyre receipt", [])
    notes.attach(note_id, "a.pdf", "application/pdf", PDF)
    notes.attach(note_id, "b.pdf", "application/pdf", PDF)
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("export_notes", Ctx(user_id=1, channel=ch)))
    text = ch.files[0][0].decode()
    assert "tyre receipt (2 files)" in text


def test_discard_idea_with_an_attachment_removes_it_too():
    note_id = notes.save(1, "kayak", ["idea"])
    att = notes.attach(note_id, "k.jpg", "image/jpeg", JPEG)
    ch = CollectingChannel()
    asyncio.run(notes_plugin.handle("discard_idea", Ctx(user_id=1, channel=ch, content="kayak")))
    assert notes.attachment(att) is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `source venv/bin/activate && pytest -q tests/test_notes_plugin.py -k "image or files or marks or attachment"`
Expected: FAIL. The first with `AssertionError: assert ['Saved. 🐣'] == ['Saved, 1 image.']` (the emote varies), others with `KeyError: 'files'` or missing markers. `test_discard_idea_with_an_attachment_removes_it_too` passes already thanks to Task 1's cascade; that is fine, it guards the cascade from the skill's side.

- [ ] **Step 3: Implement in `wren/skills/notes_skill.py`**

Add after the `PROMPT_GUIDELINES` block:

```python

def _describe(atts: list) -> str:
    """"1 image" / "3 images" / "2 files" for a list of attachment rows or
    Inbound records -- anything with a .mime or ["mime"]."""
    from .. import filetypes
    mimes = [a["mime"] if isinstance(a, dict) else a.mime for a in atts]
    n = len(mimes)
    if all(m in filetypes.IMAGE_MIMES for m in mimes):
        return f"{n} image" + ("" if n == 1 else "s")
    return f"{n} file" + ("" if n == 1 else "s")


def _marker(note_id: int) -> str:
    atts = notes.attachments(note_id)
    return f" ({_describe(atts)})" if atts else ""


async def _send_attachments(ctx: Ctx, note_ids: list[int]) -> None:
    """Send the files of these notes, in note order, stopping at the cap."""
    sent = 0
    for note_id in note_ids:
        for meta in notes.attachments(note_id):
            if sent >= notes.MAX_FILES_PER_REPLY:
                return
            row = notes.attachment(meta["id"])
            if row is None:
                continue
            await ctx.channel.send_file(row["data"], row["filename"])
            sent += 1


_REFUSALS = {
    "too big": "That's too big, 10 MB max.",
    "unsupported type": "I can keep images and PDFs, not that.",
}
```

In `_build_export`, change the plain-notes line to carry the marker:

```python
            lines.append(f"- [{n['created_at'][:10]}] {n['content']}{_marker(n['id'])}{tag_suffix}")
```

Replace the `save_note` branch of `handle`:

```python
    if intent == "save_note":
        if not ctx.files:
            notes.save(ctx.user_id, ctx.content, ctx.tags)
            await ctx.channel.send(flourish.flourish("Saved."))
            return
        note_id = notes.save(ctx.user_id, ctx.content, ctx.tags)
        kept, skipped = [], []
        for f in ctx.files:
            try:
                notes.attach(note_id, f.filename, f.mime, f.data)
                kept.append(f)
            except ValueError as e:
                skipped.append(f"Skipped {f.filename}: {_REFUSALS.get(str(e), str(e))}")
        if not kept and not ctx.content.strip():
            # every file refused and nothing to say: a note with no body and
            # no file is not worth keeping
            notes.delete(note_id)
            await ctx.channel.send(" ".join(skipped))
            return
        parts = ([f"Saved, {_describe(kept)}."] if kept else []) + skipped
        await ctx.channel.send(" ".join(parts))
```

In the `recall_notes` branch, change the question path:

```python
            if not matches:
                await ctx.channel.send("No notes found.")
            else:
                summary = brain.recall(matches, ctx.content)
                await ctx.channel.send(summary)
                await _send_attachments(ctx, [n["id"] for n in matches])
```

and the card path: build rows with files, mark the text, send files after the card:

```python
            rows = [{"content": n["content"],
                     "tags": [t for t in n["tags"].split(",") if t],
                     "created_at": n["created_at"],
                     "files": [{"id": a["id"], "filename": a["filename"], "mime": a["mime"]}
                               for a in notes.attachments(n["id"])]}
                    for n in matches]
            if rows:
                text = "\n".join(
                    f"[{n['created_at'][:10]}] {n['content']}{_marker(n['id'])}"
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
            await _send_attachments(ctx, [n["id"] for n in matches])
```

- [ ] **Step 4: Run the skill tests**

Run: `source venv/bin/activate && pytest -q tests/test_notes_plugin.py`
Expected: all pass. If `test_save_note_keeps_the_good_files...` fails on wording, match the string in the test exactly: `Saved, 1 image. Skipped evil.jpg: I can keep images and PDFs, not that.`

- [ ] **Step 5: Run the whole suite**

Run: `source venv/bin/activate && pytest -q`
Expected: all pass. No existing test asserts the exact shape of a notes card row (checked 2026-09-26), so the added `files` key breaks nothing.

- [ ] **Step 6: Commit**

```bash
git add wren/skills/notes_skill.py tests/test_notes_plugin.py
git commit -m "feat(notes): save, recall and export carry attachments

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Telegram accepts photos and documents, sends photos back as photos

**Files:**
- Modify: `wren/communication/telegram_plugin.py` (`_incoming`, `_poll_once`, new `_fetch_file`, `TelegramChannel.send_file`)
- Test: `tests/test_telegram_plugin.py` (append)

**Interfaces:**
- Consumes: `core.handle_message(..., files=)`, `channel.Inbound` (Task 2), `filetypes.sniff/IMAGE_MIMES/MAX_BYTES` (Task 1).
- Produces: `_incoming(update) -> tuple[int, str, list[dict]] | None` where each dict is `{"file_id", "filename", "mime", "file_size"}`; `async _fetch_file(file_id: str) -> bytes`.

- [ ] **Step 1: Write the failing Telegram tests**

Append to `tests/test_telegram_plugin.py`:

```python


# ── inbound files ─────────────────────────────────────────────────────────

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 32


def _photo_update(caption="tyre receipt", size=1234, **extra):
    return _update(text=None, caption=caption,
                   photo=[{"file_id": "small", "file_size": 100},
                          {"file_id": "big", "file_size": size}], **extra)


def test_incoming_photo_wants_the_largest_size_and_uses_the_caption():
    user_id, text, wanted = telegram_plugin._incoming(_photo_update())
    assert (user_id, text) == (42, "tyre receipt")
    assert wanted == [{"file_id": "big", "filename": "photo.jpg", "mime": "image/jpeg", "file_size": 1234}]


def test_incoming_document_of_an_accepted_type_is_wanted_video_is_not():
    doc = _update(text=None, caption="manual",
                  document={"file_id": "d1", "file_name": "manual.pdf", "mime_type": "application/pdf", "file_size": 5})
    _u, text, wanted = telegram_plugin._incoming(doc)
    assert text == "manual"
    assert wanted == [{"file_id": "d1", "filename": "manual.pdf", "mime": "application/pdf", "file_size": 5}]
    vid = _update(text=None, document={"file_id": "v", "file_name": "x.mp4", "mime_type": "video/mp4"})
    assert telegram_plugin._incoming(vid) is None


def test_incoming_plain_text_has_no_files():
    assert telegram_plugin._incoming(_update(text="hi")) == (42, "hi", [])


def test_uncaptioned_photo_is_still_incoming_with_empty_text():
    _u, text, wanted = telegram_plugin._incoming(_photo_update(caption=None))
    assert text == "" and len(wanted) == 1


def test_poll_downloads_the_photo_and_hands_core_an_inbound():
    api = AsyncMock(side_effect=[[_photo_update()], {"file_path": "photos/1.jpg"}])
    with patch.object(telegram_plugin, "_api", new=api), \
         patch.object(telegram_plugin, "_download", new=AsyncMock(return_value=JPEG)) as dl, \
         patch.object(core, "handle_message", new=AsyncMock()) as handle:
        asyncio.run(telegram_plugin._poll_once(None))
    assert api.await_args_list[1].args == ("getFile",)
    assert api.await_args_list[1].kwargs == {"data": {"file_id": "big"}}
    dl.assert_awaited_once_with("photos/1.jpg")
    _user, text, _channel = handle.await_args.args
    files = handle.await_args.kwargs["files"]
    assert text == "tyre receipt"
    assert [(f.filename, f.mime, f.data) for f in files] == [("photo.jpg", "image/jpeg", JPEG)]


def test_oversize_photo_is_refused_before_any_download():
    # No caption either, so there is nothing left to hand core; a caption
    # would still reach core with files=[] (see the failed-download test).
    from wren import filetypes
    api = AsyncMock(return_value=[_photo_update(caption=None, size=filetypes.MAX_BYTES + 1)])
    sent = AsyncMock()
    with patch.object(telegram_plugin, "_api", new=api), \
         patch.object(telegram_plugin, "_download", new=AsyncMock()) as dl, \
         patch.object(telegram_plugin, "_send_text", new=sent), \
         patch.object(core, "handle_message", new=AsyncMock()) as handle:
        asyncio.run(telegram_plugin._poll_once(None))
    dl.assert_not_called()
    sent.assert_awaited_once_with(42, "That's too big, 10 MB max.")
    handle.assert_not_called()                 # nothing left to hand over


def test_failed_download_is_reported_and_the_caption_still_reaches_core():
    api = AsyncMock(side_effect=[[_photo_update()], aiohttp.ClientError("boom")])
    sent = AsyncMock()
    with patch.object(telegram_plugin, "_api", new=api), \
         patch.object(telegram_plugin, "_send_text", new=sent), \
         patch.object(core, "handle_message", new=AsyncMock()) as handle:
        asyncio.run(telegram_plugin._poll_once(None))
    sent.assert_awaited_once_with(42, "Couldn't fetch that photo, try again.")
    _user, text, _channel = handle.await_args.args
    assert text == "tyre receipt" and handle.await_args.kwargs["files"] == []


def test_sticker_is_still_skipped():
    assert telegram_plugin._incoming(_update(text=None, sticker={"file_id": "s"})) is None


def test_send_file_posts_a_photo_for_image_bytes_and_a_document_otherwise():
    api = AsyncMock(return_value={})
    with patch.object(telegram_plugin, "_api", new=api):
        asyncio.run(telegram_plugin.TelegramChannel(42).send_file(JPEG, "a.jpg"))
        asyncio.run(telegram_plugin.TelegramChannel(42).send_file(b"%PDF-1.7\n", "a.pdf"))
    assert api.await_args_list[0].args == ("sendPhoto",)
    assert _form_fields(api.await_args_list[0].kwargs["data"])["photo"] == JPEG
    assert api.await_args_list[1].args == ("sendDocument",)
```

Also update the existing `test_private_text_message_reaches_core`: it unpacks `user_id, text, channel = handle.await_args.args`, which still holds; no change needed. The existing `test_non_text_update_is_skipped_without_raising` parametrize includes an empty text message `_update(update_id=3, text="")`; that still skips (no files). No change.

- [ ] **Step 2: Run them to verify they fail**

Run: `source venv/bin/activate && pytest -q tests/test_telegram_plugin.py -k "incoming or download or oversize or sticker or send_file_posts"`
Expected: FAIL with `ValueError: too many values to unpack` / `AttributeError: ... has no attribute '_download'`.

- [ ] **Step 3: Implement in `wren/communication/telegram_plugin.py`**

Add to the imports at the top:

```python
from .. import filetypes
from ..channel import Inbound
```

Replace `_incoming`:

```python
def _incoming(update: dict) -> tuple[int, str, list[dict]] | None:
    """(user_id, text, wanted_files) for an update Wren should act on, else None.

    Everything else is skipped in one place: edited messages and channel posts
    arrive under different keys than "message", groups are filtered the way
    discord_plugin filters to DMs, and stickers/voice notes/video have neither
    "text" nor an accepted file. A photo, or a document of an accepted type,
    is the one non-text message that is acted on; its caption is the text.
    `wanted_files` are descriptors -- downloading is async and happens in
    _poll_once, so this stays a pure function.
    """
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    if (message.get("chat") or {}).get("type") != "private":
        return None
    user_id = (message.get("from") or {}).get("id")
    if user_id is None:
        return None
    wanted: list[dict] = []
    photo = message.get("photo")
    if isinstance(photo, list) and photo:
        largest = photo[-1]                      # Telegram orders sizes ascending
        wanted.append({"file_id": largest.get("file_id"), "filename": "photo.jpg",
                       "mime": "image/jpeg", "file_size": largest.get("file_size")})
    doc = message.get("document")
    if isinstance(doc, dict) and doc.get("mime_type") in filetypes.ACCEPTED:
        wanted.append({"file_id": doc.get("file_id"), "filename": doc.get("file_name") or "file",
                       "mime": doc["mime_type"], "file_size": doc.get("file_size")})
    text = message.get("text") or message.get("caption") or ""
    location = message.get("location")
    if not text and not wanted and isinstance(location, dict):
        # The paperclip "Location" share is how a phone hands over its GPS fix.
        # Rendered as the words a user would type so weather_skill.set_location
        # stays the only write path -- translation, not domain logic.
        text = f"my location is {location.get('latitude')}, {location.get('longitude')}"
    if not text and not wanted:
        return None
    return user_id, text, wanted
```

Add after `_api`:

```python
async def _download(file_path: str) -> bytes:
    """GET a file Telegram has told us the path of (via getFile). Same
    timeout and the same token scrubbing as _api, for the same reason."""
    url = f"{API_ROOT}/file/bot{config.TELEGRAM_TOKEN}/{file_path}"
    client_timeout = aiohttp.ClientTimeout(total=_CLIENT_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                return await resp.read()
    except aiohttp.ClientError as e:
        detail = str(e)
        if config.TELEGRAM_TOKEN:
            detail = detail.replace(config.TELEGRAM_TOKEN, "<token>")
        raise aiohttp.ClientError(f"{type(e).__name__}: {detail}") from None


async def _fetch_file(file_id: str) -> bytes:
    info = await _api("getFile", data={"file_id": file_id})
    return await _download(info["file_path"])
```

In `_poll_once`, replace from `user_id, text = parsed` through the `await core.handle_message(...)` line:

```python
        user_id, text, wanted = parsed
        try:
            files: list[Inbound] = []
            for want in wanted:
                size = want.get("file_size")
                if size is not None and size > filetypes.MAX_BYTES:
                    await _send_text(user_id, "That's too big, 10 MB max.")
                    continue
                try:
                    data = await _fetch_file(want["file_id"])
                except (aiohttp.ClientError, TelegramError, KeyError) as e:
                    logging.warning(f"telegram: file download failed: {e}")
                    await _send_text(user_id, "Couldn't fetch that photo, try again.")
                    continue
                files.append(Inbound(filename=want["filename"], mime=want["mime"], data=data))
            if not text and not files:
                continue                         # every file refused, nothing to say
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
            await core.handle_message(_wren_user_id(user_id), text, TelegramChannel(user_id),
                                      files=files)
```

(Keep the existing `except Exception as e:` that follows.)

Replace `TelegramChannel.send_file`:

```python
    async def send_file(self, data: bytes, filename: str) -> None:
        form = aiohttp.FormData()
        form.add_field("chat_id", str(self._chat_id))
        if filetypes.sniff(data) in filetypes.IMAGE_MIMES:
            # a photo shows inline; a document is a download
            form.add_field("photo", data, filename=filename, content_type="application/octet-stream")
            await _api("sendPhoto", data=form)
            return
        form.add_field("document", data, filename=filename,
                       content_type="application/octet-stream")
        await _api("sendDocument", data=form)
```

- [ ] **Step 4: Run the Telegram tests**

Run: `source venv/bin/activate && pytest -q tests/test_telegram_plugin.py`
Expected: all pass. The earlier location test (`test_shared_location_becomes_a_set_location_message`) must still pass: it unpacks three values now — update it to `_user_id, text, _files = ...` if it unpacks `handle.await_args.args` into three (it does; `args` is still `(user_id, text, channel)`, so no change).

- [ ] **Step 5: Run the whole suite and commit**

Run: `source venv/bin/activate && pytest -q`
Expected: all pass.

```bash
git add wren/communication/telegram_plugin.py tests/test_telegram_plugin.py
git commit -m "feat(telegram): photos and PDFs come in with their caption; images go back as photos

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Multipart on the machine API and the web chat

**Files:**
- Modify: `wren/communication/http_plugin.py` (new `read_message`, `_payload` mime, `message()` uses it)
- Modify: `wren/communication/webchat.py` (`post_message` uses `read_message`; response files carry mime)
- Test: `tests/test_http_surface.py`, `tests/test_webchat.py` (append)

**Interfaces:**
- Consumes: `channel.Inbound`, `core.handle_message(files=)` (Task 2), `filetypes.sniff` (Task 1).
- Produces: `http_plugin.read_message(request) -> tuple[str, list[Inbound]]` (raises `web.HTTPBadRequest` on a non-object JSON body); response `files` entries are `{"filename", "mime", "data"}` on both `/message` and `/api/conversations/{id}/message`.

- [ ] **Step 1: Write the failing HTTP tests**

Append to `tests/test_http_surface.py`:

```python


# ── multipart ─────────────────────────────────────────────────────────────

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 32


def _multipart(text, *files):
    import aiohttp
    form = aiohttp.FormData()
    if text is not None:
        form.add_field("text", text)
    for name, data in files:
        form.add_field("file", data, filename=name, content_type="application/octet-stream")
    return form


def test_message_accepts_multipart_text_and_file(monkeypatch):
    seen = {}

    async def fake(user_id, text, channel, *, source="text", files=None):
        seen["text"], seen["files"] = text, files
        await channel.send("ok")
    monkeypatch.setattr(core, "handle_message", fake)
    status, body = call("post", "/message", token="good-token", data=_multipart("receipt", ("r.jpg", JPEG)))
    assert status == 200 and body["replies"] == ["ok"]
    assert seen["text"] == "receipt"
    assert [(f.filename, f.mime, f.data) for f in seen["files"]] == [("r.jpg", "application/octet-stream", JPEG)]


def test_message_accepts_a_file_with_no_text(monkeypatch):
    seen = {}

    async def fake(user_id, text, channel, *, source="text", files=None):
        seen["text"], seen["files"] = text, files
        await channel.send("What is this?")
    monkeypatch.setattr(core, "handle_message", fake)
    status, body = call("post", "/message", token="good-token", data=_multipart(None, ("r.jpg", JPEG)))
    assert status == 200 and seen["text"] == "" and len(seen["files"]) == 1


def test_multipart_with_neither_text_nor_file_is_400():
    status, body = call("post", "/message", token="good-token", data=_multipart(""))
    assert status == 400 and body["error"] == "missing 'text'"


def test_json_message_still_works_and_passes_no_files(monkeypatch):
    seen = {}

    async def fake(user_id, text, channel, *, source="text", files=None):
        seen["files"] = files
        await channel.send("ok")
    monkeypatch.setattr(core, "handle_message", fake)
    status, _ = call("post", "/message", token="good-token", json={"text": "hi"})
    assert status == 200 and seen["files"] == []


def test_reply_files_carry_a_sniffed_mime(monkeypatch):
    monkeypatch.setattr(core, "handle_message", _replies("here", files=[(JPEG, "a.jpg"), (b"plain", "a.txt")]))
    _, body = call("post", "/message", token="good-token", json={"text": "show"})
    assert [(f["filename"], f["mime"]) for f in body["files"]] == \
        [("a.jpg", "image/jpeg"), ("a.txt", "application/octet-stream")]


def test_dry_run_applies_to_multipart_too(monkeypatch):
    from wren import db
    seen = []

    async def fake(user_id, text, channel, *, source="text", files=None):
        seen.append(db.in_dry_run())
        await channel.send("ok")
    monkeypatch.setattr(core, "handle_message", fake)
    _, body = call("post", "/message?dry_run=1", token="good-token", data=_multipart("x", ("r.jpg", JPEG)))
    assert seen == [True] and body["dry_run"] is True
```

Every existing fake `handle_message` must accept the new keyword or the old tests break. There are nine in `tests/test_http_surface.py` shaped `async def fake(user_id, text, channel, *, source="text"):` and five in `tests/test_webchat.py` shaped `async def fake(user_id, text, channel):` (counted 2026-09-26). Rewrite them all in one go:

```bash
sed -i 's/async def fake(user_id, text, channel, \*, source="text"):/async def fake(user_id, text, channel, *, source="text", files=None):/' tests/test_http_surface.py
sed -i 's/async def fake(user_id, text, channel):/async def fake(user_id, text, channel, *, files=None):/' tests/test_webchat.py
grep -c "files=None" tests/test_http_surface.py tests/test_webchat.py   # expect 9 and 5 (plus the new tests' own)
```

Append to `tests/test_webchat.py`:

```python


# ── multipart ─────────────────────────────────────────────────────────────

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 32


def _multipart(text, *files):
    import aiohttp
    form = aiohttp.FormData()
    if text is not None:
        form.add_field("text", text)
    for name, data in files:
        form.add_field("file", data, filename=name, content_type="image/jpeg")
    return form


def _convo(token=TOKEN_A):
    _, body = call("post", "/api/conversations", token=token, json={})
    return body["id"]


def test_post_message_accepts_multipart_and_stores_the_caption(monkeypatch):
    seen = {}

    async def fake(user_id, text, channel, *, files=None):
        seen["text"], seen["files"] = text, files
        await channel.send("Saved, 1 image.")
    monkeypatch.setattr(core, "handle_message", fake)
    cid = _convo()
    status, body = call("post", f"/api/conversations/{cid}/message", token=TOKEN_A,
                        data=_multipart("receipt", ("r.jpg", JPEG)))
    assert status == 200 and body["replies"] == ["Saved, 1 image."]
    assert seen["text"] == "receipt" and [f.filename for f in seen["files"]] == ["r.jpg"]
    assert conversations.messages(cid)[0]["content"] == "receipt"


def test_post_message_with_a_file_and_no_caption_stores_a_placeholder(monkeypatch):
    async def fake(user_id, text, channel, *, files=None):
        await channel.send("What is this?")
    monkeypatch.setattr(core, "handle_message", fake)
    cid = _convo()
    status, _ = call("post", f"/api/conversations/{cid}/message", token=TOKEN_A,
                     data=_multipart(None, ("r.jpg", JPEG)))
    assert status == 200
    assert conversations.messages(cid)[0]["content"] == "(sent r.jpg)"


def test_post_message_reply_files_carry_mime(monkeypatch):
    monkeypatch.setattr(core, "handle_message", _replies("here", files=[(JPEG, "a.jpg")]))
    cid = _convo()
    _, body = call("post", f"/api/conversations/{cid}/message", token=TOKEN_A, json={"text": "show"})
    assert body["files"][0]["mime"] == "image/jpeg"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `source venv/bin/activate && pytest -q tests/test_http_surface.py tests/test_webchat.py -k "multipart or mime or placeholder or no_files"`
Expected: FAIL. Multipart posts get `400 body must be JSON`; mime tests get `KeyError: 'mime'`.

- [ ] **Step 3: Implement in `wren/communication/http_plugin.py`**

Add to the imports:

```python
from .. import filetypes
from ..channel import CollectingChannel, Inbound
```

(replacing the existing `from ..channel import CollectingChannel`).

Add above `_payload`:

```python
async def read_message(request: web.Request) -> tuple[str, list[Inbound]]:
    """(text, files) from either a JSON body {"text": ...} or a multipart form
    with a `text` field and any number of `file` parts. One parser for the
    machine API and the browser chat, so `curl -F` and a pasted screenshot
    take the same path. Raises HTTPBadRequest for a body that is neither."""
    if request.content_type == "multipart/form-data":
        form = await request.post()          # bounded by client_max_size
        text = form.get("text", "")
        files = [
            Inbound(filename=part.filename or "file",
                    mime=part.content_type or "application/octet-stream",
                    data=part.file.read())
            for part in form.getall("file", [])
            if isinstance(part, web.FileField)
        ]
        return (text if isinstance(text, str) else ""), files
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text='{"error": "body must be JSON"}',
                                 content_type="application/json")
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text='{"error": "body must be a JSON object"}',
                                 content_type="application/json")
    text = body.get("text", "")
    return (text if isinstance(text, str) else ""), []
```

Replace `_payload`:

```python
def _payload(channel: CollectingChannel, **extra) -> dict:
    return {
        "replies": channel.sent,
        "files": [
            {"filename": name,
             # sniffed, not guessed from the name: the page decides inline
             # image vs download link from this
             "mime": filetypes.sniff(data) or "application/octet-stream",
             "data": base64.b64encode(data).decode("ascii")}
            for data, name in channel.files
        ],
        **extra,
    }
```

Change `_dispatch` to accept and forward files:

```python
async def _dispatch(user_id: int, text: str, *, source: str = "text",
                    dry: bool = False, files: list[Inbound] | None = None, **extra) -> web.Response:
```

and both `core.handle_message(...)` calls inside it gain `files=files`.

Replace the body of `message()` after the auth check:

```python
    text, files = await read_message(request)
    if not text.strip() and not files:
        return web.json_response({"error": "missing 'text'"}, status=400)
    return await _dispatch(user_id, text, dry=_wants_dry_run(request), files=files)
```

- [ ] **Step 4: Implement in `wren/communication/webchat.py`**

Add the import near the other `from .. import` lines:

```python
from .. import filetypes
from .http_plugin import read_message
```

(`http_plugin` imports `webchat` only inside `build_app`, so there is no cycle at import time.)

In `post_message`, replace

```python
        body = await _json_object(request)
        text = body.get("text", "")
        if not isinstance(text, str) or not text.strip():
            return web.json_response({"error": "missing 'text'"}, status=400)
```

with

```python
        text, files = await read_message(request)
        if not text.strip() and not files:
            return web.json_response({"error": "missing 'text'"}, status=400)
        # The transcript stores words only (files are not persisted, see
        # send_file); a bare photo leaves a placeholder so the turn is visible.
        stored = text.strip() or "(sent " + ", ".join(f.filename for f in files) + ")"
```

then use `stored` where `text` was stored: `conversations.add_message(convo_id, "user", stored)` and `conversations.title_from(stored)`. Keep `core.handle_message(user_id, text, channel, files=files)` with the raw `text`.

In the response dict of `post_message`, add `"mime": filetypes.sniff(data) or "application/octet-stream",` to each file entry beside `"filename"`.

- [ ] **Step 5: Run both test files, then the suite**

Run: `source venv/bin/activate && pytest -q tests/test_http_surface.py tests/test_webchat.py && pytest -q`
Expected: all pass (the fakes were widened in Step 1).

- [ ] **Step 6: Commit**

```bash
git add wren/communication/http_plugin.py wren/communication/webchat.py tests/test_http_surface.py tests/test_webchat.py
git commit -m "feat(http): multipart text+file on /message and the web chat; reply files carry mime

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Web chat page — paste, drop, chips, inline images, card thumbnails

**Files:**
- Modify: `wren/communication/chat.html`
- Test: `tests/test_webchat.py` (append page-marker tests)

**Interfaces:**
- Consumes: multipart endpoint and `files[].mime` (Task 5); card rows' `files` metadata (Task 3).
- Produces: nothing for later tasks.

- [ ] **Step 1: Write the failing page tests**

Append to `tests/test_webchat.py`:

```python


def _page():
    from pathlib import Path
    return (Path(__file__).parent.parent / "wren" / "communication" / "chat.html").read_text()


def test_page_has_the_attachment_plumbing():
    page = _page()
    assert 'id="chips"' in page
    assert "pendingFiles" in page
    assert "new FormData()" in page
    assert 'addEventListener("paste"' in page or ".onpaste" in page
    assert '.ondrop' in page or 'addEventListener("drop"' in page


def test_api_helper_does_not_force_json_content_type_on_form_bodies():
    page = _page()
    assert "instanceof FormData" in page


def test_reply_images_render_inline_and_pdfs_as_links():
    page = _page()
    assert 'f.mime.startsWith("image/")' in page
    assert '<img class="rimg"' in page


def test_notes_card_rows_show_a_thumbnail_for_attached_images():
    page = _page()
    assert "renderCard(c, b.querySelector(\".body\"), data.files)" in page
    assert 'class="cthumb"' in page
```

- [ ] **Step 2: Run them to verify they fail**

Run: `source venv/bin/activate && pytest -q tests/test_webchat.py -k "plumbing or force_json or inline or thumbnail"`
Expected: 4 FAIL with AssertionError.

- [ ] **Step 3: CSS**

In `chat.html`, after the `#composer:focus-within .cmark { opacity: .9; }` rule add:

```css
/* pending attachments, above the input */
#chips { max-width: 760px; margin: 0 auto 6px; display: flex; flex-wrap: wrap; gap: 6px; }
#chips[hidden] { display: none; }
.chip { position: relative; width: 56px; height: 56px; border-radius: 8px; overflow: hidden;
        border: 1px solid var(--line); background: var(--panel); display: flex;
        align-items: center; justify-content: center; font-size: 11px; color: var(--muted); }
.chip img { width: 100%; height: 100%; object-fit: cover; }
.chip button { position: absolute; top: 2px; right: 2px; width: 18px; height: 18px; border: 0;
               border-radius: 50%; background: rgba(0,0,0,.55); color: #fff; font-size: 11px;
               line-height: 18px; padding: 0; cursor: pointer; }
#composer.dragover { border-color: var(--accent); }
/* files in bubbles */
.rimg { max-width: min(100%, 420px); max-height: 360px; border-radius: 10px; display: block; }
.msg.user .rimg { max-width: 240px; margin-bottom: 6px; }
.cthumb { width: 32px; height: 32px; object-fit: cover; border-radius: 4px; flex: none; }
```

- [ ] **Step 4: Markup**

Inside `<footer>` in `<main>`, directly above `<div id="composer">`, add:

```html
      <div id="chips" hidden></div>
```

- [ ] **Step 5: `api()` must not force JSON on form bodies**

Replace the `headers:` line inside `api()`:

```js
    headers: { "Authorization": "Bearer " + token,
               // a FormData body sets its own multipart boundary; forcing
               // application/json here would make the server reject it
               ...(opts.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
               ...(opts.headers || {}) },
```

- [ ] **Step 6: Rendering helpers**

Replace `fileLink`:

```js
function fileLink(f) {
  if (f.mime && f.mime.startsWith("image/")) {
    return '<img class="rimg" alt="' + esc(f.filename) + '" src="data:' + f.mime + ';base64,' + f.data + '">';
  }
  return '<a class="dl" download="' + esc(f.filename) +
         '" href="data:application/octet-stream;base64,' + f.data + '">⤓ ' +
         esc(f.filename) + "</a>";
}
```

Change `renderCard` to take the reply's files so the notes card can draw thumbnails:

```js
function renderCard(card, mount, files) {
  const draw = CARDS[card.kind];
  if (!draw) return false;      // unknown kind: leave the prose bubble alone
  draw(card.data || {}, mount, files || []);
  return true;
}
```

In `mountStoredCard`, change `renderCard(r.cards[0], mount)` to `renderCard(r.cards[0], mount, r.files)`. In `cardShell.refresh`, change `renderCard(fresh.cards[0], mount)` to `renderCard(fresh.cards[0], mount, fresh.files)`. In `send()`, change `renderCard(c, b.querySelector(".body"))` to `renderCard(c, b.querySelector(".body"), data.files)`.

In `notesCard(data, mount)` change the signature to `notesCard(data, mount, files)` and the row template to:

```js
  // Thumbnails come from the reply's own files, matched by name: the card
  // holds metadata only, the bytes ride beside it exactly once.
  const byName = {};
  for (const f of files || []) byName[f.filename] = f;
  const rows = notes.map(n => {
    const first = (n.files || []).find(a => a.mime.startsWith("image/") && byName[a.filename]);
    const thumb = first
      ? '<img class="cthumb" alt="" src="data:' + first.mime + ';base64,' + byName[first.filename].data + '">'
      : "";
    const mark = (n.files || []).length ? ' <span class="cwhen">📎' + (n.files || []).length + "</span>" : "";
    return '<li data-tags="' + esc((n.tags || []).join(",")) + '">' + thumb +
      '<span class="citem">' + esc(n.content) + mark + "</span>" +
      '<span class="cwhen">' + esc((n.created_at || "").slice(0, 10)) + "</span>" +
    "</li>";
  }).join("");
```

- [ ] **Step 7: Pending files, paste, drop, and multipart send**

Directly above `async function send() {` add:

```js
/* ── attachments ───────────────────────────────────────────────────────── */
// Files waiting to go with the next message. Images and PDFs only; the
// server sniffs bytes and refuses the rest, this filter just keeps the chip
// row honest.
let pendingFiles = [];
const ACCEPT = ["image/jpeg", "image/png", "image/webp", "image/gif", "application/pdf"];

function addFiles(list) {
  for (const f of list) {
    if (!ACCEPT.includes(f.type)) continue;
    pendingFiles.push(f);
  }
  drawChips();
}

function drawChips() {
  const box = $("#chips");
  box.innerHTML = "";
  box.hidden = pendingFiles.length === 0;
  pendingFiles.forEach((f, i) => {
    const chip = document.createElement("div");
    chip.className = "chip";
    if (f.type.startsWith("image/")) {
      const img = document.createElement("img");
      img.src = URL.createObjectURL(f);
      img.onload = () => URL.revokeObjectURL(img.src);
      chip.append(img);
    } else {
      chip.textContent = "PDF";
    }
    const x = document.createElement("button");
    x.type = "button"; x.textContent = "✕"; x.title = "remove";
    x.onclick = () => { pendingFiles.splice(i, 1); drawChips(); };
    chip.append(x);
    box.append(chip);
  });
}

$("#text").addEventListener("paste", e => {
  const items = [...(e.clipboardData || {}).items || []].filter(i => i.kind === "file");
  if (!items.length) return;
  e.preventDefault();
  addFiles(items.map(i => i.getAsFile()).filter(Boolean));
});
$("#composer").ondragover = e => { e.preventDefault(); $("#composer").classList.add("dragover"); };
$("#composer").ondragleave = () => $("#composer").classList.remove("dragover");
$("#composer").ondrop = e => {
  e.preventDefault(); $("#composer").classList.remove("dragover");
  addFiles(e.dataTransfer.files);
};
```

In `send()`:

- change the guard `if (!text || busy) return;` to `if ((!text && !pendingFiles.length) || busy) return;`
- capture the files before clearing: after `$("#text").value = ""; ...` add `const files = pendingFiles; pendingFiles = []; drawChips();`
- change `bubble("user", esc(text));` to:

```js
    const thumbs = files.filter(f => f.type.startsWith("image/"))
      .map(f => '<img class="rimg" alt="" src="' + URL.createObjectURL(f) + '">').join("");
    const names = files.filter(f => !f.type.startsWith("image/")).map(f => "📎 " + esc(f.name)).join(" ");
    bubble("user", thumbs + (names ? "<div>" + names + "</div>" : "") + esc(text));
```

- change the POST to send multipart when there are files:

```js
    let body, opts;
    if (files.length) {
      body = new FormData();
      body.append("text", text);
      for (const f of files) body.append("file", f, f.name);
      opts = { method: "POST", body };
    } else {
      opts = { method: "POST", body: JSON.stringify({ text }) };
    }
    const data = await api("/api/conversations/" + active + "/message", opts);
```

- in the `catch`, put the files back so a failed send does not lose them: `pendingFiles = files; drawChips();` before the error bubble.

- [ ] **Step 8: Run the page tests, then the suite**

Run: `source venv/bin/activate && pytest -q tests/test_webchat.py && pytest -q`
Expected: all pass.

- [ ] **Step 9: Headless pass**

Using the Playwright scratch venv from 2026-09-26 at `/tmp/claude-1000/-home-winter-work-Wren/eb3d9ea5-0f76-40b6-875c-4b0b560fc91b/scratchpad/pw` (recreate with `python3 -m venv pw && pw/bin/pip install playwright && pw/bin/playwright install chromium` if it is gone), and with `WREN_DB` pointed at a scratch copy — never the live file — start `python3 -m wren.run` on a spare port or use `?dry_run=1`-free web chat only against the scratch DB. Then:

```python
# set_input_files on a hidden <input type=file> is not available (there is none);
# drive the drop handler instead:
page.evaluate("""() => {
  const dt = new DataTransfer();
  const bytes = Uint8Array.from([0xff,0xd8,0xff,0xe0, ...new Array(32).fill(0)]);
  dt.items.add(new File([bytes], 'r.jpg', {type: 'image/jpeg'}));
  document.querySelector('#composer').dispatchEvent(new DragEvent('drop', {dataTransfer: dt, bubbles: true}));
}""")
assert page.locator("#chips .chip").count() == 1
page.fill("#text", "tyre receipt")
page.click("#send")
page.wait_for_timeout(1500)
```

Expected: the reply bubble says `Saved, 1 image.` and the user bubble shows the thumbnail. Then send `show my notes` and confirm the card row has a `.cthumb` and an inline `.rimg` appears under it. Screenshot to the scratchpad and look at it.

- [ ] **Step 10: Commit**

```bash
git add wren/communication/chat.html tests/test_webchat.py
git commit -m "feat(webchat): paste or drop images and PDFs; inline replies; card thumbnails

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Machine-API and dry-run end-to-end check

**Files:**
- Test: `tests/test_http_surface.py` (append one end-to-end test)

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the failing end-to-end test**

Append to `tests/test_http_surface.py`:

```python


def test_multipart_photo_end_to_end_saves_a_note_with_the_image(monkeypatch):
    # Real core, real notes skill, real store; only the classifier is faked.
    from wren import brain, contacts, settings
    from wren.skills import notes_store
    contacts.init_db(); settings.init_db(); notes_store.init_db()
    monkeypatch.setattr(brain, "detect_intent", lambda *a, **k: {
        "intent": "chat", "content": "", "tags": ["receipt"], "person": None})
    status, body = call("post", "/message", token="good-token", data=_multipart("tyre place", ("r.jpg", JPEG)))
    assert status == 200 and body["replies"] == ["Saved, 1 image."]
    note = notes_store.list_recent(1)[0]
    assert note["content"] == "tyre place" and note["tags"] == "receipt"
    assert [a["mime"] for a in notes_store.attachments(note["id"])] == ["image/jpeg"]


def test_multipart_photo_in_a_dry_run_leaves_no_note(monkeypatch):
    from wren import brain, contacts, settings
    from wren.skills import notes_store
    contacts.init_db(); settings.init_db(); notes_store.init_db()
    monkeypatch.setattr(brain, "detect_intent", lambda *a, **k: {
        "intent": "chat", "content": "", "tags": [], "person": None})
    status, body = call("post", "/message?dry_run=1", token="good-token", data=_multipart("tyre place", ("r.jpg", JPEG)))
    assert status == 200 and body["replies"] == ["Saved, 1 image."] and body["dry_run"] is True
    assert notes_store.list_recent(1) == []
```

- [ ] **Step 2: Run them**

Run: `source venv/bin/activate && pytest -q tests/test_http_surface.py -k "end_to_end or dry_run_leaves"`
Expected: PASS if Tasks 1–5 are correct. If the first fails with `['Saved. 🐣']`, core is not forcing `save_note` for multipart files — check Task 5's `_dispatch` forwards `files=files` on both `handle_message` calls.

- [ ] **Step 3: Commit**

```bash
git add tests/test_http_surface.py
git commit -m "test(http): a multipart photo becomes a note end to end, and not in a dry run

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Docs, restart, live check

**Files:**
- Modify: `README.md` (Notes section and the HTTP table), `PROJECT_PLAN.md` (backlog entry), `CLAUDE.md` (test count)

- [ ] **Step 1: README**

In the HTTP routes table, change the `/message` row to:

```markdown
| `POST /message` | `{"text": "..."}` or multipart `text` + `file` parts | `{"replies": [...], "files": [{filename, mime, data}]}` |
```

and after the curl example add:

```markdown
A photo or PDF goes in as multipart and becomes a note with the caption as
its text:

```bash
curl -s localhost:8787/message -H "Authorization: Bearer $WREN_TOKEN" \
  -F text="tyre place receipt" -F file=@receipt.jpg
```
```

Find the Notes section (`grep -n "^## Notes" README.md`) and add:

```markdown
**Photos and PDFs.** Send a photo on Telegram, or paste or drop one into the
web chat, with a caption: it becomes a note with the caption as its text and
the file attached. Without a caption Wren asks "What is this?" and your next
message is the caption. "Show my notes" marks attached notes and sends the
files back, five per reply at most; the web chat shows images inline.
Images (JPEG, PNG, WebP, GIF) and PDFs, 10 MB each. Wren keeps the file; it
does not look at it.
```

- [ ] **Step 2: PROJECT_PLAN and CLAUDE.md**

In `PROJECT_PLAN.md`, replace the **Inbound images and files.** paragraph's first sentence with `**Inbound images and files.** Landed 2026-09-27 (spec: docs/superpowers/specs/2026-09-26-inbound-images-design.md; Telegram and web chat, images and PDFs, no vision). Discord and vision remain open.` and keep the rest as history. In `CLAUDE.md`, update the `pytest -q` comment to the new passing count and date.

- [ ] **Step 3: Full suite, restart, live check**

```bash
source venv/bin/activate && pytest -q
sudo -n systemctl restart wren && sleep 3 && systemctl is-active wren
journalctl -u wren --no-pager -n 8
```

Expected: green; `active`; the three plugins start with no traceback. Then one real dry run through the machine API with a real JPEG:

```bash
TOKEN=$(grep '^WREN_TOKENS=' .env | cut -d= -f2 | cut -d: -f1)
curl -s "localhost:8787/message?dry_run=1" -H "Authorization: Bearer $TOKEN" \
  -F text="dry run receipt" -F file=@/path/to/any.jpg
```

Expected: `{"replies": ["Saved, 1 image."], "files": [], "dry_run": true}` and no new note in the real database (`curl` `/message` with `{"text":"show my notes"}` and confirm the dry-run note is absent).

- [ ] **Step 4: Commit and push**

```bash
git add README.md PROJECT_PLAN.md CLAUDE.md
git commit -m "docs: inbound images and files

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git push origin main
```

The repo is public: before pushing, `git diff --name-only origin/main..main | grep -Ei "\.env$|\.db$"` must print nothing.
