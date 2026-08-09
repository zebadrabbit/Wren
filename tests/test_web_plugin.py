import os, asyncio
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import patch
from wren.skills import web_skill as web_plugin
from wren.skills import web_search as web
from wren import brain, config
from wren.channel import Ctx, CollectingChannel

RESULTS = [
    {"title": "A", "snippet": "sa", "url": "http://a"},
    {"title": "B", "snippet": "sb", "url": "http://b"},
    {"title": "C", "snippet": "sc", "url": "http://c"},
]

def test_resolve_target_pasted_url():
    assert web_plugin._resolve_target("read http://x.com/page please", RESULTS) == "http://x.com/page"

def test_resolve_target_ordinal_word():
    assert web_plugin._resolve_target("read the second one", RESULTS) == "http://b"

def test_resolve_target_digit():
    assert web_plugin._resolve_target("more detail on #3", RESULTS) == "http://c"

def test_resolve_target_defaults_to_first():
    assert web_plugin._resolve_target("go deeper", RESULTS) == "http://a"

def test_resolve_target_none_without_results_or_url():
    assert web_plugin._resolve_target("read the first one", []) is None

def test_resolve_target_strips_trailing_period():
    assert web_plugin._resolve_target("read http://x.com/page.", RESULTS) == "http://x.com/page"

def test_resolve_target_strips_wrapping_paren():
    assert web_plugin._resolve_target("see (http://x.com/y)", RESULTS) == "http://x.com/y"

def test_resolve_target_keeps_balanced_parens_in_url():
    url = "read https://en.wikipedia.org/wiki/Python_(programming_language)"
    assert web_plugin._resolve_target(url, RESULTS) == "https://en.wikipedia.org/wiki/Python_(programming_language)"

def test_resolve_target_strips_unbalanced_trailing_paren():
    assert web_plugin._resolve_target("see (http://x.com/y)", RESULTS) == "http://x.com/y"

def test_resolve_target_ordinal_not_matched_inside_larger_number():
    # "21st" must NOT be treated as "first"; 21 is out of range -> None
    assert web_plugin._resolve_target("read the 21st article", RESULTS) is None

def test_resolve_target_first_still_works():
    assert web_plugin._resolve_target("read the first one", RESULTS) == "http://a"

def test_web_search_stores_results_and_replies(monkeypatch):
    monkeypatch.setattr(config, "SEARXNG_URL", "http://searx.local/")
    web_plugin._last_results.clear()
    ch = CollectingChannel()
    with patch.object(web, "search", return_value=RESULTS), \
         patch.object(brain, "summarize_web", return_value="Summary here."):
        asyncio.run(web_plugin.handle("web_search", Ctx(user_id=1, channel=ch, content="port strike")))
    assert web_plugin._last_results[1] == RESULTS
    sent = ch.sent[0]
    assert "Summary here." in sent
    assert "http://a" in sent

def test_web_search_no_results(monkeypatch):
    monkeypatch.setattr(config, "SEARXNG_URL", "http://searx.local/")
    ch = CollectingChannel()
    with patch.object(web, "search", return_value=[]):
        asyncio.run(web_plugin.handle("web_search", Ctx(user_id=1, channel=ch, content="asdf")))
    assert ch.sent == ["Couldn't find anything on that."]

def test_web_search_search_error(monkeypatch):
    monkeypatch.setattr(config, "SEARXNG_URL", "http://searx.local/")
    ch = CollectingChannel()
    with patch.object(web, "search", side_effect=RuntimeError("down")):
        asyncio.run(web_plugin.handle("web_search", Ctx(user_id=1, channel=ch, content="q")))
    assert ch.sent == ["Search is unavailable right now."]

def test_read_page_uses_last_results(monkeypatch):
    monkeypatch.setattr(config, "SEARXNG_URL", "http://searx.local/")
    web_plugin._last_results[1] = RESULTS
    ch = CollectingChannel()
    with patch.object(web, "scrape", return_value="# page") as mock_scrape, \
         patch.object(brain, "summarize_web", return_value="Page summary."):
        asyncio.run(web_plugin.handle("read_page", Ctx(user_id=1, channel=ch, content="read the second one")))
    mock_scrape.assert_called_once_with("http://b")
    assert ch.sent == ["Page summary."]

def test_read_page_no_target(monkeypatch):
    monkeypatch.setattr(config, "SEARXNG_URL", "http://searx.local/")
    web_plugin._last_results.clear()
    ch = CollectingChannel()
    asyncio.run(web_plugin.handle("read_page", Ctx(user_id=2, channel=ch, content="read the first one")))
    assert ch.sent == ["Search for something first, or paste a link."]

def test_read_page_scrape_error(monkeypatch):
    monkeypatch.setattr(config, "SEARXNG_URL", "http://searx.local/")
    web_plugin._last_results[1] = RESULTS
    ch = CollectingChannel()
    with patch.object(web, "scrape", side_effect=RuntimeError("boom")):
        asyncio.run(web_plugin.handle("read_page", Ctx(user_id=1, channel=ch, content="read it")))
    assert ch.sent == ["Couldn't fetch that page."]

def test_handle_disabled_when_unconfigured(monkeypatch):
    monkeypatch.setattr(config, "SEARXNG_URL", "")
    ch = CollectingChannel()
    asyncio.run(web_plugin.handle("web_search", Ctx(user_id=1, channel=ch, content="q")))
    assert ch.sent == ["Web lookup isn't configured."]
