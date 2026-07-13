# Web-lookup Capability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Wren a live web-lookup capability — search via local SearXNG, read full pages via Firecrawl, summarized in Wren's terse voice.

**Architecture:** One new IO module (`wren/web.py`, sync `search`/`scrape`), one new plugin (`wren/web_plugin.py`, intents `web_search`/`read_page`) following the existing plugin contract, and a `brain.summarize_web` helper reusing `_complete`. Network calls run via `asyncio.to_thread` so they don't block the event loop.

**Tech Stack:** Python 3.12, httpx (already pulled in by `openai`), pytest, discord.py.

## Global Constraints

- Plugin contract: a plugin module exposes `INTENTS: list[str]`, `PROMPT_GUIDELINES: str`, and `async def handle(intent, message, client, user_id, content, tags, person, when)`. Optional `async def start(client)`.
- Optional-feature pattern: the feature is inert unless `SEARXNG_URL` is set (mirrors `EMAIL_WATCH`).
- Tests run offline — mock httpx at the boundary. Never touch the real `wren.db` or hit a real network.
- Tests set required env vars via `os.environ.setdefault(...)` at the top of the file, before importing `wren` modules (see existing test files).
- Wren's persona in LLM prompts: "Short, structured, ready. No filler. /no_think". Summaries must not invent facts beyond the provided web content.
- Commit after each task.

---

### Task 1: Config + dependency

**Files:**
- Modify: `wren/config.py` (append after the TIMEZONE block, ~line 74)
- Modify: `.env.example`
- Modify: `requirements.txt`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `config.SEARXNG_URL: str`, `config.FIRECRAWL_URL: str`, `config.FIRECRAWL_API_KEY: str` (all default `""`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_web_lookup_defaults_empty(monkeypatch):
    # unset -> empty strings (feature disabled)
    import importlib
    from wren import config as cfg
    assert isinstance(cfg.SEARXNG_URL, str)
    assert isinstance(cfg.FIRECRAWL_URL, str)
    assert isinstance(cfg.FIRECRAWL_API_KEY, str)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/pytest tests/test_config.py::test_web_lookup_defaults_empty -v`
Expected: FAIL with `AttributeError: module 'wren.config' has no attribute 'SEARXNG_URL'`

- [ ] **Step 3: Add config settings**

Append to `wren/config.py`:

```python
SEARXNG_URL = os.environ.get("SEARXNG_URL", "")
FIRECRAWL_URL = os.environ.get("FIRECRAWL_URL", "")
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/pytest tests/test_config.py::test_web_lookup_defaults_empty -v`
Expected: PASS

- [ ] **Step 5: Update `.env.example`**

Append:

```
# Web lookup (optional — leave SEARXNG_URL unset to disable)
# SEARXNG_URL=http://192.168.1.153/searxng/
# FIRECRAWL_URL=http://localhost:3002
# FIRECRAWL_API_KEY=          # only if your Firecrawl container requires auth
```

- [ ] **Step 6: Add httpx to `requirements.txt`**

Add a line `httpx` (already a transitive dep of `openai`; pinning it explicitly makes the direct use intentional).

- [ ] **Step 7: Commit**

```bash
git add wren/config.py .env.example requirements.txt tests/test_config.py
git commit -m "feat: add SearXNG/Firecrawl config for web lookup"
```

---

### Task 2: `wren/web.py` — SearXNG search + Firecrawl scrape

**Files:**
- Create: `wren/web.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `config.SEARXNG_URL`, `config.FIRECRAWL_URL`, `config.FIRECRAWL_API_KEY`.
- Produces:
  - `search(query: str, limit: int = 5) -> list[dict]` — each dict `{"title": str, "snippet": str, "url": str}`.
  - `scrape(url: str) -> str` — page markdown.
  - Module attribute `httpx` (so tests can `monkeypatch.setattr(web.httpx, "get"/"post", ...)`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_web.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/pytest tests/test_web.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'wren.web'`

- [ ] **Step 3: Implement `wren/web.py`**

```python
import httpx
from . import config

SEARCH_TIMEOUT = 10.0
SCRAPE_TIMEOUT = 30.0

def search(query: str, limit: int = 5) -> list[dict]:
    if not config.SEARXNG_URL:
        raise RuntimeError("SEARXNG_URL is not configured.")
    url = config.SEARXNG_URL.rstrip("/") + "/search"
    resp = httpx.get(url, params={"q": query, "format": "json"}, timeout=SEARCH_TIMEOUT)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return [
        {"title": r.get("title", ""), "snippet": r.get("content", ""), "url": r.get("url", "")}
        for r in results[:limit]
    ]

def scrape(url: str) -> str:
    if not config.FIRECRAWL_URL:
        raise RuntimeError("FIRECRAWL_URL is not configured.")
    endpoint = config.FIRECRAWL_URL.rstrip("/") + "/v1/scrape"
    headers = {}
    if config.FIRECRAWL_API_KEY:
        headers["Authorization"] = f"Bearer {config.FIRECRAWL_API_KEY}"
    resp = httpx.post(
        endpoint,
        json={"url": url, "formats": ["markdown"]},
        headers=headers,
        timeout=SCRAPE_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("data", {}).get("markdown", "")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/pytest tests/test_web.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add wren/web.py tests/test_web.py
git commit -m "feat: add web.search (SearXNG) and web.scrape (Firecrawl)"
```

---

### Task 3: `brain.summarize_web` helper

**Files:**
- Modify: `wren/brain.py` (add function after `expand`, ~line 147)
- Test: `tests/test_brain.py`

**Interfaces:**
- Consumes: `brain._complete` (existing).
- Produces: `summarize_web(query: str, content) -> str`. `content` is either a `list[dict]` of `{title, snippet, url}` (search path) or a `str` of markdown (scrape path).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_brain.py`:

```python
def test_summarize_web_from_results_returns_string():
    results = [{"title": "T", "snippet": "it is sunny", "url": "http://a"}]
    with patch.object(brain, "_get_client", return_value=_client_returning("It's sunny.")):
        out = brain.summarize_web("weather", results)
    assert isinstance(out, str) and len(out) > 0

def test_summarize_web_from_markdown_returns_string():
    with patch.object(brain, "_get_client", return_value=_client_returning("Summary.")):
        out = brain.summarize_web("read it", "# Article\nlong body text")
    assert isinstance(out, str) and len(out) > 0

def test_summarize_web_puts_content_in_prompt():
    results = [{"title": "Port news", "snippet": "strike ended", "url": "http://a"}]
    captured = {}
    def fake_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        return _mock_completion("ok")
    client = MagicMock()
    client.chat.completions.create.side_effect = fake_create
    with patch.object(brain, "_get_client", return_value=client):
        brain.summarize_web("port strike", results)
    user_msg = captured["messages"][1]["content"]
    assert "strike ended" in user_msg
    assert "port strike" in user_msg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/pytest tests/test_brain.py -k summarize_web -v`
Expected: FAIL with `AttributeError: module 'wren.brain' has no attribute 'summarize_web'`

- [ ] **Step 3: Implement `summarize_web`**

Add to `wren/brain.py`:

```python
def summarize_web(query: str, content) -> str:
    if isinstance(content, str):
        context = content
    else:
        context = "\n".join(
            f"- {r['title']}: {r['snippet']} ({r['url']})" for r in content
        )
    prompt = (
        f"Web results for '{query}':\n{context}\n\n"
        "Answer the user's query using only these results. Cite sources by "
        "title when useful. If the results don't answer it, say so plainly."
    )
    return _complete(
        [
            {"role": "system", "content": "You are Wren. Short, structured, ready. No filler. Answer directly using ONLY the provided web content — do not invent facts. No reasoning shown. /no_think"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        max_tokens=500,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/pytest tests/test_brain.py -k summarize_web -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add wren/brain.py tests/test_brain.py
git commit -m "feat: add brain.summarize_web for grounding replies in web content"
```

---

### Task 4: `wren/web_plugin.py` — the plugin

**Files:**
- Create: `wren/web_plugin.py`
- Test: `tests/test_web_plugin.py`

**Interfaces:**
- Consumes: `web.search`, `web.scrape`, `brain.summarize_web`, `config.SEARXNG_URL`.
- Produces: `INTENTS`, `PROMPT_GUIDELINES`, `async handle(...)`, `_resolve_target(content, results) -> str | None`, `_last_results: dict[int, list[dict]]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_web_plugin.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/pytest tests/test_web_plugin.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'wren.web_plugin'`

- [ ] **Step 3: Implement `wren/web_plugin.py`**

```python
import asyncio
import logging
import re
from . import web
from . import brain
from . import config

INTENTS = ["web_search", "read_page"]

PROMPT_GUIDELINES = """- web_search: user wants current/live/real-world information — news, weather, prices, "what's happening with X", "look up X", "search for X"; distinct from chat which handles timeless questions and casual conversation
- read_page: user wants Wren to read a specific web page in full — a pasted URL, or a follow-up like "read me the first one"/"more detail on #2" after a search"""

_last_results: dict[int, list[dict]] = {}
# ponytail: in-memory, lost on restart — fine for "read the first one"
# follow-ups. Persist only if that limitation actually bites.

_URL_RE = re.compile(r"https?://\S+")
_ORDINALS = {
    "first": 0, "1st": 0,
    "second": 1, "2nd": 1,
    "third": 2, "3rd": 2,
    "fourth": 3, "4th": 3,
    "fifth": 4, "5th": 4,
}

def _resolve_target(content: str, results: list[dict]) -> str | None:
    m = _URL_RE.search(content)
    if m:
        return m.group(0)
    if not results:
        return None
    low = content.lower()
    for word, idx in _ORDINALS.items():
        if word in low:
            return results[idx]["url"] if idx < len(results) else None
    m = re.search(r"#?(\d+)", content)
    if m:
        idx = int(m.group(1)) - 1
        return results[idx]["url"] if 0 <= idx < len(results) else None
    return results[0]["url"]  # no explicit reference -> default to top hit

async def handle(intent, message, client, user_id, content, tags, person, when):
    if not config.SEARXNG_URL:
        await message.channel.send("Web lookup isn't configured.")
        return

    if intent == "web_search":
        try:
            results = await asyncio.to_thread(web.search, content)
        except Exception as e:
            logging.warning(f"web search failed: {e}")
            await message.channel.send("Search is unavailable right now.")
            return
        if not results:
            await message.channel.send("Couldn't find anything on that.")
            return
        _last_results[user_id] = results
        summary = await asyncio.to_thread(brain.summarize_web, content, results)
        links = "\n".join(f"{i+1}. {r['title']} — {r['url']}" for i, r in enumerate(results))
        await message.channel.send(f"{summary}\n\n{links}")

    elif intent == "read_page":
        url = _resolve_target(content, _last_results.get(user_id, []))
        if not url:
            await message.channel.send("Search for something first, or paste a link.")
            return
        try:
            markdown = await asyncio.to_thread(web.scrape, url)
        except Exception as e:
            logging.warning(f"scrape failed: {e}")
            await message.channel.send("Couldn't fetch that page.")
            return
        summary = await asyncio.to_thread(brain.summarize_web, content or url, markdown)
        await message.channel.send(summary)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/pytest tests/test_web_plugin.py -v`
Expected: PASS (12 tests)

- [ ] **Step 5: Commit**

```bash
git add wren/web_plugin.py tests/test_web_plugin.py
git commit -m "feat: add web_plugin with web_search and read_page intents"
```

---

### Task 5: Register plugin + docs

**Files:**
- Modify: `wren/plugins.py` (lines 1-7)
- Modify: `wren/bot.py` (HELP_TEXT, ~line 43)
- Modify: `README.md`
- Test: `tests/test_plugins.py`

**Interfaces:**
- Consumes: `web_plugin.INTENTS` (`["web_search", "read_page"]`).
- Produces: `web_search` and `read_page` routed via `plugins.INTENT_HANDLERS`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_plugins.py`:

```python
def test_web_intents_registered():
    from wren import plugins, web_plugin
    for intent in ("web_search", "read_page"):
        assert intent in plugins.all_intents()
        assert plugins.INTENT_HANDLERS[intent] is web_plugin
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/pytest tests/test_plugins.py::test_web_intents_registered -v`
Expected: FAIL (`web_search` not in `all_intents()`, or KeyError)

- [ ] **Step 3: Register the plugin**

In `wren/plugins.py`, add the import and list entry:

```python
from . import reminder_plugin
from . import web_plugin

PLUGINS = [notes_plugin, shopping_plugin, email_plugin, reminder_plugin, web_plugin]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/pytest tests/test_plugins.py::test_web_intents_registered -v`
Expected: PASS

- [ ] **Step 5: Update HELP_TEXT**

In `wren/bot.py`, add a section to `HELP_TEXT` before the closing "Anything else..." line:

```
**Web** (when configured)
- "what's the weather in Chicago tomorrow" / "any news on the port strike" — searches the web and summarizes with links
- "read me the first one" / "read https://…" — fetches a page in full and summarizes
```

- [ ] **Step 6: Update README**

In `README.md`, add a bullet to the capabilities intro (the paragraph near the top listing what Wren does) mentioning web search + page reading, and a short "Web lookup" subsection noting it needs `SEARXNG_URL` (and optionally `FIRECRAWL_URL`) set, pointing at `.env.example`.

- [ ] **Step 7: Run full suite**

Run: `venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add wren/plugins.py wren/bot.py README.md tests/test_plugins.py
git commit -m "feat: register web plugin and document web lookup"
```

---

## Self-Review

- **Spec coverage:** web.py search+scrape (T2), summarize (T3), plugin w/ both intents + ordinal resolution + in-memory state + error handling (T4), config/optional-feature gate (T1, T4 disabled test), registration + docs + help (T5), httpx dep (T1). Weather-as-search: no special code, covered by web_search. All spec sections map to a task.
- **Placeholders:** none — all steps carry real code/commands.
- **Type consistency:** `search`→`list[dict]{title,snippet,url}` consumed identically in `summarize_web` and `web_plugin`; `_resolve_target` returns `str | None`; `handle` signature matches the plugin contract used by `bot.py`.
