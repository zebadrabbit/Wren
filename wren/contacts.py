import sqlite3
from datetime import datetime, timezone

from . import db

# One id column per surface that has ids of its own. Adding a surface with
# its own id space means adding a column here; nothing else in Wren knows the
# list. The web chat is deliberately absent: it authenticates by token, and a
# token maps straight onto a Wren id (config.WREN_TOKENS).
SURFACES = ("discord", "telegram")


def _col(surface: str) -> str:
    if surface not in SURFACES:
        raise ValueError(f"unknown surface {surface!r}; contacts hold ids for {SURFACES}")
    return f"{surface}_id"


def init_db() -> None:
    with db.conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                alias       TEXT PRIMARY KEY,
                discord_id  TEXT UNIQUE,
                telegram_id TEXT UNIQUE,
                added_at    TEXT NOT NULL
            )
        """)
        # Before 2026-09-26 the table was (alias, discord_id NOT NULL UNIQUE,
        # added_at). SQLite cannot drop NOT NULL or add a UNIQUE column in
        # place, so an old table is rebuilt; it holds a household, not a
        # customer base, and the copy is one statement.
        info = {row[1]: row for row in con.execute("PRAGMA table_info(contacts)")}
        if "telegram_id" not in info or info["discord_id"][3]:   # [3] is notnull
            con.executescript("""
                CREATE TABLE contacts_new (
                    alias       TEXT PRIMARY KEY,
                    discord_id  TEXT UNIQUE,
                    telegram_id TEXT UNIQUE,
                    added_at    TEXT NOT NULL
                );
                INSERT INTO contacts_new (alias, discord_id, added_at)
                    SELECT alias, discord_id, added_at FROM contacts;
                DROP TABLE contacts;
                ALTER TABLE contacts_new RENAME TO contacts;
            """)


def add(alias: str, surface_id: int, surface: str = "discord") -> None:
    col = _col(surface)
    ts = datetime.now(timezone.utc).isoformat()
    with db.conn() as con:
        con.execute(
            f"INSERT INTO contacts (alias, {col}, added_at) VALUES (?,?,?)",
            (alias.strip().lower(), str(surface_id), ts),
        )


def set_id(alias: str, surface: str, surface_id: int) -> bool:
    """Give an existing contact their id on another surface."""
    col = _col(surface)
    with db.conn() as con:
        cur = con.execute(f"UPDATE contacts SET {col}=? WHERE alias=?",
                          (str(surface_id), alias.strip().lower()))
        return cur.rowcount > 0


def get(alias: str) -> dict | None:
    """One contact's row: alias and the id on each surface (None where unset)."""
    with db.conn() as con:
        row = con.execute(
            f"SELECT alias, {', '.join(_col(s) for s in SURFACES)} FROM contacts WHERE alias=?",
            (alias.strip().lower(),)).fetchone()
    if not row:
        return None
    return {"alias": row[0], **{s: (int(v) if v is not None else None) for s, v in zip(SURFACES, row[1:])}}


def remove(alias: str) -> bool:
    with db.conn() as con:
        cur = con.execute("DELETE FROM contacts WHERE alias=?", (alias.strip().lower(),))
        return cur.rowcount > 0


# A contact's Wren id -- what every note, reminder and pin is filed under, and
# what core's whitelist gate checks -- is their Discord id when they have one,
# otherwise their Telegram id. That is the rule README states for a
# Telegram-first install (WREN_OWNER_ID is the Telegram id), applied per
# person, so a Discord-era install and a Telegram-only one key rows the same way.
_WREN_ID = "COALESCE(discord_id, telegram_id)"


def all() -> dict[str, int]:
    with db.conn() as con:
        rows = con.execute(f"SELECT alias, {_WREN_ID} FROM contacts").fetchall()
    return {alias: int(wren_id) for alias, wren_id in rows}


def wren_id(surface: str, surface_id: int) -> int | None:
    """Who is talking, given the number a surface calls them."""
    col = _col(surface)
    with db.conn() as con:
        row = con.execute(f"SELECT {_WREN_ID} FROM contacts WHERE {col}=?",
                          (str(surface_id),)).fetchone()
    return int(row[0]) if row else None


def surface_id(surface: str, wren_id: int) -> int | None:
    """The inverse, for delivery: where a surface should send to reach this person."""
    col = _col(surface)
    with db.conn() as con:
        row = con.execute(f"SELECT {col} FROM contacts WHERE {_WREN_ID}=?",
                          (str(wren_id),)).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def surfaces_of(wren_id: int) -> list[str]:
    """The surfaces this contact has an id on, in SURFACES order. Empty for
    the owner, who is not a contact."""
    with db.conn() as con:
        row = con.execute(
            f"SELECT {', '.join(_col(s) for s in SURFACES)} FROM contacts WHERE {_WREN_ID}=?",
            (str(wren_id),)).fetchone()
    if not row:
        return []
    return [s for s, v in zip(SURFACES, row) if v is not None]
