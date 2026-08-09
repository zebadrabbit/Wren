# GitHub Repo Activity Notifier — Design

## Goal

Wren watches a configured list of GitHub repos and DMs the owner when a
repo gains a star, gets a new push to its default branch, or gets a new
issue/PR opened. Follows the existing `email_plugin.py` shape: a passive
background watcher with no user-facing intent, polling on an interval and
notifying via `discord_utils.notify`.

## Config (`wren/config.py`)

Three new env vars, parsed the same way `EMAIL_WATCH`/`EMAIL_POLL_SECONDS`
already are:

```python
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_WATCH: list[str] = [r.strip() for r in os.environ.get("GITHUB_WATCH", "").split(",") if r.strip()]
GITHUB_POLL_SECONDS = int(os.environ.get("GITHUB_POLL_SECONDS", "60"))
```

`GITHUB_WATCH` is a comma-separated list of `owner/repo` strings, e.g.
`GITHUB_WATCH=zebadrabbit/Wren,zebadrabbit/other-repo`. `GITHUB_TOKEN` is
sent as a `Bearer` header on every request — required for private repos,
and raises the GitHub API rate limit from 60/hr (unauthenticated) to
5000/hr. At 3 requests per repo per poll (see below), even 5 watched repos
at the default 60s interval is ~900 requests/hr — well under the
authenticated limit.

`.env.example` gets a new commented block, following the existing
`EMAIL_WATCH` block's format and placement (after the email section).

## `wren/github_state.py` (new module — dedupe state)

Mirrors `wren/contacts.py`'s shape exactly: module-level `DB_PATH`,
`_conn()`, `init_db()`, plain functions, no classes.

```python
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
```

Registered in `wren/bot.py`'s `on_ready` alongside the other `init_db()`
calls (`notes.init_db()`, `shopping.init_db()`, `reminders.init_db()`,
`contacts.init_db()`).

## `wren/github_plugin.py` (new module — the watcher)

Mirrors `wren/email_plugin.py`'s shape: no `INTENTS`/`PROMPT_GUIDELINES`
(passive watcher, nothing to classify), `PLUGIN_NAME`, `is_active()`,
`start(client)`.

```python
import asyncio
import logging
import httpx
from . import config
from . import discord_utils
from . import github_state

PLUGIN_NAME = "GitHub Watcher"

def is_active() -> bool:
    return bool(config.GITHUB_WATCH)

def _headers() -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    if config.GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {config.GITHUB_TOKEN}"
    return headers

def _get(url: str) -> httpx.Response:
    resp = httpx.get(url, headers=_headers(), timeout=10)
    resp.raise_for_status()
    return resp

async def _poll_repo(client, repo: str) -> None:
    prior = github_state.get(repo)

    info = _get(f"https://api.github.com/repos/{repo}").json()
    star_count = info["stargazers_count"]
    default_branch = info["default_branch"]

    commits = _get(f"https://api.github.com/repos/{repo}/commits?sha={default_branch}&per_page=1").json()
    latest_sha = commits[0]["sha"] if commits else None
    latest_commit = commits[0] if commits else None

    issues = _get(f"https://api.github.com/repos/{repo}/issues?state=all&sort=created&direction=desc&per_page=10").json()
    highest_issue_number = max((i["number"] for i in issues), default=0)

    if prior is None:
        github_state.upsert(repo, star_count, latest_sha, highest_issue_number)
        return

    if star_count > prior["star_count"]:
        await discord_utils.notify(client, "owner", f"{repo} gained a star (total: {star_count})")

    if latest_sha and latest_sha != prior["last_commit_sha"]:
        message = latest_commit["commit"]["message"].splitlines()[0]
        author = latest_commit["commit"]["author"]["name"]
        await discord_utils.notify(client, "owner", f'{repo}: new push — "{message}" by {author}')

    for item in issues:
        if item["number"] > prior["last_issue_number"]:
            kind = "PR" if "pull_request" in item else "issue"
            await discord_utils.notify(client, "owner", f'{repo} #{item["number"]}: {item["title"]} ({kind})')

    github_state.upsert(repo, star_count, latest_sha, highest_issue_number)

async def start(client) -> None:
    if not config.GITHUB_WATCH:
        return
    while True:
        for repo in config.GITHUB_WATCH:
            try:
                await _poll_repo(client, repo)
            except Exception as e:
                logging.warning(f"github watcher poll failed for {repo}: {e}")
        await asyncio.sleep(config.GITHUB_POLL_SECONDS)
```

**First-run behavior:** when a repo has no `github_state` row yet, the
first poll records a baseline (current star count, latest commit SHA,
highest issue/PR number) and sends no notifications — otherwise every
newly-added repo would immediately report its entire star/commit/issue
history as "new."

**Push notification scope:** reports only the single latest commit on the
default branch, not every commit pushed since the last poll. If five
commits land between polls, one notification fires naming the latest —
matches "report current state changed" rather than "diff every commit,"
consistent with how `email_plugin` reports each unseen message individually
but doesn't attempt to reconstruct missed history.

## `wren/plugins.py`

```python
from . import github_plugin
...
PLUGINS = [notes_plugin, shopping_plugin, email_plugin, reminder_plugin, web_plugin, contacts_plugin, pins_plugin, github_plugin]
```

Automatically picked up by `list_plugins` (`plugin_status()` already
iterates `PLUGINS` generically) — no changes needed there.

## `wren/bot.py`

- `from . import github_state` added to imports.
- `github_state.init_db()` added to `on_ready` alongside the other
  `init_db()` calls.
- `HELP_TEXT` gets one line under a note that this plugin is passive (no
  commands) — following the pattern of how `email_plugin` (also passive)
  isn't mentioned in `HELP_TEXT` today, this plugin similarly won't get a
  `HELP_TEXT` entry, since `HELP_TEXT` documents things the user can *say*,
  not passive watchers. Visibility comes from `list_plugins` instead
  (already covers it for free).

## Testing

Mirrors `tests/test_email_plugin.py`'s approach: `httpx.get` calls are
mocked (`unittest.mock.patch`), so no real network calls in tests.
Coverage: `is_active()` true/false on `GITHUB_WATCH` presence; first-run
baseline records state and sends zero notifications; a star-count increase
notifies with the right text; an unchanged star count does not notify; a
changed commit SHA notifies with commit message/author; an unchanged SHA
does not notify; a higher issue number notifies, tagged `(issue)` vs
`(PR)` based on the `pull_request` key; multiple new issues in one poll
each get their own notification; `github_state.upsert`/`get` round-trip
correctly via a temp DB (same `tmp_db` fixture pattern already used in
`tests/test_brain.py`/`tests/test_contacts_plugin.py`).

## Out of scope

- Per-repo notification routing (owner only, no `EMAIL_WATCH`-style
  `repo:contact` mapping).
- Named "who starred" attribution (would require the paginated
  `/stargazers` endpoint with the `star+json` media type — star count only
  for now).
- Reconstructing every commit pushed since the last poll (latest commit
  only).
- Any user-facing command/intent for this plugin (passive watcher only,
  same as `email_plugin`).
- Webhooks (polling only, matching `email_plugin`'s IMAP-polling
  precedent — no public endpoint to receive GitHub webhook payloads).
