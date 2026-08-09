from datetime import datetime, timezone

from . import db

TITLE_MAX = 60


def init_db() -> None:
    with db.conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id   TEXT NOT NULL,
                title      TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role            TEXT NOT NULL,
                content         TEXT NOT NULL,
                created_at      TEXT NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_conv_owner ON conversations(owner_id, updated_at DESC)")


def _now() -> str:
    # microseconds, not seconds: list_for orders by updated_at DESC and two
    # conversations touched in the same second would tie, letting an idle
    # newer conversation sort above the one you just replied to
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def title_from(text: str) -> str:
    """First user message, truncated. Cheaper than an LLM round-trip to name
    something the user can rename."""
    clean = " ".join((text or "").split()) or "New chat"
    return clean if len(clean) <= TITLE_MAX else clean[: TITLE_MAX - 1] + "…"


def create(owner_id: int, title: str = "New chat") -> int:
    ts = _now()
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO conversations (owner_id, title, created_at, updated_at) VALUES (?,?,?,?)",
            (str(owner_id), title, ts, ts),
        )
        return cur.lastrowid


def list_for(owner_id: int) -> list[dict]:
    with db.conn() as con:
        rows = con.execute(
            "SELECT id, title, created_at, updated_at FROM conversations "
            "WHERE owner_id=? ORDER BY updated_at DESC, id DESC",
            (str(owner_id),),
        ).fetchall()
    return [
        {"id": r[0], "title": r[1], "created_at": r[2], "updated_at": r[3]}
        for r in rows
    ]


def get(conversation_id: int, owner_id: int) -> dict | None:
    """None when it does not exist OR belongs to someone else — the caller
    turns both into a 404, so an id cannot be probed for existence."""
    with db.conn() as con:
        row = con.execute(
            "SELECT id, title, created_at, updated_at FROM conversations WHERE id=? AND owner_id=?",
            (conversation_id, str(owner_id)),
        ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "title": row[1], "created_at": row[2], "updated_at": row[3]}


def rename(conversation_id: int, owner_id: int, title: str) -> bool:
    title = " ".join((title or "").split())
    if not title:
        return False
    with db.conn() as con:
        cur = con.execute(
            "UPDATE conversations SET title=?, updated_at=? WHERE id=? AND owner_id=?",
            (title[:TITLE_MAX], _now(), conversation_id, str(owner_id)),
        )
        return cur.rowcount > 0


def delete(conversation_id: int, owner_id: int) -> bool:
    with db.conn() as con:
        cur = con.execute(
            "DELETE FROM conversations WHERE id=? AND owner_id=?",
            (conversation_id, str(owner_id)),
        )
        if cur.rowcount == 0:
            return False
        # explicit rather than ON DELETE CASCADE: sqlite only honours foreign
        # keys when PRAGMA foreign_keys=ON is set per-connection, which is off
        # by default and easy to forget on a new connection
        con.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
        return True


def touch(conversation_id: int, owner_id: int) -> None:
    with db.conn() as con:
        con.execute(
            "UPDATE conversations SET updated_at=? WHERE id=? AND owner_id=?",
            (_now(), conversation_id, str(owner_id)),
        )


def add_message(conversation_id: int, role: str, content: str) -> int:
    if role not in ("user", "assistant"):
        raise ValueError(f"role must be 'user' or 'assistant', got {role!r}")
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?,?,?,?)",
            (conversation_id, role, content, _now()),
        )
        return cur.lastrowid


def messages(conversation_id: int, before_id: int | None = None, limit: int | None = None) -> list[dict]:
    """Oldest-first, the shape brain.detect_intent expects.

    `before_id` excludes the in-flight message: the surface persists what you
    typed before dispatching (so a failed LLM call doesn't lose it), and the
    model already receives that text separately — without this it would see it
    twice.
    """
    sql = "SELECT id, role, content, created_at FROM messages WHERE conversation_id=?"
    params: list = [conversation_id]
    if before_id is not None:
        sql += " AND id < ?"
        params.append(before_id)
    if limit is not None:
        # newest N, then flip — a plain LIMIT on an ascending sort would give
        # the OLDEST N, i.e. the least relevant context
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
    else:
        sql += " ORDER BY id ASC"
    with db.conn() as con:
        rows = con.execute(sql, params).fetchall()
    if limit is not None:
        rows = list(reversed(rows))
    return [
        {"id": r[0], "role": r[1], "content": r[2], "created_at": r[3]}
        for r in rows
    ]
