"""What the Gmail watcher saw in the last day, for the briefing's digest.

With EMAIL_DIGEST on, a watched sender's mail is queued here instead of
pushed as it arrives; the daily briefing lists it. Rows older than a day are
pruned on every add, so "recent" is always the last 24 hours and nothing
needs marking as reported -- an on-demand "what's my day look like" and the
scheduled push show the same thing.
"""
from datetime import datetime, timedelta, timezone

from .. import db

_KEEP = timedelta(hours=24)


def init_db() -> None:
    with db.conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS email_digest (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                contact  TEXT NOT NULL,
                sender   TEXT NOT NULL,
                subject  TEXT NOT NULL,
                seen_at  TEXT NOT NULL
            )
        """)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def add(contact: str, sender: str, subject: str) -> None:
    cutoff = (datetime.now(timezone.utc) - _KEEP).isoformat(timespec="seconds")
    with db.conn() as con:
        con.execute("DELETE FROM email_digest WHERE seen_at < ?", (cutoff,))
        con.execute("INSERT INTO email_digest (contact, sender, subject, seen_at) VALUES (?,?,?,?)",
                    (contact, sender, subject, _now()))


def recent(contact: str) -> list[dict]:
    """This contact's watched mail from the last day, newest first."""
    cutoff = (datetime.now(timezone.utc) - _KEEP).isoformat(timespec="seconds")
    with db.conn() as con:
        rows = con.execute(
            "SELECT sender, subject, seen_at FROM email_digest WHERE contact=? AND seen_at >= ?"
            " ORDER BY seen_at DESC, id DESC", (contact, cutoff)).fetchall()
    return [{"sender": s, "subject": j, "seen_at": t} for s, j, t in rows]
