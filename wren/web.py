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
