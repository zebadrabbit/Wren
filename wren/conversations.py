import json
import sqlite3
from datetime import datetime, timezone

from . import db

TITLE_MAX = 60

# The one conversation every new turn for a person goes to. Old conversations
# keep their rows; timeline() reads them all by owner, so nothing is migrated.
HOME_TITLE = "timeline"


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
                created_at      TEXT NOT NULL,
                card            TEXT,
                channel         TEXT,
                kind            TEXT
            )
        """)
        # Additive, nullable migrations for a database that predates each
        # column: `card` (2026-08-17), then `channel` and `kind` (2026-10-13,
        # the one-timeline change). A fresh install gets all three from the
        # CREATE TABLE above. Two overlapping init_db() calls (systemd
        # restarting Wren while someone runs it by hand) can both see a column
        # missing and both ALTER; sqlite runs DDL in autocommit so the loser
        # raises "duplicate column name". That is harmless -- the column is
        # there either way -- so exactly that error is swallowed.
        cols = {row[1] for row in con.execute("PRAGMA table_info(messages)")}
        for col in ("card", "channel", "kind"):
            if col not in cols:
                try:
                    con.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT")
                except sqlite3.OperationalError as e:
                    if "duplicate column name" not in str(e):
                        raise

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


def home(owner_id: int) -> int:
    """This person's home conversation, created on first use.

    ponytail: no lock. Two first turns racing can each insert a home; the
    timeline joins on owner_id so both are read, and later turns land in
    whichever this SELECT finds first. Add a UNIQUE(owner_id, title) partial
    index if a duplicate is ever observed to matter.
    """
    with db.conn() as con:
        row = con.execute(
            "SELECT id FROM conversations WHERE owner_id=? AND title=? ORDER BY id LIMIT 1",
            (str(owner_id), HOME_TITLE),
        ).fetchone()
        if row:
            return row[0]
        ts = _now()
        cur = con.execute(
            "INSERT INTO conversations (owner_id, title, created_at, updated_at) VALUES (?,?,?,?)",
            (str(owner_id), HOME_TITLE, ts, ts),
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


def add_message(conversation_id: int, role: str, content: str,
                card: dict | None = None, *, channel: str | None = None,
                kind: str | None = None) -> int:
    """channel: which door the turn happened on (web, voice, discord,
    telegram). kind: "notice" when Wren spoke first; None for a turn."""
    if role not in ("user", "assistant"):
        raise ValueError(f"role must be 'user' or 'assistant', got {role!r}")
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at, card, channel, kind) "
            "VALUES (?,?,?,?,?,?,?)",
            (conversation_id, role, content, _now(),
             json.dumps(card) if card is not None else None, channel, kind),
        )
        return cur.lastrowid


def messages(conversation_id: int, before_id: int | None = None, limit: int | None = None) -> list[dict]:
    """Oldest-first, the shape brain.detect_intent expects.

    `before_id` excludes the in-flight message: the surface persists what you
    typed before dispatching (so a failed LLM call doesn't lose it), and the
    model already receives that text separately — without this it would see it
    twice.
    """
    sql = "SELECT id, role, content, created_at, card FROM messages WHERE conversation_id=?"
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
        {"id": r[0], "role": r[1], "content": r[2], "created_at": r[3],
         # stored as JSON text; callers want the object. A row written before
         # this column existed is NULL, which must read back as None and not
         # as the string "null".
         "card": json.loads(r[4]) if r[4] else None}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# the timeline: every conversation a person owns, read as one thread
# ---------------------------------------------------------------------------

_ROW = "m.id, m.role, m.content, m.created_at, m.channel, m.kind, m.card"


def _shape(rows) -> list[dict]:
    return [
        {"id": r[0], "role": r[1], "content": r[2], "created_at": r[3],
         "channel": r[4], "kind": r[5],
         # stored as JSON text; a NULL (any row before the column) is None
         "card": json.loads(r[6]) if r[6] else None}
        for r in rows
    ]


def _cursor(con, owner_id: int, message_id: int) -> tuple[str, int] | None:
    """(created_at, id) of one of this owner's rows, or None. Scoped to the
    owner so a cursor naming someone else's row pages nothing of theirs."""
    return con.execute(
        "SELECT m.created_at, m.id FROM messages m JOIN conversations c ON c.id=m.conversation_id "
        "WHERE m.id=? AND c.owner_id=?",
        (message_id, str(owner_id)),
    ).fetchone()


def timeline(owner_id: int, *, before: int | None = None, after: int | None = None,
             limit: int = 50) -> list[dict]:
    """Oldest-first rows of this person's whole timeline.

    Ordered by (created_at, id), not id: rows written before today live in
    several conversations whose ids follow creation order, not time order,
    and this join is what lets them stay where they are. Cursors are ids and
    resolve to that row's (created_at, id) so paging follows the same order.
    `before` returns the newest `limit` rows older than the cursor;
    `after` the oldest `limit` rows newer than it (the poll reads forward).
    """
    sql = (f"SELECT {_ROW} FROM messages m JOIN conversations c ON c.id=m.conversation_id "
           "WHERE c.owner_id=?")
    params: list = [str(owner_id)]
    with db.conn() as con:
        for value, op in ((before, "<"), (after, ">")):
            if value is None:
                continue
            cur = _cursor(con, owner_id, value)
            if cur is None:
                return []
            sql += f" AND (m.created_at, m.id) {op} (?, ?)"
            params.extend(cur)
        if after is not None:
            sql += " ORDER BY m.created_at ASC, m.id ASC LIMIT ?"
            params.append(limit)
            return _shape(con.execute(sql, params).fetchall())
        sql += " ORDER BY m.created_at DESC, m.id DESC LIMIT ?"
        params.append(limit)
        return _shape(reversed(con.execute(sql, params).fetchall()))


def recent(owner_id: int, *, before_id: int, limit: int = 10) -> list[dict]:
    """The model's history window: newest `limit` turns before the in-flight
    one, in the {role, content} shape brain.detect_intent reads. Cards and
    channels are deliberately absent -- the model gets words only."""
    return [{"role": r["role"], "content": r["content"]}
            for r in timeline(owner_id, before=before_id, limit=limit)]


def by_ids(ids: list[int]) -> list[dict]:
    """Timeline-shaped rows for exactly these ids, in timeline order. What the
    HTTP surface hands the page after a turn, so the page draws what was
    stored and not a second rendering of the same reply."""
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    with db.conn() as con:
        rows = con.execute(
            f"SELECT {_ROW} FROM messages m WHERE m.id IN ({marks}) ORDER BY m.created_at, m.id",
            list(ids),
        ).fetchall()
    return _shape(rows)


def notice(owner_id: int, text: str, channel: str) -> int:
    """Wren spoke first: a reminder, the briefing, a watcher alert. Written by
    the router after a surface delivered it, naming that surface."""
    return add_message(home(owner_id), "assistant", text, channel=channel, kind="notice")
