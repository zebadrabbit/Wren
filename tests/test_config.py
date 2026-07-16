import os
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")
os.environ.setdefault("TIMEZONE", "UTC")

import pytest
from wren import config, contacts

def test_build_whitelist_owner_only():
    assert config._build_whitelist("1") == {"owner": 1}

def test_build_whitelist_invalid_owner_raises():
    with pytest.raises(RuntimeError):
        config._build_whitelist("abc")

def test_parse_email_watch_empty():
    assert config._parse_email_watch("") == {}

def test_parse_email_watch_single_pair():
    assert config._parse_email_watch("alice@example.com:owner") == {"alice@example.com": "owner"}

def test_parse_email_watch_multiple_pairs():
    result = config._parse_email_watch("alice@example.com:owner,bob@example.com:husband")
    assert result == {"alice@example.com": "owner", "bob@example.com": "husband"}

def test_parse_email_watch_whitespace_and_case_tolerant():
    result = config._parse_email_watch(" Alice@Example.com : Owner , bob@example.com:husband ")
    assert result == {"alice@example.com": "owner", "bob@example.com": "husband"}

def test_email_poll_seconds_default():
    assert config.EMAIL_POLL_SECONDS == 60

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

def test_timezone_default_is_utc():
    assert config.TIMEZONE == "UTC"

def test_validate_timezone_valid():
    assert config._validate_timezone("America/Chicago") == "America/Chicago"

def test_validate_timezone_invalid_raises():
    with pytest.raises(RuntimeError):
        config._validate_timezone("Not/AZone")

def test_validate_timezone_empty_raises():
    with pytest.raises(RuntimeError):
        config._validate_timezone("")

def test_validate_timezone_strips_whitespace():
    assert config._validate_timezone(" America/Chicago ") == "America/Chicago"

def test_web_lookup_defaults_empty(monkeypatch):
    # unset -> empty strings (feature disabled)
    import importlib
    from wren import config as cfg
    assert isinstance(cfg.SEARXNG_URL, str)
    assert isinstance(cfg.FIRECRAWL_URL, str)
    assert isinstance(cfg.FIRECRAWL_API_KEY, str)

@pytest.fixture
def tmp_contacts_db(tmp_path, monkeypatch):
    monkeypatch.setattr(contacts, "DB_PATH", str(tmp_path / "test.db"))
    contacts.init_db()

def test_whitelist_is_owner_only_with_no_contacts(tmp_contacts_db):
    assert config.whitelist() == {"owner": 1}

def test_whitelist_merges_in_dynamic_contacts(tmp_contacts_db):
    contacts.add("hubby", 222222222222222222)
    assert config.whitelist() == {"owner": 1, "hubby": 222222222222222222}

def test_whitelist_reflects_removal_immediately(tmp_contacts_db):
    contacts.add("hubby", 222222222222222222)
    contacts.remove("hubby")
    assert config.whitelist() == {"owner": 1}

def test_id_to_name_is_inverse_of_whitelist(tmp_contacts_db):
    contacts.add("hubby", 222222222222222222)
    assert config.id_to_name() == {1: "owner", 222222222222222222: "hubby"}
