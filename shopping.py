import sqlite3
from datetime import datetime, timezone

DB_PATH = "wren.db"

def _conn():
    return sqlite3.connect(DB_PATH)

def init_db() -> None:
    with _conn() as con:
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
    with _conn() as con:
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
    with _conn() as con:
        cur = con.execute(
            "UPDATE shopping_items SET status='removed' WHERE item=? AND status='active'",
            (normalized,),
        )
        return cur.rowcount > 0

def active_items() -> list[dict]:
    with _conn() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM shopping_items WHERE status='active' ORDER BY added_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]

def common_items(threshold: int = 3, limit: int = 5) -> list[dict]:
    with _conn() as con:
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
