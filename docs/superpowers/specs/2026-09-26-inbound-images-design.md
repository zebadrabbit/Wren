# Inbound Images and Files — Design

A photo or a PDF can enter Wren through Telegram or the web chat, becomes a
note with the caption as its text, and comes back out through recall. No
vision model is involved: Wren keeps the file, it does not look at it.

## Why

`PROJECT_PLAN.md` names this the clearest gap between how notes are used and
what Wren accepts: a receipt, a screenshot or a whiteboard photo cannot enter
through any door. `telegram_plugin._incoming` skips every message without a
`text` field, which is exactly how photos arrive; the web chat has no upload,
paste or drop handling; the machine API is JSON-only. Decided 2026-09-21 as
the next feature after the memory/briefing roadmap landed. Decisions taken in
the 2026-09-26 brainstorm:

- **Keep, don't understand.** No vision in this slice. The configured models
  are local text models; a vision slice can be added later on top of the
  stored bytes.
- **Caption is the note.** A captioned image is a note; an uncaptioned one
  gets one question, "What is this?", and the reply becomes the caption.
- **Telegram and web chat** in this slice. Discord later.
- **Images plus PDFs.** JPEG, PNG, WebP, GIF, PDF.

## Verified constraints (checked 2026-09-26)

1. Every door reaches `core.handle_message(user_id, text, channel, *,
   source)` (`discord_plugin.py:82`, `telegram_plugin.py:260`,
   `http_plugin.py:63/65`, `webchat.py:269`). One entry point, so files ride
   one new keyword argument.
2. `Channel.send_file(data, filename)` already exists on every surface
   (`channel.py`); Telegram posts it as `sendDocument`
   (`telegram_plugin.py:160`), the web chat returns it base64 inline and does
   not persist it (`webchat.py` `send_file`, "returned inline, not stored").
3. `telegram_plugin._api(method, *, data)` is the single Bot API caller with
   the token-scrubbing and timeout behaviour; `_incoming(update)` is the one
   place non-text messages are skipped (`telegram_plugin.py:200`).
4. `webchat._json_object` parses the message body; `http_plugin.build_app`
   sets `client_max_size=MAX_BODY_BYTES` (25 MB) for the whole app.
5. `notes_store.save/search/find/delete` are the whole notes API; `delete`
   is a bare `DELETE FROM notes`. Notes have `owner_id, content, tags,
   created_at`.
6. `core._pending` is the yes/no confirmation park, keyed by user id with a
   monotonic TTL, consumed by `_take_pending` before the classifier runs.
7. `db.conn()` is the only connection path, and `db.dry_run()` snapshots the
   whole file — so blobs stored in SQLite are covered by per-test isolation
   and by dry runs with no extra work, and files on disk would not be.

## Storage

New table in the same database, created by `notes_store.init_db`:

```sql
CREATE TABLE IF NOT EXISTS attachments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id    INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    filename   TEXT NOT NULL,
    mime       TEXT NOT NULL,
    size       INTEGER NOT NULL,
    data       BLOB NOT NULL,
    created_at TEXT NOT NULL
)
```

`ON DELETE CASCADE` needs `PRAGMA foreign_keys=ON` per connection, which
SQLite does not default to; `notes_store.delete` deletes attachments
explicitly in the same transaction instead of relying on the pragma, so the
cascade holds even from a connection that forgot it.

Bytes live in SQLite deliberately: one file to back up, per-test isolation
and `db.dry_run()` cover them for free, and the volume is a household's
receipts and screenshots. Ceiling, to be recorded as a `ponytail:` comment
in the store: move blobs to disk if the database passes a few hundred MB,
because the dry-run backup copies the whole file each time.

Accepted types are decided by sniffing the first bytes, never by filename or
by the surface's declared mime:

| sniff | mime |
|---|---|
| `FF D8 FF` | image/jpeg |
| `89 50 4E 47 0D 0A 1A 0A` | image/png |
| `52 49 46 46 .. .. .. .. 57 45 42 50` | image/webp |
| `47 49 46 38` | image/gif |
| `25 50 44 46` | application/pdf |

Limits, three module constants in `notes_store`, no env knobs:

| constant | value |
|---|---|
| `MAX_ATTACHMENT_BYTES` | 10 MB |
| `MAX_FILES_PER_REPLY` | 5 |
| (core) `_FILES_TTL` | 300 s |

`notes_store` API additions:

- `attach(note_id, filename, mime, data) -> int` — raises `ValueError`
  ("too big" / "unsupported type") after sniffing; the sniffed mime is what
  gets stored, not the caller's.
- `attachments(note_id) -> list[dict]` — id, filename, mime, size only.
- `attachment(attachment_id) -> dict | None` — the row with `data`.
- `delete(note_id)` also removes that note's attachments.

## Core flow

`channel.py` gains:

```python
@dataclass
class Inbound:
    filename: str
    mime: str        # as declared by the surface; the store re-sniffs
    data: bytes
```

`Ctx` gains `files: list[Inbound] = field(default_factory=list)`.
`core.handle_message` gains `files: list[Inbound] | None = None`.

Order inside `handle_message`, after the authz gate (a stranger's files are
dropped with their text, silently, as text is today) and after the yes/no
pending check (a pending confirmation is still answered first):

1. **Files and a caption.** Run the classifier on the caption as usual, but
   only to harvest `tags`; force `intent = "save_note"` and `content =
   caption`. Without vision there is nothing else Wren can do with a
   picture, and "receipt from the tyre place" must not gamble on a small
   model's guess. Dispatch to the notes skill with `ctx.files` set.
2. **Files and no caption.** Park them: `_pending_files[user_id] = (files,
   deadline)`, reply "What is this?", return. A second uncaptioned photo
   before the reply replaces the parked set — Wren asks again.
3. **Text with parked files.** If `_pending_files` holds an unexpired entry
   for this user, pop it and treat the message as case 1 with that text as
   the caption, whatever the text says. A wrong answer is one note to
   delete; guessing whether "what's the weather" was a caption is worse.
   If the entry has expired, pop it, say "That photo timed out, send it
   again." and then handle the text normally, so nothing vanishes silently.

`_pending_files` is a second dict beside `_pending`, same shape (user id →
payload, monotonic deadline), same reasoning about single-shot surfaces.
Voice confirmation does not apply: nothing here is destructive.

Skills other than notes never read `ctx.files` in this slice.

## Notes skill

- `save_note` with `ctx.files`: `notes.save(...)` then `notes.attach(...)`
  per file. Reply "Saved, 1 image." / "Saved, 2 files." A file the store
  rejects (size or type) is reported in the reply and the note is still
  saved with the rest; if every file is rejected and the caption is empty,
  nothing is saved and the reply says why.
- `recall_notes` (both the prose and the card path): for the notes about to
  be shown, look up `notes.attachments(note_id)`; append " (1 image)" /
  " (2 files)" to that note's line so a reader on a surface that cannot show
  files, or in an export, knows something is attached; then
  `ctx.channel.send_file(data, filename)` for each, stopping at
  `MAX_FILES_PER_REPLY` across the reply.
- `discard_idea` and any other note deletion: unchanged in behaviour; the
  cascade lives in the store.
- `export_notes` (`_build_export`): each note line carries the same
  " (1 image)" marker; bytes are not exported in this slice.

## Surfaces

### Telegram

`_incoming(update)` returns `(user_id, text, files)`; `files` is an empty
list for a plain text message. Two new shapes are accepted, everything else
skips as before (stickers, voice notes, video), and the docstring's list is
updated:

- `message.photo`: a list of sizes; take the last (largest). `caption` is
  the text.
- `message.document` whose `mime_type` is one of the accepted types (the
  store re-sniffs; the check here only avoids downloading a video). `caption`
  is the text; `file_name` is the filename.

Download is `getFile(file_id)` → `file_path`, then a GET of
`{API_ROOT}/file/bot{token}/{file_path}`, through the same client and
timeout `_api` uses, with the same token scrubbing on error. If
`file_size` is present and over `MAX_ATTACHMENT_BYTES`, reply "That's too
big, 10 MB max." and skip the download. A failed download replies "Couldn't
fetch that photo, try again." and is logged scrubbed.

The plugin decides nothing about what a photo means; it hands bytes and
caption to core. Same rule that kept location sharing out of domain logic.

### Web chat

`POST /api/conversations/{id}/message` accepts `multipart/form-data` as well
as JSON: a `text` field (may be empty when files are present) plus zero or
more `file` parts. Parsing lives in one helper, `_read_message(request) ->
(text, files)`, used by this endpoint and by `http_plugin` `/message`, so
`curl -F text=... -F file=@receipt.jpg` works on the machine API and
`?dry_run=1` still applies. JSON bodies are unchanged. The existing
`client_max_size` (25 MB) bounds the whole request; the store's 10 MB per
file is the real cap.

`chat.html`:
- paste (`paste` event with image items) and drag-and-drop on the composer
  add files to a pending list; a thumbnail chip per file with a remove
  button sits above the input; Send posts multipart when the list is
  non-empty, JSON otherwise.
- the user's bubble for the live turn shows the thumbnails beside the
  caption.
- reply files (`data.files`, already base64) render inline as `<img>` for
  image mimes and as the existing download link for PDFs.
- the notes card shows a small thumbnail per row for the live render.
- the stored transcript does not re-show images on reopen (conversation
  messages do not persist files today). Known limit; recall always works.

### Outbound (`send_file`)

- Telegram: `sendPhoto` for image mimes so it shows as a photo;
  `sendDocument` (as now) for everything else. Telegram's photo limit is
  10 MB, which equals `MAX_ATTACHMENT_BYTES`.
- Web chat / machine API: unchanged, base64 inline.

## Errors

One plain reply each, never a silent drop:

| case | reply |
|---|---|
| over 10 MB | That's too big, 10 MB max. |
| unsupported type (after sniff) | I can keep images and PDFs, not that. |
| Telegram download failed | Couldn't fetch that photo, try again. |
| parked photo expired | That photo timed out, send it again. |
| store failure | core's existing "Something went wrong" + error ack |

## Testing

House shape: `CollectingChannel` + `Ctx`, no transport mocks in skill tests.

- `notes_store`: attach/list/fetch; delete cascades; oversize rejected;
  sniff rejects a renamed executable; sniffed mime wins over declared.
- `core`: files+caption forces `save_note` and keeps classifier tags;
  uncaptioned parks and asks; next text attaches; expiry replies and then
  handles the text normally; stranger's files dropped; pending yes/no still
  answered first.
- `telegram_plugin._incoming` / `_poll_once`: photo (largest size), document
  of accepted mime, oversize refused before download, sticker still skipped,
  download failure reply; `_api` faked as existing tests do.
- `webchat` / `http_plugin`: multipart with text+file, multipart with file
  only, JSON unchanged, `/message -F` on the machine API, dry_run with files.
- `notes_skill`: "Saved, 1 image."; recall sends files with the cap; marker
  text present; partial rejection reply.
- One live headless pass (Playwright, scratch venv) on paste and drop in the
  web chat, as done for the layout fix.

## Out of scope

Vision/description, Discord, persisting files in the conversation
transcript, exporting bytes, editing or removing a single attachment from a
note, images in the daily briefing.
