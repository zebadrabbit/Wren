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
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_attachments_note ON attachments(note_id)"
        )

def save(owner_id: int, content: str, tags: list[str]) -> int:
    ts = datetime.now(timezone.utc).isoformat()
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO notes (owner_id, content, tags, created_at) VALUES (?,?,?,?)",
            (str(owner_id), content, ",".join(tags), ts),
        )
        return cur.lastrowid

def search(owner_id: int, tags: list[str] | None = None) -> list[dict]:
    with db.conn() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM notes WHERE owner_id=? ORDER BY created_at DESC",
            (str(owner_id),),
        ).fetchall()
    results = [dict(r) for r in rows]
    if tags:
        wanted = set(tags)
        results = [
            r for r in results
            if wanted & set(filter(None, r["tags"].split(",")))
        ]
    return results

def list_recent(owner_id: int, n: int = 10) -> list[dict]:
    with db.conn() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM notes WHERE owner_id=? ORDER BY created_at DESC LIMIT ?",
            (str(owner_id), n),
        ).fetchall()
    return [dict(r) for r in rows]

def delete(note_id: int) -> bool:
    with db.conn() as con:
        # Explicit, not via the REFERENCES cascade: SQLite only honours ON
        # DELETE CASCADE when PRAGMA foreign_keys=ON is set per connection,
        # and db.conn() does not set it. Same transaction either way.
        con.execute("DELETE FROM attachments WHERE note_id=?", (note_id,))
        cur = con.execute("DELETE FROM notes WHERE id=?", (note_id,))
        return cur.rowcount > 0

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
