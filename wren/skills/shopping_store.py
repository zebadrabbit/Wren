import sqlite3
from datetime import datetime, timezone

from .. import db

def init_db() -> None:
    with db.conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS shopping_items (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                item          TEXT NOT NULL,
                original_text TEXT NOT NULL,
                added_by      TEXT NOT NULL,
                status        TEXT NOT NULL,
                added_at      TEXT NOT NULL
            )
        """)

def add(item_text: str, added_by: str) -> tuple[int, bool]:
    normalized = item_text.strip().lower()
    with db.conn() as con:
        con.row_factory = sqlite3.Row
        existing = con.execute(
            "SELECT id FROM shopping_items WHERE item=? AND status='active'",
            (normalized,),
        ).fetchone()
        if existing:
            return existing["id"], False
        ts = datetime.now(timezone.utc).isoformat()
        cur = con.execute(
            "INSERT INTO shopping_items (item, original_text, added_by, status, added_at) VALUES (?,?,?,?,?)",
            (normalized, item_text.strip(), added_by, "active", ts),
        )
        return cur.lastrowid, True

def remove(item_text: str) -> bool:
    normalized = item_text.strip().lower()
    with db.conn() as con:
        cur = con.execute(
            "UPDATE shopping_items SET status='removed' WHERE item=? AND status='active'",
            (normalized,),
        )
        return cur.rowcount > 0

def restore(item_text: str) -> bool:
    """Undo a remove: flip the most recently removed row back to active.

    Deliberately not add() again. add() only matches rows with status='active',
    so on a removed item it INSERTs a second row -- which inflates
    common_items(), making an item you removed and put back look more
    frequently bought than it is, and loses the original added_by/added_at.
    Flipping the status back is the actual inverse of remove().
    """
    normalized = item_text.strip().lower()
    with db.conn() as con:
        cur = con.execute(
            "UPDATE shopping_items SET status='active' WHERE id = ("
            "  SELECT id FROM shopping_items WHERE item=? AND status='removed'"
            "  ORDER BY id DESC LIMIT 1)",
            (normalized,),
        )
        return cur.rowcount > 0

def clear() -> int:
    """Mark every active item removed; returns how many there were.

    A status flip, not a DELETE, for the same reason remove() is: the rows stay
    as history, so common_items() still knows what gets bought regularly (and a
    mistaken clear is recoverable with one UPDATE).
    """
    with db.conn() as con:
        cur = con.execute("UPDATE shopping_items SET status='removed' WHERE status='active'")
        return cur.rowcount

def active_items() -> list[dict]:
    with db.conn() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM shopping_items WHERE status='active' ORDER BY added_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]

def common_items(threshold: int = 3, limit: int = 5) -> list[dict]:
    with db.conn() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT item, COUNT(*) as count
            FROM shopping_items
            GROUP BY item
            HAVING COUNT(*) >= ?
            ORDER BY count DESC
            LIMIT ?
            """,
            (threshold, limit),
        ).fetchall()
    return [dict(r) for r in rows]
