import logging
import os
import zoneinfo
from typing import Callable, NamedTuple
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

# same story as DISCORD_TOKEN: only needed when COMMUNICATION_PLUGINS includes
# 'telegram', and telegram_plugin.start() is what refuses to run without it.
# No poll-interval knob to go with it — the Telegram plugin long-polls, so its
# only timing constant is how long the API holds the request open, which is
# not a per-install choice (see _LONG_POLL_SECONDS there).
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")

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


def reload_llm_chain() -> None:
    """Re-resolve LLM_CHAIN from the current environment.

    Deliberately re-runs the same expression import does, so a runtime model
    switch and a restart cannot diverge.

    Refuses a rebuild that would leave no usable provider: an empty chain at
    boot is a loud startup error, but an empty chain at runtime would mean Wren
    silently stops being able to answer because someone touched a dropdown.
    """
    global LLM_CHAIN
    rebuilt = [c for c in (providers.resolve(n) for n in _provider_names) if c is not None]
    if not rebuilt:
        raise RuntimeError(
            "that change would leave no usable LLM provider; keeping the current one"
        )
    LLM_CHAIN = rebuilt


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


def _serialize_email_watch(value: dict[str, str]) -> str:
    """Inverse of _parse_email_watch, so a value handed back to it round-trips."""
    return ",".join(f"{addr}:{name}" for addr, name in value.items())


IMAP_HOST = os.environ.get("IMAP_HOST", "")
IMAP_USER = os.environ.get("IMAP_USER", "")
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "")
EMAIL_POLL_SECONDS = int(os.environ.get("EMAIL_POLL_SECONDS", "60"))
EMAIL_WATCH: dict[str, str] = _parse_email_watch(os.environ.get("EMAIL_WATCH", ""))


def _parse_github_watch(raw: str) -> list[str]:
    return [r.strip() for r in raw.split(",") if r.strip()]


def _serialize_github_watch(value: list[str]) -> str:
    """Inverse of _parse_github_watch, so a value handed back to it round-trips."""
    return ",".join(value)


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

# providers.py and reload_llm_chain() read these from os.environ directly,
# never from the module attribute (see _apply_model) -- the attribute exists
# purely so these five behave like every other settable for
# serialize_setting()/GET /api/plugins instead of needing their own
# special-cased read path. _apply_model keeps the two in step.
OLLAMA_MODEL     = os.environ.get("OLLAMA_MODEL", "")
LMSTUDIO_MODEL   = os.environ.get("LMSTUDIO_MODEL", "")
OPENAI_MODEL     = os.environ.get("OPENAI_MODEL", "")
CLAUDE_MODEL     = os.environ.get("CLAUDE_MODEL", "")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "")

# ── Runtime-editable settings ────────────────────────────────────────────────
# Everything above is read from .env at import. These may additionally be
# overridden at runtime from the settings table, by the owner, through the web
# chat's plugins panel.
#
# SETTABLE is a POSITIVE ALLOWLIST and that is the whole security model: no
# request can reach a name that is not in here, so DISCORD_TOKEN and friends
# are unreachable by construction. GET /api/plugins returns these VALUES, so
# adding a credential to this dict would leak it. Do not.


def _coerce_poll_seconds(raw: str) -> int:
    value = int(raw)
    if value < 5:
        # A 0 spins the poll loop as fast as the CPU allows, and hammers IMAP
        # and GitHub's rate limit with it.
        raise ValueError("poll interval must be at least 5 seconds")
    return value


def _coerce_notify_via(raw: str) -> str:
    from . import router          # local: router imports config, so not at module level

    name = raw.strip()
    surface = router.surface(name)
    if surface is None:
        known = ", ".join(router.registered()) or "none running"
        raise ValueError(f"'{name}' is not a running communication plugin ({known})")
    if not getattr(surface, "CAN_NOTIFY", True):
        raise ValueError(f"'{name}' is send-only and cannot deliver unprompted messages")
    return name


def _apply_attr(key: str, value) -> None:
    """Default apply: assign onto this module. Works because every ordinary
    consumer reads config.X at call time."""
    globals()[key] = value


def _apply_model(key: str, value) -> None:
    """Model names must land in two places that cannot be allowed to drift:
    os.environ, which is the one providers.resolve() actually reads, and the
    module attribute, kept in step purely so these keys serialize and display
    like every other settable. Setting only the attribute would do nothing --
    providers.py never looks at it.

    Writes both plus the rebuild as one unit: if reload_llm_chain() refuses
    (empty chain), both are put back exactly as they were first. Without this,
    a refused clear_override -- or a refused stored setting at boot, which
    apply_overrides() only logs and moves past -- would leave os.environ (or
    the attribute) pointing at a model that disagrees with the LLM_CHAIN entry
    still in use, a split that would only resolve itself on the next restart.
    """
    previous_env = os.environ.get(key)
    previous_attr = globals()[key]
    globals()[key] = value
    os.environ[key] = value
    try:
        reload_llm_chain()
    except RuntimeError:
        globals()[key] = previous_attr
        if previous_env is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous_env
        raise


class Setting(NamedTuple):
    """Everything the system needs to know about one runtime-editable setting.

    Previously three dicts keyed by the same names -- SETTABLE, _SERIALIZE and
    _BOOT_COERCERS -- with nothing enforcing they stayed in step. One record
    means adding a setting is one edit in one place.
    """
    coerce: Callable[[str], object]
    # Used by apply_overrides instead of coerce when boot cannot do the full
    # check. NOTIFY_VIA validates against the router, which is empty at boot.
    boot_coerce: Callable[[str], object] | None = None
    serialize: Callable[[object], str] = str
    apply: Callable[[str, object], None] = _apply_attr


SETTABLE = {
    "TIMEZONE":              Setting(_validate_timezone),
    "REMINDER_POLL_SECONDS": Setting(_coerce_poll_seconds),
    "EMAIL_POLL_SECONDS":    Setting(_coerce_poll_seconds),
    "GITHUB_POLL_SECONDS":   Setting(_coerce_poll_seconds),
    "SEARXNG_URL":           Setting(str.strip),
    "FIRECRAWL_URL":         Setting(str.strip),
    "GITHUB_WATCH":          Setting(_parse_github_watch, serialize=_serialize_github_watch),
    "EMAIL_WATCH":           Setting(_parse_email_watch, serialize=_serialize_email_watch),
    "NOTIFY_VIA":            Setting(_coerce_notify_via, boot_coerce=str.strip),

    # Model NAMES are not credentials, so they belong in the allowlist; the
    # matching *_API_KEY values are and never will. Listed literally rather
    # than derived from LLM_PROVIDERS so the allowlist stays readable in one
    # place. Note a side effect worth knowing: providers.resolve() returns None
    # when a provider's model is unset, so setting one here can ADD that
    # provider to the fallback chain, not merely change its model.
    "OLLAMA_MODEL":     Setting(str.strip, apply=_apply_model),
    "LMSTUDIO_MODEL":   Setting(str.strip, apply=_apply_model),
    "OPENAI_MODEL":     Setting(str.strip, apply=_apply_model),
    "CLAUDE_MODEL":     Setting(str.strip, apply=_apply_model),
    "OPENROUTER_MODEL": Setting(str.strip, apply=_apply_model),
}


def serialize_setting(key: str) -> str:
    """The inverse of SETTABLE[key].coerce -- the string form that, fed
    straight back into set_override(key, ...), reproduces config.<key>
    exactly.

    This is what GET /api/plugins and the return value of PATCH /api/settings
    must use instead of str(getattr(config, key)) for every settable value.
    webchat.py has no domain logic (hard rule 2 in CLAUDE.md), so both call
    sites go through this one function rather than each reimplementing it.
    """
    return SETTABLE[key].serialize(globals()[key])


# Snapshot of what .env produced, taken before any override is applied, so
# clear_override() can restore the file's value exactly rather than trying to
# re-derive it.
_DEFAULTS = {key: globals()[key] for key in SETTABLE}


def apply_overrides() -> None:
    """Read the settings table onto this module.

    This is the entire live-apply mechanism, and it works only because every
    consumer reads `config.X` at call time -- including the
    `asyncio.sleep(config.REMINDER_POLL_SECONDS)` inside the reminder loop.
    Nothing in wren/ captures a config value into a module-level constant at
    import. If that ever changes, live edits silently stop working for whatever
    got captured.
    """
    from . import settings

    for key, raw in settings.all().items():
        if key.startswith("skill."):
            # registry.py owns this namespace (skill.<module>.enabled, read
            # via registry.is_enabled()) -- it shares this table but is not a
            # SETTABLE entry, so without this skip every toggled skill would
            # log a false "unknown stored setting" warning on every boot,
            # training the operator to ignore the one warning that actually
            # means something (a row written by a genuinely older Wren).
            continue
        spec = SETTABLE.get(key)
        if spec is None:
            # A version that no longer knows this key must still boot -- the
            # row was written by an older Wren, and crashing on it would make
            # the upgrade unrecoverable without hand-editing the database.
            logging.warning(f"ignoring unknown stored setting '{key}'")
            continue
        coerce = spec.boot_coerce or spec.coerce
        try:
            spec.apply(key, coerce(raw))
        except (ValueError, RuntimeError) as e:
            logging.warning(f"ignoring invalid stored setting '{key}'={raw!r}: {e}")


def set_override(key: str, raw: str):
    """Validate, persist, then apply -- in that order.

    A rejected value never reaches the database, and a failed write never
    leaves memory ahead of disk. Coercion happens during validation, so the
    value is usually known-good by the time it is applied -- except for the
    *_MODEL keys, where "known-good" (does a chain still resolve?) can only
    be confirmed by apply() actually rebuilding LLM_CHAIN. If that rebuild is
    refused, the row this call just persisted is put back to whatever was
    there before (or removed, if there was nothing), so a refused apply still
    leaves no change persisted -- the same guarantee the docstring already
    made for a refused *coerce*, extended to cover a refused apply too.
    """
    from . import settings

    if key not in SETTABLE:
        raise KeyError(key)
    spec = SETTABLE[key]
    value = spec.coerce(raw)         # raises ValueError/RuntimeError if bad
    previous_raw = settings.get(key)
    settings.set(key, raw)
    try:
        spec.apply(key, value)
    except Exception:
        if previous_raw is None:
            settings.unset(key)
        else:
            settings.set(key, previous_raw)
        raise
    return value


def clear_override(key: str) -> None:
    """Drop the stored row and restore what .env produced at import.

    Same apply-can-fail problem as set_override, mirrored: if the *_MODEL
    apply refuses (empty chain), the row just unset is put back exactly as it
    was, so a refused clear does not leave "no override" persisted while the
    running process is, in fact, still overridden.
    """
    from . import settings

    if key not in SETTABLE:
        raise KeyError(key)
    previous_raw = settings.get(key)
    settings.unset(key)
    try:
        SETTABLE[key].apply(key, _DEFAULTS[key])
    except Exception:
        if previous_raw is not None:
            settings.set(key, previous_raw)
        raise
