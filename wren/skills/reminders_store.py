import sqlite3
from datetime import datetime, timezone

from .. import db

def init_db() -> None:
    with db.conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id   TEXT NOT NULL,
                content    TEXT NOT NULL,
                fire_at    TEXT NOT NULL,
                status     TEXT NOT NULL,
                created_at TEXT NOT NULL,
                via        TEXT
            )
        """)
        # Same additive migration conversations.py uses for messages.card, and
        # for the same reason: rows written before this column existed must
        # read back as via=None, i.e. "deliver wherever NOTIFY_VIA says".
        cols = {row[1] for row in con.execute("PRAGMA table_info(reminders)")}
        if "via" not in cols:
            try:
                con.execute("ALTER TABLE reminders ADD COLUMN via TEXT")
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e):
                    raise

def save(owner_id: int, content: str, fire_at: str, via: str | None = None) -> int:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO reminders (owner_id, content, fire_at, status, created_at, via)"
            " VALUES (?,?,?,?,?,?)",
            (str(owner_id), content, fire_at, "pending", ts, via),
        )
        return cur.lastrowid

def pending(owner_id: int) -> list[dict]:
    with db.conn() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM reminders WHERE owner_id=? AND status='pending' ORDER BY fire_at ASC",
            (str(owner_id),),
        ).fetchall()
    return [dict(r) for r in rows]

def find_pending(owner_id: int, substring: str) -> list[dict]:
    needle = substring.lower()
    hits = [r for r in pending(owner_id) if needle in r["content"].lower()]
    # exact wins, same reason as notes_store.find
    exact = [r for r in hits if r["content"].strip().lower() == substring.strip().lower()]
    return exact if len(exact) == 1 else hits

def cancel(reminder_id: int) -> bool:
    with db.conn() as con:
        cur = con.execute(
            "UPDATE reminders SET status='cancelled' WHERE id=? AND status='pending'",
            (reminder_id,),
        )
        return cur.rowcount > 0

def cancel_all(owner_id: int) -> int:
    with db.conn() as con:
        cur = con.execute(
            "UPDATE reminders SET status='cancelled' WHERE owner_id=? AND status='pending'",
            (str(owner_id),),
        )
        return cur.rowcount

def due(now_iso: str) -> list[dict]:
    with db.conn() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM reminders WHERE status='pending' AND fire_at<=? ORDER BY fire_at ASC",
            (now_iso,),
        ).fetchall()
    return [dict(r) for r in rows]

def mark_fired(reminder_id: int) -> None:
    with db.conn() as con:
        con.execute("UPDATE reminders SET status='fired' WHERE id=?", (reminder_id,))
