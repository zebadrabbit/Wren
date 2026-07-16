import sqlite3

DB_PATH = "wren.db"

def _conn() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)

def init_db() -> None:
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS github_state (
                repo               TEXT PRIMARY KEY,
                star_count         INTEGER NOT NULL,
                last_commit_sha    TEXT,
                last_issue_number  INTEGER NOT NULL DEFAULT 0
            )
        """)

def get(repo: str) -> dict | None:
    with _conn() as con:
        row = con.execute(
            "SELECT star_count, last_commit_sha, last_issue_number FROM github_state WHERE repo=?",
            (repo,),
        ).fetchone()
    if row is None:
        return None
    return {"star_count": row[0], "last_commit_sha": row[1], "last_issue_number": row[2]}

def upsert(repo: str, star_count: int, last_commit_sha: str | None, last_issue_number: int) -> None:
    with _conn() as con:
        con.execute(
            """
            INSERT INTO github_state (repo, star_count, last_commit_sha, last_issue_number)
            VALUES (?,?,?,?)
            ON CONFLICT(repo) DO UPDATE SET
                star_count=excluded.star_count,
                last_commit_sha=excluded.last_commit_sha,
                last_issue_number=excluded.last_issue_number
            """,
            (repo, star_count, last_commit_sha, last_issue_number),
        )
