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
    monkeypatch.setenv("WREN_DB", str(tmp_path / "wren.db"))
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

# ── surfaces / token config (added with the transport split) ────────────────

def test_parse_tokens_empty_is_empty_dict():
    assert config._parse_tokens("") == {}
    assert config._parse_tokens("   ") == {}

def test_parse_tokens_single_pair():
    assert config._parse_tokens("secret:412341234123") == {"secret": 412341234123}

def test_parse_tokens_multiple_pairs_and_whitespace():
    assert config._parse_tokens(" a:1 , b:2 ") == {"a": 1, "b": 2}

def test_parse_tokens_keeps_colons_in_the_token():
    # rsplit on the LAST colon — a token containing ':' must survive
    assert config._parse_tokens("aa:bb:7") == {"aa:bb": 7}

def test_parse_tokens_rejects_missing_colon():
    with pytest.raises(RuntimeError, match="token:user_id"):
        config._parse_tokens("justatoken")

def test_parse_tokens_rejects_non_numeric_user_id():
    with pytest.raises(RuntimeError, match="numeric id"):
        config._parse_tokens("secret:notanumber")

def test_parse_tokens_rejects_empty_token():
    with pytest.raises(RuntimeError, match="numeric id"):
        config._parse_tokens(":123")

def _reload(monkeypatch, **env):
    import importlib
    # config.py calls load_dotenv() at import time, so a reload would re-read
    # the developer's real .env and undo any delenv() the test just did --
    # making these tests pass or fail depending on whose machine they run on.
    # Stub it out so the reload sees only the env this test set up.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return importlib.reload(config)

def test_communication_plugins_defaults_to_discord(monkeypatch):
    monkeypatch.delenv("COMMUNICATION_PLUGINS", raising=False)
    monkeypatch.delenv("NOTIFY_VIA", raising=False)
    reloaded = _reload(monkeypatch)
    try:
        assert reloaded.COMMUNICATION_PLUGINS == ["discord"]
        assert reloaded.NOTIFY_VIA == "discord"
    finally:
        _reload(monkeypatch)

def test_communication_plugins_parses_list_and_notify_defaults_to_first(monkeypatch):
    monkeypatch.delenv("NOTIFY_VIA", raising=False)
    reloaded = _reload(monkeypatch, COMMUNICATION_PLUGINS="http, discord")
    try:
        assert reloaded.COMMUNICATION_PLUGINS == ["http", "discord"]
        # an http-only install must not silently route notifications at a
        # surface that was never enabled
        assert reloaded.NOTIFY_VIA == "http"
    finally:
        monkeypatch.delenv("COMMUNICATION_PLUGINS", raising=False)
        _reload(monkeypatch)

def test_notify_via_overrides_the_default(monkeypatch):
    reloaded = _reload(monkeypatch, COMMUNICATION_PLUGINS="http", NOTIFY_VIA="discord")
    try:
        assert reloaded.NOTIFY_VIA == "discord"
    finally:
        monkeypatch.delenv("COMMUNICATION_PLUGINS", raising=False)
        monkeypatch.delenv("NOTIFY_VIA", raising=False)
        _reload(monkeypatch)

def test_discord_token_is_optional(tmp_path):
    # The headline claim of the transport split: Wren must import and run with
    # no Discord account at all. This has to be a subprocess launched from a
    # directory with no .env — config calls load_dotenv(), so in-process the
    # repo's own .env would supply DISCORD_TOKEN and the test would pass
    # vacuously (or fail confusingly, which is how this was found).
    import subprocess, sys, pathlib
    repo = str(pathlib.Path(__file__).resolve().parent.parent)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": repo,
        "WREN_DB": str(tmp_path / "wren.db"),
        "COMMUNICATION_PLUGINS": "http",
        "WREN_TOKENS": "tok:1",
        "OWNER_ID": "1",
        "LLM_PROVIDERS": "lmstudio",
        "LMSTUDIO_BASE_URL": "http://test",
        "LMSTUDIO_MODEL": "test-model",
    }
    result = subprocess.run(
        [sys.executable, "-c",
         "from wren import config, core, run;"
         "print(repr(config.DISCORD_TOKEN), config.COMMUNICATION_PLUGINS, config.NOTIFY_VIA)"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, f"importing Wren without DISCORD_TOKEN failed:\n{result.stderr}"
    assert result.stdout.strip() == "'' ['http'] http"
