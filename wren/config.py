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

# optional: Wren is a general bot now. A self-hoster running COMMUNICATION_PLUGINS=http
# has no Discord account at all, so requiring this at import would stop Wren
# from starting.
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")

COMMUNICATION_PLUGINS = [s.strip() for s in os.environ.get("COMMUNICATION_PLUGINS", "discord").split(",") if s.strip()]

# where unprompted messages (reminders, watcher alerts) go. Defaults to the
# first enabled surface rather than a hardcoded "discord", so an http-only
# install routes somewhere real without extra config.
NOTIFY_VIA = os.environ.get("NOTIFY_VIA", "").strip() or (COMMUNICATION_PLUGINS[0] if COMMUNICATION_PLUGINS else "discord")


def _parse_tokens(raw: str) -> dict[str, int]:
    """WREN_TOKENS=secret:412341234123,other:998877665544"""
    result: dict[str, int] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            raise RuntimeError(f"WREN_TOKENS entry '{pair}' must be token:user_id.")
        # rsplit, not split: the user id is the last field, so a token that
        # happens to contain a colon still parses
        token, user_id = pair.rsplit(":", 1)
        token, user_id = token.strip(), user_id.strip()
        if not token or not user_id.isdigit():
            raise RuntimeError(f"WREN_TOKENS entry '{pair}' must be token:user_id with a numeric id.")
        result[token] = int(user_id)
    return result


WREN_TOKENS: dict[str, int] = _parse_tokens(os.environ.get("WREN_TOKENS", ""))

WREN_HTTP_HOST = os.environ.get("WREN_HTTP_HOST", "127.0.0.1")
WREN_HTTP_PORT = int(os.environ.get("WREN_HTTP_PORT", "8787"))
WREN_STT_MODEL = os.environ.get("WREN_STT_MODEL", "base.en")
# tuning knobs — accuracy/speed depends on the actual box. "auto" picks CUDA
# when it's there, and int8 keeps a CPU-only host usable.
WREN_STT_DEVICE = os.environ.get("WREN_STT_DEVICE", "auto")
WREN_STT_COMPUTE = os.environ.get("WREN_STT_COMPUTE", "int8")

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


def _parse_github_watch(raw: str) -> list[str]:
    return [r.strip() for r in raw.split(",") if r.strip()]


GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_WATCH: list[str] = _parse_github_watch(os.environ.get("GITHUB_WATCH", ""))
GITHUB_POLL_SECONDS = int(os.environ.get("GITHUB_POLL_SECONDS", "60"))

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
