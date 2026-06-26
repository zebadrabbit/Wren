import sqlite3
from datetime import datetime, timezone

DB_PATH = "wren.db"

def _conn():
    return sqlite3.connect(DB_PATH)

def init_db() -> None:
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id   TEXT NOT NULL,
                content    TEXT NOT NULL,
                tags       TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)

def save(owner_id: int, content: str, tags: list[str]) -> int:
    ts = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO notes (owner_id, content, tags, created_at) VALUES (?,?,?,?)",
            (str(owner_id), content, ",".join(tags), ts),
        )
        return cur.lastrowid

def search(owner_id: int, tags: list[str] | None = None) -> list[dict]:
    with _conn() as con:
        con.row_factory = sqlite3.Row
        if tags:
            like_clauses = " OR ".join("tags LIKE ?" for _ in tags)
            params = [f"%{t}%" for t in tags] + [str(owner_id)]
            rows = con.execute(
                f"SELECT * FROM notes WHERE ({like_clauses}) AND owner_id=? ORDER BY created_at DESC",
                params,
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM notes WHERE owner_id=? ORDER BY created_at DESC",
                (str(owner_id),),
            ).fetchall()
    return [dict(r) for r in rows]

def list_recent(owner_id: int, n: int = 10) -> list[dict]:
    with _conn() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM notes WHERE owner_id=? ORDER BY created_at DESC LIMIT ?",
            (str(owner_id), n),
        ).fetchall()
    return [dict(r) for r in rows]
