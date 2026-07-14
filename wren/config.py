import os
import zoneinfo
from dotenv import load_dotenv

from . import providers
from . import contacts

load_dotenv()

def _require(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Missing required env var: {key}. Copy .env.example to .env and fill it in.")
    return val

DISCORD_TOKEN = _require("DISCORD_TOKEN")

_provider_names = [p.strip() for p in os.environ.get("LLM_PROVIDERS", "").split(",") if p.strip()]
LLM_CHAIN = [c for c in (providers.resolve(name) for name in _provider_names) if c is not None]
if not LLM_CHAIN:
    raise RuntimeError(
        "No usable LLM providers configured. Set LLM_PROVIDERS in .env to a "
        "comma-separated list (e.g. lmstudio,openai) and set that provider's "
        "base_url/api_key/model env vars."
    )

def _build_whitelist(owner_raw: str) -> dict[str, int]:
    if not owner_raw.isdigit():
        raise RuntimeError("OWNER_ID must be a numeric Discord user ID.")
    return {"owner": int(owner_raw)}


_owner_raw = _require("OWNER_ID")

WHITELIST: dict[str, int] = _build_whitelist(_owner_raw)


def whitelist() -> dict[str, int]:
    return {**WHITELIST, **contacts.all()}


def id_to_name() -> dict[int, str]:
    return {v: k for k, v in whitelist().items()}


def _parse_email_watch(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        addr, name = pair.split(":", 1)
        result[addr.strip().lower()] = name.strip().lower()
    return result


IMAP_HOST = os.environ.get("IMAP_HOST", "")
IMAP_USER = os.environ.get("IMAP_USER", "")
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "")
EMAIL_POLL_SECONDS = int(os.environ.get("EMAIL_POLL_SECONDS", "60"))
EMAIL_WATCH: dict[str, str] = _parse_email_watch(os.environ.get("EMAIL_WATCH", ""))

REMINDER_POLL_SECONDS = int(os.environ.get("REMINDER_POLL_SECONDS", "30"))


def _validate_timezone(raw: str) -> str:
    raw = raw.strip()
    try:
        zoneinfo.ZoneInfo(raw)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        raise RuntimeError(f"TIMEZONE '{raw}' is not a valid IANA timezone name (e.g. America/Chicago).")
    return raw


TIMEZONE = _validate_timezone(os.environ.get("TIMEZONE", "UTC"))

SEARXNG_URL = os.environ.get("SEARXNG_URL", "")
FIRECRAWL_URL = os.environ.get("FIRECRAWL_URL", "")
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "")
