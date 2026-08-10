from datetime import datetime, timezone

from . import db

# Runtime-editable settings, as key -> raw string. Deliberately shaped like
# contacts.py, the other runtime-mutable store in this codebase.
#
# Only DEVIATIONS from .env live here. A missing row means "whatever .env
# said", which is what makes unset() mean *revert to the file* rather than
# *set to empty* -- and what makes a fresh install behave exactly as before.


def init_db() -> None:
    with db.conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)


def get(key: str) -> str | None:
    with db.conn() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set(key: str, value: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with db.conn() as con:
        con.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, str(value), ts),
        )


def unset(key: str) -> bool:
    with db.conn() as con:
        cur = con.execute("DELETE FROM settings WHERE key=?", (key,))
        return cur.rowcount > 0


def all() -> dict[str, str]:
    with db.conn() as con:
        rows = con.execute("SELECT key, value FROM settings").fetchall()
    return {key: value for key, value in rows}
