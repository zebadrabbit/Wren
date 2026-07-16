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
