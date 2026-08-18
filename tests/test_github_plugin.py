import os, asyncio, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import MagicMock, AsyncMock, patch
from wren import config, router
from wren.communication import github_state, github_plugin

@pytest.fixture(autouse=True)
def tmp_db():
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
         patch.object(router, "notify_name", new=AsyncMock()) as mock_notify:
        mock_get.side_effect = [
            MagicMock(json=lambda: _repo_info(5)),
            MagicMock(json=lambda: [_commit("sha1")]),
            MagicMock(json=lambda: [_issue(3)]),
        ]
        asyncio.run(github_plugin._poll_repo("owner/repo"))

    mock_notify.assert_not_called()
    assert github_state.get("owner/repo") == {"star_count": 5, "last_commit_sha": "sha1", "last_issue_number": 3}

def test_poll_repo_notifies_on_new_star():
    github_state.upsert("owner/repo", 5, "sha1", 3)
    with patch.object(github_plugin, "_get") as mock_get, \
         patch.object(router, "notify_name", new=AsyncMock(return_value=True)) as mock_notify:
        mock_get.side_effect = [
            MagicMock(json=lambda: _repo_info(6)),
            MagicMock(json=lambda: [_commit("sha1")]),
            MagicMock(json=lambda: [_issue(3)]),
        ]
        asyncio.run(github_plugin._poll_repo("owner/repo"))

    mock_notify.assert_awaited_once_with("owner", "owner/repo gained a star (total: 6)")
    assert github_state.get("owner/repo")["star_count"] == 6

def test_poll_repo_no_notify_when_star_count_unchanged():
    github_state.upsert("owner/repo", 5, "sha1", 3)
    with patch.object(github_plugin, "_get") as mock_get, \
         patch.object(router, "notify_name", new=AsyncMock()) as mock_notify:
        mock_get.side_effect = [
            MagicMock(json=lambda: _repo_info(5)),
            MagicMock(json=lambda: [_commit("sha1")]),
            MagicMock(json=lambda: [_issue(3)]),
        ]
        asyncio.run(github_plugin._poll_repo("owner/repo"))

    mock_notify.assert_not_called()

def test_poll_repo_notifies_on_new_commit():
    github_state.upsert("owner/repo", 5, "sha1", 3)
    with patch.object(github_plugin, "_get") as mock_get, \
         patch.object(router, "notify_name", new=AsyncMock(return_value=True)) as mock_notify:
        mock_get.side_effect = [
            MagicMock(json=lambda: _repo_info(5)),
            MagicMock(json=lambda: [_commit("sha2", message="feat: new thing", author="alice")]),
            MagicMock(json=lambda: [_issue(3)]),
        ]
        asyncio.run(github_plugin._poll_repo("owner/repo"))

    mock_notify.assert_awaited_once_with("owner", 'owner/repo: new push — "feat: new thing" by alice')
    assert github_state.get("owner/repo")["last_commit_sha"] == "sha2"

def test_poll_repo_notifies_on_new_issue_and_pr_with_correct_labels():
    github_state.upsert("owner/repo", 5, "sha1", 3)
    with patch.object(github_plugin, "_get") as mock_get, \
         patch.object(router, "notify_name", new=AsyncMock(return_value=True)) as mock_notify:
        mock_get.side_effect = [
            MagicMock(json=lambda: _repo_info(5)),
            MagicMock(json=lambda: [_commit("sha1")]),
            MagicMock(json=lambda: [_issue(5, title="New PR", is_pr=True), _issue(4, title="New issue")]),
        ]
        asyncio.run(github_plugin._poll_repo("owner/repo"))

    assert mock_notify.await_count == 2
    mock_notify.assert_any_await("owner", "owner/repo #5: New PR (PR)")
    mock_notify.assert_any_await("owner", "owner/repo #4: New issue (issue)")
    assert github_state.get("owner/repo")["last_issue_number"] == 5

def test_start_returns_immediately_when_watch_empty(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_WATCH", [])

    def _fail_poll(repo):
        raise AssertionError("_poll_repo should not be called when GITHUB_WATCH is empty")

    monkeypatch.setattr(github_plugin, "_poll_repo", _fail_poll)
    asyncio.run(asyncio.wait_for(github_plugin.start(), timeout=1))
