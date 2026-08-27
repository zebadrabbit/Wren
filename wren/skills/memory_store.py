import sqlite3
from datetime import datetime, timezone

from .. import db

def init_db() -> None:
    with db.conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id      TEXT NOT NULL,
                category      TEXT NOT NULL,
                fact          TEXT NOT NULL,
                mention_count INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT NOT NULL,
                last_seen_at  TEXT NOT NULL
            )
        """)
        # The queue is a table rather than an in-process list on purpose: a
        # restart between "you said it" and "the sweeper read it" must not
        # lose the turn, and every surface writes to it, not just web chat.
        con.execute("""
            CREATE TABLE IF NOT EXISTS memory_queue (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id   TEXT NOT NULL,
                text       TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_memories_owner ON memories(owner_id)")

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def save(owner_id: int, category: str, fact: str) -> int:
    ts = _now()
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO memories (owner_id, category, fact, mention_count, created_at, last_seen_at)"
            " VALUES (?,?,?,1,?,?)",
            (str(owner_id), category, fact, ts, ts),
        )
        return cur.lastrowid

def all_for(owner_id: int) -> list[dict]:
    with db.conn() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM memories WHERE owner_id=?"
            " ORDER BY mention_count DESC, last_seen_at DESC",
            (str(owner_id),),
        ).fetchall()
    return [dict(r) for r in rows]

def bump(memory_id: int) -> None:
    with db.conn() as con:
        con.execute(
            "UPDATE memories SET mention_count = mention_count + 1, last_seen_at=? WHERE id=?",
            (_now(), memory_id),
        )

def forget(memory_id: int) -> bool:
    with db.conn() as con:
        return con.execute("DELETE FROM memories WHERE id=?", (memory_id,)).rowcount > 0

def enqueue(owner_id: int, text: str) -> None:
    with db.conn() as con:
        con.execute(
            "INSERT INTO memory_queue (owner_id, text, created_at) VALUES (?,?,?)",
            (str(owner_id), text, _now()),
        )

def drain(limit: int = 20) -> list[dict]:
    """Take up to `limit` queued turns and delete them in the same transaction.

    Delete-on-read rather than mark-as-done: a turn that makes the extractor
    fall over must not come back every sweep forever, and anything genuinely
    worth remembering gets said again.
    """
    with db.conn() as con:
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM memory_queue ORDER BY id LIMIT ?", (limit,)).fetchall()]
        if rows:
            con.execute(
                "DELETE FROM memory_queue WHERE id IN (%s)" % ",".join("?" * len(rows)),
                [r["id"] for r in rows],
            )
    return rows
