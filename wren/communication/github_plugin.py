import asyncio
import logging
import httpx
from .. import config
from .. import router
from . import github_state

PLUGIN_NAME = "GitHub Watcher"
ROLE = "input"   # input-only: watches repo activity, never sends; notify a chat plugin via router.notify_name

def is_active() -> bool:
    return bool(config.GITHUB_WATCH)


def inactive_reason() -> str:
    return "GITHUB_WATCH is empty — no repositories are being watched."

def _headers() -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    if config.GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {config.GITHUB_TOKEN}"
    return headers

def _get(url: str):
    resp = httpx.get(url, headers=_headers(), timeout=10)
    resp.raise_for_status()
    return resp

async def _poll_repo(repo: str) -> None:
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
        await router.notify_name("owner", f"{repo} gained a star (total: {star_count})")

    if latest_sha and latest_sha != prior["last_commit_sha"]:
        message = latest_commit["commit"]["message"].splitlines()[0]
        author = latest_commit["commit"]["author"]["name"]
        await router.notify_name("owner", f'{repo}: new push — "{message}" by {author}')

    for item in sorted(issues, key=lambda i: i["number"]):
        if item["number"] > prior["last_issue_number"]:
            kind = "PR" if "pull_request" in item else "issue"
            await router.notify_name("owner", f'{repo} #{item["number"]}: {item["title"]} ({kind})')

    github_state.upsert(repo, star_count, latest_sha, highest_issue_number)

async def start() -> None:
    if not config.GITHUB_WATCH:
        return
    while True:
        for repo in config.GITHUB_WATCH:
            try:
                await _poll_repo(repo)
            except Exception as e:
                logging.warning(f"github watcher poll failed for {repo}: {e}")
        await asyncio.sleep(config.GITHUB_POLL_SECONDS)
