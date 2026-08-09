# GitHub Repo Activity Notifier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wren watches a configured list of GitHub repos and DMs the owner when a repo gains a star, gets a new push to its default branch, or gets a new issue/PR opened.

**Architecture:** A passive background watcher plugin (`github_plugin.py`), following `email_plugin.py`'s exact shape: no user-facing intent, `start(client)` polls on an interval. Last-seen state (star count, latest commit SHA, highest issue/PR number) persists in a new SQLite table (`github_state.py`, mirroring `contacts.py`) so restarts don't cause repeat notifications. Three GitHub REST API calls per repo per poll, via `httpx` (already a dependency).

**Tech Stack:** Python, `httpx` (HTTP), `sqlite3`, pytest, `unittest.mock`.

## Global Constraints

- `GITHUB_WATCH` is a comma-separated `owner/repo` list; `GITHUB_TOKEN` optional but recommended (sent as `Bearer` header); `GITHUB_POLL_SECONDS` defaults to `60` — exact env var names and defaults from the spec.
- No per-repo notification routing — always `discord_utils.notify(client, "owner", text)`.
- First poll for a repo with no prior state records a baseline and sends zero notifications (spec's "First-run behavior").
- Push notifications report only the single latest commit on the default branch, not a full diff of everything pushed since the last poll.
- No new user-facing intent, no `HELP_TEXT` entry — this plugin is passive, discoverable via `list_plugins` only (already generic).
- Tests mock `httpx`/the DB — no real network calls, no real `wren.db` (per this project's standing DB-safety rule: verification/tests always use a temp `DB_PATH`, never the real database file).

---

### Task 1: `github_state.py` — persistence for last-seen state

**Files:**
- Create: `wren/github_state.py`
- Test: `tests/test_github_state.py`

**Interfaces:**
- Consumes: nothing (only `sqlite3`, stdlib).
- Produces: `github_state.DB_PATH` (module-level, monkeypatchable like `contacts.DB_PATH`), `github_state.init_db() -> None`, `github_state.get(repo: str) -> dict | None` (keys: `star_count: int`, `last_commit_sha: str | None`, `last_issue_number: int`), `github_state.upsert(repo: str, star_count: int, last_commit_sha: str | None, last_issue_number: int) -> None`. Task 2's `github_plugin.py` calls all three of these by name.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_github_state.py`:

```python
import os, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from wren import github_state

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(github_state, "DB_PATH", str(tmp_path / "test.db"))
    github_state.init_db()

def test_get_missing_repo_returns_none():
    assert github_state.get("owner/repo") is None

def test_upsert_then_get_round_trips():
    github_state.upsert("owner/repo", 5, "abc123", 10)
    assert github_state.get("owner/repo") == {"star_count": 5, "last_commit_sha": "abc123", "last_issue_number": 10}

def test_upsert_overwrites_existing_row():
    github_state.upsert("owner/repo", 5, "abc123", 10)
    github_state.upsert("owner/repo", 7, "def456", 12)
    assert github_state.get("owner/repo") == {"star_count": 7, "last_commit_sha": "def456", "last_issue_number": 12}

def test_upsert_allows_null_commit_sha():
    github_state.upsert("owner/empty-repo", 0, None, 0)
    assert github_state.get("owner/empty-repo") == {"star_count": 0, "last_commit_sha": None, "last_issue_number": 0}

def test_state_is_independent_per_repo():
    github_state.upsert("owner/repo-a", 1, "aaa", 1)
    github_state.upsert("owner/repo-b", 2, "bbb", 2)
    assert github_state.get("owner/repo-a") == {"star_count": 1, "last_commit_sha": "aaa", "last_issue_number": 1}
    assert github_state.get("owner/repo-b") == {"star_count": 2, "last_commit_sha": "bbb", "last_issue_number": 2}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/winter/work/Wren && venv/bin/pytest tests/test_github_state.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'wren.github_state'`.

- [ ] **Step 3: Write the implementation**

Create `wren/github_state.py`:

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

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/winter/work/Wren && venv/bin/pytest tests/test_github_state.py -v`
Expected: all 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add wren/github_state.py tests/test_github_state.py
git commit -m "feat: add github_state module for GitHub watcher dedupe state"
```

---

### Task 2: `wren/config.py` — env vars for the GitHub watcher

**Files:**
- Modify: `wren/config.py`
- Modify: `.env.example`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `config.GITHUB_TOKEN: str`, `config.GITHUB_WATCH: list[str]`, `config.GITHUB_POLL_SECONDS: int`, `config._parse_github_watch(raw: str) -> list[str]`. Task 3's `github_plugin.py` reads the three module-level values by name.

- [ ] **Step 1: Write the failing tests**

This project's established pattern (see `tests/test_config.py`'s existing `_parse_email_watch`/`EMAIL_POLL_SECONDS` tests) is: extract parsing logic into a pure `_parse_*` helper and test that directly, then separately assert the module-level constant's at-import default — no env-reload/monkeypatch-then-reimport machinery. Follow it exactly. Add to `tests/test_config.py`:

```python
def test_parse_github_watch_empty():
    assert config._parse_github_watch("") == []

def test_parse_github_watch_single_repo():
    assert config._parse_github_watch("owner/repo") == ["owner/repo"]

def test_parse_github_watch_multiple_repos():
    assert config._parse_github_watch("owner/repo-a,owner/repo-b") == ["owner/repo-a", "owner/repo-b"]

def test_parse_github_watch_whitespace_tolerant():
    assert config._parse_github_watch(" owner/repo-a , owner/repo-b ") == ["owner/repo-a", "owner/repo-b"]

def test_github_token_default_empty():
    assert config.GITHUB_TOKEN == ""

def test_github_watch_default_empty():
    assert config.GITHUB_WATCH == []

def test_github_poll_seconds_default():
    assert config.GITHUB_POLL_SECONDS == 60
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/winter/work/Wren && venv/bin/pytest tests/test_config.py -v`
Expected: FAIL — new tests fail with `AttributeError: module 'wren.config' has no attribute '_parse_github_watch'` (or equivalent for the other new names).

- [ ] **Step 3: Implement**

In `wren/config.py`, immediately after the existing `EMAIL_WATCH` block (after the line `EMAIL_WATCH: dict[str, str] = _parse_email_watch(os.environ.get("EMAIL_WATCH", ""))`), add:

```python
def _parse_github_watch(raw: str) -> list[str]:
    return [r.strip() for r in raw.split(",") if r.strip()]


GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_WATCH: list[str] = _parse_github_watch(os.environ.get("GITHUB_WATCH", ""))
GITHUB_POLL_SECONDS = int(os.environ.get("GITHUB_POLL_SECONDS", "60"))
```

In `.env.example`, add a new block after the existing email-watcher block (after the `EMAIL_WATCH=...` example line, before the `# Reminder poll interval` block):

```
# GitHub repo activity watcher (optional — leave GITHUB_WATCH unset to disable)
# GITHUB_TOKEN=ghp_...                  # optional; required for private repos, raises rate limit to 5000/hr
# GITHUB_WATCH=owner/repo,owner/repo2   # comma-separated owner/repo list
# GITHUB_POLL_SECONDS=60
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/winter/work/Wren && venv/bin/pytest tests/test_config.py -v`
Expected: all tests PASS, including the new ones.

- [ ] **Step 5: Commit**

```bash
git add wren/config.py .env.example tests/test_config.py
git commit -m "feat: add GITHUB_TOKEN/GITHUB_WATCH/GITHUB_POLL_SECONDS config"
```

---

### Task 3: `wren/github_plugin.py` — the watcher

**Files:**
- Create: `wren/github_plugin.py`
- Test: `tests/test_github_plugin.py`

**Interfaces:**
- Consumes: `github_state.get(repo) -> dict | None`, `github_state.upsert(repo, star_count, last_commit_sha, last_issue_number) -> None` (Task 1); `config.GITHUB_TOKEN: str`, `config.GITHUB_WATCH: list[str]`, `config.GITHUB_POLL_SECONDS: int` (Task 2); `discord_utils.notify(client, contact_name: str, text: str) -> bool` (existing, unchanged).
- Produces: `github_plugin.PLUGIN_NAME = "GitHub Watcher"`, `github_plugin.is_active() -> bool`, `github_plugin.start(client) -> None` (a coroutine — matches every other plugin's `start`, consumed generically by `plugins.start_all` in Task 4, no code change needed there since `plugins.start_all` already iterates `PLUGINS` and calls `.start` on anything that has it).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_github_plugin.py`:

```python
import os, asyncio, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import MagicMock, AsyncMock, patch
from wren import config, discord_utils, github_state, github_plugin

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(github_state, "DB_PATH", str(tmp_path / "test.db"))
    github_state.init_db()

def _repo_info(stars: int, default_branch: str = "main") -> dict:
    return {"stargazers_count": stars, "default_branch": default_branch}

def _commit(sha: str, message: str = "fix: something", author: str = "zebadrabbit") -> dict:
    return {"sha": sha, "commit": {"message": message, "author": {"name": author}}}

def _issue(number: int, title: str = "Some title", is_pr: bool = False) -> dict:
    item = {"number": number, "title": title}
    if is_pr:
        item["pull_request"] = {}
    return item

def test_is_active_true_when_watch_configured(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_WATCH", ["owner/repo"])
    assert github_plugin.is_active() is True

def test_is_active_false_when_watch_empty(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_WATCH", [])
    assert github_plugin.is_active() is False

def test_poll_repo_first_run_records_baseline_no_notify():
    with patch.object(github_plugin, "_get") as mock_get, \
         patch.object(discord_utils, "notify", new=AsyncMock()) as mock_notify:
        mock_get.side_effect = [
            MagicMock(json=lambda: _repo_info(5)),
            MagicMock(json=lambda: [_commit("sha1")]),
            MagicMock(json=lambda: [_issue(3)]),
        ]
        asyncio.run(github_plugin._poll_repo(None, "owner/repo"))

    mock_notify.assert_not_called()
    assert github_state.get("owner/repo") == {"star_count": 5, "last_commit_sha": "sha1", "last_issue_number": 3}

def test_poll_repo_notifies_on_new_star():
    github_state.upsert("owner/repo", 5, "sha1", 3)
    with patch.object(github_plugin, "_get") as mock_get, \
         patch.object(discord_utils, "notify", new=AsyncMock(return_value=True)) as mock_notify:
        mock_get.side_effect = [
            MagicMock(json=lambda: _repo_info(6)),
            MagicMock(json=lambda: [_commit("sha1")]),
            MagicMock(json=lambda: [_issue(3)]),
        ]
        asyncio.run(github_plugin._poll_repo(None, "owner/repo"))

    mock_notify.assert_awaited_once_with(None, "owner", "owner/repo gained a star (total: 6)")
    assert github_state.get("owner/repo")["star_count"] == 6

def test_poll_repo_no_notify_when_star_count_unchanged():
    github_state.upsert("owner/repo", 5, "sha1", 3)
    with patch.object(github_plugin, "_get") as mock_get, \
         patch.object(discord_utils, "notify", new=AsyncMock()) as mock_notify:
        mock_get.side_effect = [
            MagicMock(json=lambda: _repo_info(5)),
            MagicMock(json=lambda: [_commit("sha1")]),
            MagicMock(json=lambda: [_issue(3)]),
        ]
        asyncio.run(github_plugin._poll_repo(None, "owner/repo"))

    mock_notify.assert_not_called()

def test_poll_repo_notifies_on_new_commit():
    github_state.upsert("owner/repo", 5, "sha1", 3)
    with patch.object(github_plugin, "_get") as mock_get, \
         patch.object(discord_utils, "notify", new=AsyncMock(return_value=True)) as mock_notify:
        mock_get.side_effect = [
            MagicMock(json=lambda: _repo_info(5)),
            MagicMock(json=lambda: [_commit("sha2", message="feat: new thing", author="alice")]),
            MagicMock(json=lambda: [_issue(3)]),
        ]
        asyncio.run(github_plugin._poll_repo(None, "owner/repo"))

    mock_notify.assert_awaited_once_with(None, "owner", 'owner/repo: new push — "feat: new thing" by alice')
    assert github_state.get("owner/repo")["last_commit_sha"] == "sha2"

def test_poll_repo_notifies_on_new_issue_and_pr_with_correct_labels():
    github_state.upsert("owner/repo", 5, "sha1", 3)
    with patch.object(github_plugin, "_get") as mock_get, \
         patch.object(discord_utils, "notify", new=AsyncMock(return_value=True)) as mock_notify:
        mock_get.side_effect = [
            MagicMock(json=lambda: _repo_info(5)),
            MagicMock(json=lambda: [_commit("sha1")]),
            MagicMock(json=lambda: [_issue(5, title="New PR", is_pr=True), _issue(4, title="New issue")]),
        ]
        asyncio.run(github_plugin._poll_repo(None, "owner/repo"))

    assert mock_notify.await_count == 2
    mock_notify.assert_any_await(None, "owner", "owner/repo #5: New PR (PR)")
    mock_notify.assert_any_await(None, "owner", "owner/repo #4: New issue (issue)")
    assert github_state.get("owner/repo")["last_issue_number"] == 5

def test_start_returns_immediately_when_watch_empty(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_WATCH", [])

    def _fail_poll(client, repo):
        raise AssertionError("_poll_repo should not be called when GITHUB_WATCH is empty")

    monkeypatch.setattr(github_plugin, "_poll_repo", _fail_poll)
    asyncio.run(asyncio.wait_for(github_plugin.start(None), timeout=1))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/winter/work/Wren && venv/bin/pytest tests/test_github_plugin.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'wren.github_plugin'`.

- [ ] **Step 3: Write the implementation**

Create `wren/github_plugin.py`:

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

def _get(url: str):
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

    for item in sorted(issues, key=lambda i: i["number"]):
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

Note: `test_poll_repo_notifies_on_new_issue_and_pr_with_correct_labels` expects the PR notification (`#5`) before the issue notification (`#4`) — the implementation's `sorted(issues, key=lambda i: i["number"])` iterates ascending by number, so `#4` fires before `#5`. Re-check that test's `assert_any_await` calls — order doesn't matter for `assert_any_await` (it checks the call happened at any point), so this is fine either way; both calls are asserted independently, not by sequence.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/winter/work/Wren && venv/bin/pytest tests/test_github_plugin.py -v`
Expected: all 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add wren/github_plugin.py tests/test_github_plugin.py
git commit -m "feat: add github_plugin watcher for stars, pushes, issues/PRs"
```

---

### Task 4: Wire into `plugins.py` and `bot.py`

**Files:**
- Modify: `wren/plugins.py`
- Modify: `wren/bot.py`

**Interfaces:**
- Consumes: `github_plugin` module (Task 3, has `PLUGIN_NAME`, `is_active`, `start` — matches every other plugin's shape, so `plugins.py`'s existing generic `INTENT_HANDLERS`/`all_intents`/`all_guidelines`/`plugin_status`/`start_all` all work with zero changes to their bodies); `github_state.init_db()` (Task 1).
- Produces: nothing new consumed by later tasks — this is the final integration point for this plan.

- [ ] **Step 1: Add the import and registration to `wren/plugins.py`**

Current `wren/plugins.py` top:

```python
import asyncio
from . import notes_plugin
from . import shopping_plugin
from . import email_plugin
from . import reminder_plugin
from . import web_plugin
from . import contacts_plugin
from . import pins_plugin

PLUGINS = [notes_plugin, shopping_plugin, email_plugin, reminder_plugin, web_plugin, contacts_plugin, pins_plugin]
```

Replace with:

```python
import asyncio
from . import notes_plugin
from . import shopping_plugin
from . import email_plugin
from . import reminder_plugin
from . import web_plugin
from . import contacts_plugin
from . import pins_plugin
from . import github_plugin

PLUGINS = [notes_plugin, shopping_plugin, email_plugin, reminder_plugin, web_plugin, contacts_plugin, pins_plugin, github_plugin]
```

- [ ] **Step 2: Add `github_state.init_db()` to `wren/bot.py`'s `on_ready`**

Current (`wren/bot.py`, imports and `on_ready`):

```python
from . import contacts
from . import plugins
from . import discord_utils
```
```python
async def on_ready():
    notes.init_db()
    shopping.init_db()
    reminders.init_db()
    contacts.init_db()
    await plugins.start_all(client)
    logging.info(f"Wren online as {client.user}")
```

Replace with:

```python
from . import contacts
from . import plugins
from . import discord_utils
from . import github_state
```
```python
async def on_ready():
    notes.init_db()
    shopping.init_db()
    reminders.init_db()
    contacts.init_db()
    github_state.init_db()
    await plugins.start_all(client)
    logging.info(f"Wren online as {client.user}")
```

- [ ] **Step 3: Verify by reading, not running**

`bot.py` has no test coverage (module-level `client.run()`, pre-existing condition — see Global Constraints in the classifier-history plan for the same note, applies here too). Verify correctness by re-reading the full modified import block and `on_ready` function, confirming `github_state.init_db()` is called alongside the other `init_db()` calls and nothing else in `on_ready` changed.

Run: `cd /home/winter/work/Wren && venv/bin/python -c "import ast; ast.parse(open('wren/bot.py').read())"`
Expected: no output (syntax check passes).

Run: `cd /home/winter/work/Wren && venv/bin/python -c "import ast; ast.parse(open('wren/plugins.py').read())"`
Expected: no output (syntax check passes).

- [ ] **Step 4: Run the full test suite**

Run: `cd /home/winter/work/Wren && venv/bin/pytest -v`
Expected: all PASS, prior count plus this plan's ~13 new tests (5 in `test_github_state.py`, ~4 new in `test_config.py`, 8 in `test_github_plugin.py`) — `bot.py` isn't imported by the test suite, so this task can't break existing tests; this confirms Tasks 1-3's changes still hold together.

- [ ] **Step 5: Commit**

```bash
git add wren/plugins.py wren/bot.py
git commit -m "feat: register github_plugin and init github_state on startup"
```

## Out of scope

- Per-repo notification routing (owner only).
- Named "who starred" attribution.
- Reconstructing every commit pushed since the last poll (latest commit only).
- Any user-facing command/intent for this plugin.
- Webhooks (polling only).
- A `HELP_TEXT` entry (passive watcher, discoverable via `list_plugins` only).
