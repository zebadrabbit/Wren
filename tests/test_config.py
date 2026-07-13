import os
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("HUSBAND_ID", "2")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from wren import config

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
