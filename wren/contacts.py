from datetime import datetime, timezone

from . import db

def init_db() -> None:
    with db.conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                alias      TEXT PRIMARY KEY,
                discord_id TEXT NOT NULL UNIQUE,
                added_at   TEXT NOT NULL
            )
        """)

def add(alias: str, discord_id: int) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with db.conn() as con:
        con.execute(
            "INSERT INTO contacts (alias, discord_id, added_at) VALUES (?,?,?)",
            (alias.strip().lower(), str(discord_id), ts),
        )

def remove(alias: str) -> bool:
    with db.conn() as con:
        cur = con.execute("DELETE FROM contacts WHERE alias=?", (alias.strip().lower(),))
        return cur.rowcount > 0

def all() -> dict[str, int]:
    with db.conn() as con:
        rows = con.execute("SELECT alias, discord_id FROM contacts").fetchall()
    return {alias: int(discord_id) for alias, discord_id in rows}
