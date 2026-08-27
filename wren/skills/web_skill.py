import asyncio
import logging
import re
from . import web_search as web
from .. import brain
from .. import config
from ..channel import Ctx

INTENTS = ["web_search", "read_page"]
PLUGIN_NAME = "Web Lookup"

PROMPT_GUIDELINES = """- web_search: user wants current/live/real-world information — news, prices, "what's happening with X", "look up X", "search for X"; distinct from chat which handles timeless questions and casual conversation
- read_page: user wants Wren to read a specific web page in full — a pasted URL, or a follow-up like "read me the first one"/"more detail on #2" after a search"""

def is_active() -> bool:
    return bool(config.SEARXNG_URL)


def inactive_reason() -> str:
    return "SEARXNG_URL is not set — web search and page reading are disabled."

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

def _clean_url(url: str) -> str:
    url = url.rstrip(".,;:!?\"'")
    # strip a trailing bracket only when it's unbalanced — keeps
    # ".../Python_(programming_language)" intact while cleaning "(http://x)"
    for close, open_ in ((")", "("), ("]", "["), ("}", "{")):
        while url.endswith(close) and url.count(close) > url.count(open_):
            url = url[:-1]
    return url

def _resolve_target(content: str, results: list[dict]) -> str | None:
    m = _URL_RE.search(content)
    if m:
        return _clean_url(m.group(0))
    if not results:
        return None
    low = content.lower()
    for word, idx in _ORDINALS.items():
        if re.search(rf"\b{word}\b", low):
            return results[idx]["url"] if idx < len(results) else None
    m = re.search(r"#?(\d+)", content)
    if m:
        idx = int(m.group(1)) - 1
        return results[idx]["url"] if 0 <= idx < len(results) else None
    return results[0]["url"]  # no explicit reference -> default to top hit

async def handle(intent: str, ctx: Ctx) -> None:
    if not config.SEARXNG_URL:
        await ctx.channel.send("Web lookup isn't configured.")
        return

    if intent == "web_search":
        try:
            results = await asyncio.to_thread(web.search, ctx.content)
        except Exception as e:
            logging.warning(f"web search failed: {e}")
            await ctx.channel.send("Search is unavailable right now.")
            return
        if not results:
            await ctx.channel.send("Couldn't find anything on that.")
            return
        _last_results[ctx.user_id] = results
        summary = await asyncio.to_thread(brain.summarize_web, ctx.content, results)
        links = "\n".join(f"{i+1}. {r['title']} — {r['url']}" for i, r in enumerate(results))
        await ctx.channel.send(f"{summary}\n\n{links}")

    elif intent == "read_page":
        url = _resolve_target(ctx.content, _last_results.get(ctx.user_id, []))
        if not url:
            await ctx.channel.send("Search for something first, or paste a link.")
            return
        try:
            markdown = await asyncio.to_thread(web.scrape, url)
        except Exception as e:
            logging.warning(f"scrape failed: {e}")
            await ctx.channel.send("Couldn't fetch that page.")
            return
        summary = await asyncio.to_thread(brain.summarize_web, ctx.content or url, markdown)
        await ctx.channel.send(summary)
