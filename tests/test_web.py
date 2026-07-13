import os
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import MagicMock
import pytest
from wren import web, config

def _resp(json_body):
    r = MagicMock()
    r.json.return_value = json_body
    r.raise_for_status.return_value = None
    return r

def test_search_parses_and_maps_fields(monkeypatch):
    monkeypatch.setattr(config, "SEARXNG_URL", "http://searx.local/")
    body = {"results": [
        {"title": "T1", "content": "snip1", "url": "http://a"},
        {"title": "T2", "content": "snip2", "url": "http://b"},
    ]}
    monkeypatch.setattr(web.httpx, "get", lambda *a, **k: _resp(body))
    out = web.search("weather")
    assert out == [
        {"title": "T1", "snippet": "snip1", "url": "http://a"},
        {"title": "T2", "snippet": "snip2", "url": "http://b"},
    ]

def test_search_caps_at_limit(monkeypatch):
    monkeypatch.setattr(config, "SEARXNG_URL", "http://searx.local/")
    body = {"results": [{"title": f"T{i}", "content": "s", "url": f"http://{i}"} for i in range(10)]}
    monkeypatch.setattr(web.httpx, "get", lambda *a, **k: _resp(body))
    assert len(web.search("q")) == 5

def test_search_empty_results(monkeypatch):
    monkeypatch.setattr(config, "SEARXNG_URL", "http://searx.local/")
    monkeypatch.setattr(web.httpx, "get", lambda *a, **k: _resp({"results": []}))
    assert web.search("q") == []

def test_search_raises_when_unconfigured(monkeypatch):
    monkeypatch.setattr(config, "SEARXNG_URL", "")
    with pytest.raises(RuntimeError):
        web.search("q")

def test_scrape_returns_markdown_and_sends_no_auth_by_default(monkeypatch):
    monkeypatch.setattr(config, "FIRECRAWL_URL", "http://fc.local")
    monkeypatch.setattr(config, "FIRECRAWL_API_KEY", "")
    captured = {}
    def fake_post(endpoint, json=None, headers=None, timeout=None):
        captured["endpoint"] = endpoint
        captured["json"] = json
        captured["headers"] = headers
        return _resp({"data": {"markdown": "# Hello"}})
    monkeypatch.setattr(web.httpx, "post", fake_post)
    md = web.scrape("http://example.com")
    assert md == "# Hello"
    assert captured["endpoint"] == "http://fc.local/v1/scrape"
    assert captured["json"] == {"url": "http://example.com", "formats": ["markdown"]}
    assert "Authorization" not in (captured["headers"] or {})

def test_scrape_sends_bearer_when_key_set(monkeypatch):
    monkeypatch.setattr(config, "FIRECRAWL_URL", "http://fc.local")
    monkeypatch.setattr(config, "FIRECRAWL_API_KEY", "fc-secret")
    captured = {}
    def fake_post(endpoint, json=None, headers=None, timeout=None):
        captured["headers"] = headers
        return _resp({"data": {"markdown": "x"}})
    monkeypatch.setattr(web.httpx, "post", fake_post)
    web.scrape("http://example.com")
    assert captured["headers"]["Authorization"] == "Bearer fc-secret"
