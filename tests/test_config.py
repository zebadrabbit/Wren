import os
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("HUSBAND_ID", "2")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

import pytest
from wren import config

def test_build_whitelist_owner_only():
    assert config._build_whitelist("1", "") == {"owner": 1}

def test_build_whitelist_with_husband():
    assert config._build_whitelist("1", "2") == {"owner": 1, "husband": 2}

def test_build_whitelist_invalid_owner_raises():
    with pytest.raises(RuntimeError):
        config._build_whitelist("abc", "")

def test_build_whitelist_invalid_husband_raises():
    with pytest.raises(RuntimeError):
        config._build_whitelist("1", "abc")

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
