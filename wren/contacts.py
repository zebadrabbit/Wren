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
                wren_id     TEXT,
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
                    wren_id     TEXT,
                    added_at    TEXT NOT NULL
                );
                INSERT INTO contacts_new (alias, discord_id, added_at)
                    SELECT alias, discord_id, added_at FROM contacts;
                DROP TABLE contacts;
                ALTER TABLE contacts_new RENAME TO contacts;
            """)
        # Before 2026-10-14 a person's Wren id was computed, COALESCE(discord_id,
        # telegram_id), so a Telegram-first person who later gained a Discord
        # id changed id and lost their notes. It is stored now: the id they
        # had under the old rule is written once and never moves.
        if "wren_id" not in {row[1] for row in con.execute("PRAGMA table_info(contacts)")}:
            con.execute("ALTER TABLE contacts ADD COLUMN wren_id TEXT")
        con.execute("UPDATE contacts SET wren_id = COALESCE(discord_id, telegram_id) WHERE wren_id IS NULL")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS contacts_wren_id ON contacts(wren_id)")


def add(alias: str, surface_id: int, surface: str = "discord") -> None:
    col = _col(surface)
    ts = datetime.now(timezone.utc).isoformat()
    with db.conn() as con:
        # the first id a person is added with is their Wren id for good
        con.execute(
            f"INSERT INTO contacts (alias, {col}, wren_id, added_at) VALUES (?,?,?,?)",
            (alias.strip().lower(), str(surface_id), str(surface_id), ts),
        )


def set_id(alias: str, surface: str, surface_id: int) -> bool:
    """Give an existing contact their id on another surface."""
    col = _col(surface)
    with db.conn() as con:
        cur = con.execute(f"UPDATE contacts SET {col}=? WHERE alias=?",
                          (str(surface_id), alias.strip().lower()))
        return cur.rowcount > 0


class Taken(ValueError):
    """A refusal the person can read: the reason is the message."""


def register(alias: str, surface: str, surface_id: int, reserved=()) -> bool:
    """Add a person, or give an existing one their id on another surface:
    the one rule set behind "add 555 as kim on telegram" and the People tab.
    True when the person is new. Raises Taken, with the reason, when the name
    is reserved (the owner's) and not already a contact, when the person
    already has an id on that surface, or when the id is registered under
    another name."""
    _col(surface)
    alias = alias.strip().lower()
    row = get(alias)
    if row is None:
        if alias in reserved:
            raise Taken("That name's already taken.")
    elif row[surface] is not None:
        raise Taken(f"{alias} already has a {surface} id.")
    try:
        if row is None:
            add(alias, surface_id, surface=surface)
        else:
            set_id(alias, surface, surface_id)
    except sqlite3.IntegrityError:
        raise Taken("That user ID is already registered under another name.") from None
    return row is None


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
# what core's whitelist gate checks -- is the id they were first added with,
# stored at add() and never changed by a second surface. Every chat surface
# translates its own id to this one on the way in and back on the way out.
_WREN_ID = "wren_id"


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
