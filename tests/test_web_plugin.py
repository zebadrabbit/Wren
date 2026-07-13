import os, asyncio
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import MagicMock, AsyncMock, patch
from wren import web_plugin, web, brain, config

RESULTS = [
    {"title": "A", "snippet": "sa", "url": "http://a"},
    {"title": "B", "snippet": "sb", "url": "http://b"},
    {"title": "C", "snippet": "sc", "url": "http://c"},
]

def _message():
    m = MagicMock()
    m.channel.send = AsyncMock()
    return m

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

def test_resolve_target_ordinal_not_matched_inside_larger_number():
    # "21st" must NOT be treated as "first"; 21 is out of range -> None
    assert web_plugin._resolve_target("read the 21st article", RESULTS) is None

def test_resolve_target_first_still_works():
    assert web_plugin._resolve_target("read the first one", RESULTS) == "http://a"

def test_web_search_stores_results_and_replies(monkeypatch):
    monkeypatch.setattr(config, "SEARXNG_URL", "http://searx.local/")
    web_plugin._last_results.clear()
    msg = _message()
    with patch.object(web, "search", return_value=RESULTS), \
         patch.object(brain, "summarize_web", return_value="Summary here."):
        asyncio.run(web_plugin.handle("web_search", msg, None, 1, "port strike", [], None, None))
    assert web_plugin._last_results[1] == RESULTS
    sent = msg.channel.send.await_args.args[0]
    assert "Summary here." in sent
    assert "http://a" in sent

def test_web_search_no_results(monkeypatch):
    monkeypatch.setattr(config, "SEARXNG_URL", "http://searx.local/")
    msg = _message()
    with patch.object(web, "search", return_value=[]):
        asyncio.run(web_plugin.handle("web_search", msg, None, 1, "asdf", [], None, None))
    msg.channel.send.assert_awaited_once_with("Couldn't find anything on that.")

def test_web_search_search_error(monkeypatch):
    monkeypatch.setattr(config, "SEARXNG_URL", "http://searx.local/")
    msg = _message()
    with patch.object(web, "search", side_effect=RuntimeError("down")):
        asyncio.run(web_plugin.handle("web_search", msg, None, 1, "q", [], None, None))
    msg.channel.send.assert_awaited_once_with("Search is unavailable right now.")

def test_read_page_uses_last_results(monkeypatch):
    monkeypatch.setattr(config, "SEARXNG_URL", "http://searx.local/")
    web_plugin._last_results[1] = RESULTS
    msg = _message()
    with patch.object(web, "scrape", return_value="# page") as mock_scrape, \
         patch.object(brain, "summarize_web", return_value="Page summary."):
        asyncio.run(web_plugin.handle("read_page", msg, None, 1, "read the second one", [], None, None))
    mock_scrape.assert_called_once_with("http://b")
    msg.channel.send.assert_awaited_once_with("Page summary.")

def test_read_page_no_target(monkeypatch):
    monkeypatch.setattr(config, "SEARXNG_URL", "http://searx.local/")
    web_plugin._last_results.clear()
    msg = _message()
    asyncio.run(web_plugin.handle("read_page", msg, None, 2, "read the first one", [], None, None))
    msg.channel.send.assert_awaited_once_with("Search for something first, or paste a link.")

def test_read_page_scrape_error(monkeypatch):
    monkeypatch.setattr(config, "SEARXNG_URL", "http://searx.local/")
    web_plugin._last_results[1] = RESULTS
    msg = _message()
    with patch.object(web, "scrape", side_effect=RuntimeError("boom")):
        asyncio.run(web_plugin.handle("read_page", msg, None, 1, "read it", [], None, None))
    msg.channel.send.assert_awaited_once_with("Couldn't fetch that page.")

def test_handle_disabled_when_unconfigured(monkeypatch):
    monkeypatch.setattr(config, "SEARXNG_URL", "")
    msg = _message()
    asyncio.run(web_plugin.handle("web_search", msg, None, 1, "q", [], None, None))
    msg.channel.send.assert_awaited_once_with("Web lookup isn't configured.")
