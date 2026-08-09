# Web-lookup capability for Wren

**Date:** 2026-07-12
**Status:** Approved, pending implementation plan

## Problem

Wren's `chat` intent answers from the LLM's frozen training knowledge. It has
no access to *live* information — today's news, current weather, "what's
happening with X right now". The household runs a local SearXNG instance and a
Firecrawl container that can fill exactly this gap.

## Goal

Give Wren one general "look it up on the live web" capability. The user asks
naturally ("what's the weather in Chicago tomorrow", "any news on the port
strike", "look up X") and Wren searches SearXNG, summarizes the results in its
usual terse voice with source links, and can fetch a full page via Firecrawl
when asked to go deeper.

News and weather are not separate features — they are two flavors of the same
web-lookup intent. No dedicated weather/news commands.

## Non-goals

- No structured weather API (open-meteo etc.) in v1. Weather goes through the
  same SearXNG search. If snippet-based weather proves too thin later, add
  open-meteo (free, no key) then — marked with a `ponytail:` comment.
- No persistent search history / DB. Follow-up state ("the first one") is
  in-memory only and does not survive a restart.
- No new external dependency beyond `httpx`, which `openai` already pulls in.

## Architecture

Follows the existing plugin contract exactly (`INTENTS`, `PROMPT_GUIDELINES`,
`handle(...)`, optional `start()`), mirroring `notes_plugin.py` /
`email_plugin.py`.

### New module: `wren/web.py`

Isolates all HTTP/IO so it can be unit-tested without a network. Two thin
**synchronous** functions:

- `search(query: str) -> list[dict]`
  Calls SearXNG's JSON API: `GET {SEARXNG_URL}/search?q=<query>&format=json`.
  Returns up to 5 results as `{"title", "snippet", "url"}` dicts (mapping
  SearXNG's `title` / `content` / `url` fields). Empty list if no results.

- `scrape(url: str) -> str`
  Calls Firecrawl: `POST {FIRECRAWL_URL}/v1/scrape` with `{"url": url,
  "formats": ["markdown"]}`, optional `Authorization: Bearer <key>` header when
  `FIRECRAWL_API_KEY` is set. Returns the page markdown string. Raises on
  failure (handler catches).

Both use `httpx` with a sensible timeout (search ~10s, scrape ~30s).

### New plugin: `wren/web_plugin.py`

```
INTENTS = ["web_search", "read_page"]
```

In-memory follow-up state, keyed by Discord user id:

```python
_last_results: dict[int, list[dict]] = {}
# ponytail: in-memory, lost on restart — fine for "read the first one"
# follow-ups. Persist only if that limitation actually bites.
```

**`web_search` handler:**
1. `results = await asyncio.to_thread(web.search, content)` (keeps a slow
   request off the event loop).
2. If empty → "Couldn't find anything on that." and return.
3. Store `_last_results[user_id] = results`.
4. `summary = brain.summarize_web(content, results)` — LLM writes a short
   answer grounded in the snippets.
5. Send the summary, then the source links (numbered, so "the first one" maps
   to a result).

**`read_page` handler** — resolve the target URL, in priority order:
1. A URL present in the message `content` → use it directly.
2. An ordinal reference ("first", "second", "#2", "1") → index into
   `_last_results[user_id]`.
3. Otherwise → default to `_last_results[user_id][0]` (the last search's top
   hit).
If no target can be resolved (no URL, no prior search) → ask the user to search
first or paste a link. Then `markdown = await asyncio.to_thread(web.scrape,
url)`, summarize with `brain.summarize_web(content, ...)` using the full text,
and send.

### `brain.py` addition

`summarize_web(query: str, results_or_text) -> str` — a small helper reusing
`_complete()`. System prompt keeps Wren's established terse persona ("Short,
structured, ready. No filler. /no_think"), instructs it to answer the query
using only the provided web content and not invent facts. Accepts either the
list of snippet dicts (search path) or a single markdown string (scrape path).

### `brain.py` classifier prompt

Register `web_search` / `read_page` guidelines via the existing
`PROMPT_GUIDELINES` mechanism so `detect_intent` routes correctly:

- `web_search`: user wants current / live / real-world information — news,
  weather, prices, "what's happening with X", "look up X", "search for X".
  Distinct from `chat`, which handles timeless questions the model already
  knows and casual conversation.
- `read_page`: user wants Wren to read a specific web page in full — a pasted
  URL, or a follow-up like "read me the first one" / "more detail on #2" after
  a search.

### Config (`wren/config.py` + `.env.example`)

Mirrors the `EMAIL_WATCH` optional-feature pattern — the plugin is inert unless
`SEARXNG_URL` is set.

```
SEARXNG_URL = os.environ.get("SEARXNG_URL", "")          # e.g. http://192.168.1.153/searxng/
FIRECRAWL_URL = os.environ.get("FIRECRAWL_URL", "")      # e.g. http://localhost:3002
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "")  # optional
```

`web.search` / `web.scrape` raise a clear error if their URL is unset; the
plugin's `handle` guards on `config.SEARXNG_URL` and tells the user the feature
isn't configured rather than erroring.

## Error handling

- SearXNG unreachable / bad status → log warning, reply "Search is
  unavailable right now."
- No results → "Couldn't find anything on that."
- Firecrawl unreachable / bad status → log warning, reply "Couldn't fetch that
  page."
- `read_page` with no resolvable target → "Search for something first, or paste
  a link."

All network errors are caught in the handler; a failure never crashes the bot
or leaks a stack trace to the user.

## Testing

`tests/test_web.py`, following existing test style (no framework beyond
pytest, no live network):

- `search` parses a mocked SearXNG JSON body into the expected
  `{title, snippet, url}` shape and caps at 5.
- `search` returns `[]` on an empty results body.
- `scrape` posts the right payload and returns the markdown from a mocked
  Firecrawl response; includes the Bearer header only when a key is set.
- Ordinal resolution in `read_page`: "first" / "#2" / a pasted URL / no prior
  search each resolve to the correct target (or the correct "search first"
  fallback).

Mock `httpx` at the boundary (e.g. `monkeypatch` the client call) so tests run
offline.

## Docs

- `.env.example`: add the three new vars with comments, in an optional block.
- `README.md`: one line under capabilities — Wren can search the web (SearXNG)
  and read pages (Firecrawl) when configured.
- `HELP_TEXT`: mention web search / reading a link.

## Files touched

- new `wren/web.py`
- new `wren/web_plugin.py`
- new `tests/test_web.py`
- `wren/plugins.py` — register `web_plugin` in `PLUGINS`
- `wren/brain.py` — `summarize_web` helper
- `wren/config.py` — three new settings
- `.env.example`, `README.md`, help text — docs
- `requirements.txt` — add `httpx` explicitly
