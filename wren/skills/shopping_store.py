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
                added_at      TEXT NOT NULL,
                list          TEXT NOT NULL DEFAULT 'shopping'
            )
        """)
        # Named lists (2026-09-26): rows written before the column existed
        # are the shopping list, which the DEFAULT says for them. Same
        # additive migration as reminders.via.
        cols = {row[1] for row in con.execute("PRAGMA table_info(shopping_items)")}
        if "list" not in cols:
            try:
                con.execute("ALTER TABLE shopping_items ADD COLUMN list TEXT NOT NULL DEFAULT 'shopping'")
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e):
                    raise

def add(item_text: str, added_by: str, list_name: str = "shopping") -> tuple[int, bool]:
    normalized = item_text.strip().lower()
    with db.conn() as con:
        con.row_factory = sqlite3.Row
        existing = con.execute(
            "SELECT id FROM shopping_items WHERE item=? AND status='active' AND list=?",
            (normalized, list_name),
        ).fetchone()
        if existing:
            return existing["id"], False
        ts = datetime.now(timezone.utc).isoformat()
        cur = con.execute(
            "INSERT INTO shopping_items (item, original_text, added_by, status, added_at, list)"
            " VALUES (?,?,?,?,?,?)",
            (normalized, item_text.strip(), added_by, "active", ts, list_name),
        )
        return cur.lastrowid, True

def remove(item_text: str, list_name: str = "shopping") -> bool:
    normalized = item_text.strip().lower()
    with db.conn() as con:
        cur = con.execute(
            "UPDATE shopping_items SET status='removed' WHERE item=? AND status='active' AND list=?",
            (normalized, list_name),
        )
        return cur.rowcount > 0

def restore(item_text: str, list_name: str = "shopping") -> bool:
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
            "  SELECT id FROM shopping_items WHERE item=? AND status='removed' AND list=?"
            "  ORDER BY id DESC LIMIT 1)",
            (normalized, list_name),
        )
        return cur.rowcount > 0

def clear(list_name: str = "shopping") -> int:
    """Mark every active item removed; returns how many there were.

    A status flip, not a DELETE, for the same reason remove() is: the rows stay
    as history, so common_items() still knows what gets bought regularly (and a
    mistaken clear is recoverable with one UPDATE).
    """
    with db.conn() as con:
        ids = [r[0] for r in con.execute(
            "SELECT id FROM shopping_items WHERE status='active' AND list=?", (list_name,))]
        con.execute("UPDATE shopping_items SET status='removed' WHERE status='active' AND list=?",
                    (list_name,))
    # ponytail: process-local, per list -- "undo" after a clear has a ten
    # minute window in core anyway, and a restart is longer than that
    _last_cleared[list_name] = ids
    return len(ids)


_last_cleared: dict[str, list[int]] = {}


def restore_cleared(list_name: str = "shopping") -> int:
    """Undo the most recent clear of this list: exactly the rows it removed,
    not everything ever removed. Returns how many came back."""
    ids = _last_cleared.pop(list_name, [])
    if not ids:
        return 0
    with db.conn() as con:
        cur = con.execute(
            f"UPDATE shopping_items SET status='active' WHERE status='removed' AND id IN ({','.join('?' * len(ids))})",
            ids)
        return cur.rowcount

def active_items(list_name: str = "shopping") -> list[dict]:
    with db.conn() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM shopping_items WHERE status='active' AND list=? ORDER BY added_at ASC",
            (list_name,),
        ).fetchall()
    return [dict(r) for r in rows]

def common_items(threshold: int = 3, limit: int = 5, list_name: str = "shopping") -> list[dict]:
    with db.conn() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT item, COUNT(*) as count
            FROM shopping_items
            WHERE list=?
            GROUP BY item
            HAVING COUNT(*) >= ?
            ORDER BY count DESC
            LIMIT ?
            """,
            (list_name, threshold, limit),
        ).fetchall()
    return [dict(r) for r in rows]
