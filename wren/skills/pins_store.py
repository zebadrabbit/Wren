from datetime import datetime, timezone

from .. import db

def init_db() -> None:
    with db.conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pins (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id   TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

def save(owner_id: int, content: str) -> int:
    ts = datetime.now(timezone.utc).isoformat()
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO pins (owner_id, content, created_at) VALUES (?,?,?)",
            (str(owner_id), content, ts),
        )
        return cur.lastrowid

def all(owner_id: int) -> list[dict]:
    with db.conn() as con:
        rows = con.execute(
            "SELECT id, content, created_at FROM pins WHERE owner_id=? ORDER BY created_at ASC",
            (str(owner_id),),
        ).fetchall()
    return [{"id": r[0], "content": r[1], "created_at": r[2]} for r in rows]

def find(owner_id: int, substring: str) -> list[dict]:
    needle = substring.lower()
    return [p for p in all(owner_id) if needle in p["content"].lower()]

def delete(pin_id: int) -> bool:
    with db.conn() as con:
        cur = con.execute("DELETE FROM pins WHERE id=?", (pin_id,))
        return cur.rowcount > 0
