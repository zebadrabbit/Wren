# Reminder Timezone Support — Design

## Goal

"remind me at 9am my time" produced a reminder scheduled for the wrong
hour, silently interpreted as UTC with no indication to the user that
anything was assumed. Root cause: `brain.py` asked the LLM to compute an
absolute *UTC* datetime from a relative/absolute local-sounding phrase —
asking a small model to do timezone conversion *and* relative-time math in
one step, with no way for the user to know UTC was assumed. Fix: give the
LLM a local "now" to reason from, have it output a local (unconverted)
datetime, and let deterministic code do the local→UTC conversion.

## Scope: one global `TIMEZONE`, not per-owner

Wren is a household-scale bot (currently up to two whitelisted contacts,
same reasoning as the shared shopping list) — one global `TIMEZONE` setting
is sufficient. Per-owner timezones would need a new settings-storage
mechanism that doesn't exist yet, for a problem this household almost
certainly doesn't have (different people in different zones). Not pursued.

## `config.py`

```python
import zoneinfo

TIMEZONE = os.environ.get("TIMEZONE", "UTC")
try:
    zoneinfo.ZoneInfo(TIMEZONE)
except zoneinfo.ZoneInfoNotFoundError:
    raise RuntimeError(f"TIMEZONE '{TIMEZONE}' is not a valid IANA timezone name (e.g. America/Chicago).")
```

Stdlib `zoneinfo` (Python 3.9+) — no new dependency. Default `UTC` keeps
existing behavior for anyone who doesn't set it.

## `brain.py`

`_now()` changes from reporting UTC to reporting the current time in
`config.TIMEZONE`:

```python
def _now() -> str:
    return datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%A %B %d %Y %H:%M %Z")
```

(`%Z` renders the zone abbreviation, e.g. "CDT" — more readable in the
prompt than the IANA name.)

The `when` guideline changes from "an absolute ISO 8601 **UTC** datetime"
to "an absolute ISO 8601 datetime **in the same timezone as 'today'
above** — do not convert to UTC yourself." This is the actual fix: the
model's only job becomes relative-time arithmetic in a timezone it's
already been told "now" in, not also converting zones.

## `reminder_plugin.py`

`_parse_when()` now performs the local→UTC conversion and always returns a
UTC-normalized datetime (or `None`):

```python
def _parse_when(when):
    if not when:
        return None
    try:
        parsed = datetime.fromisoformat(when)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(config.TIMEZONE))
    parsed_utc = parsed.astimezone(timezone.utc)
    if parsed_utc < datetime.now(timezone.utc) - _GRACE:
        return None
    return parsed_utc
```

(If the model *does* include an offset/UTC designator despite the
instruction not to, `astimezone(timezone.utc)` still normalizes it
correctly — the code doesn't break, it just means the model followed the
old habit; harmless either way.)

`set_reminder`'s caller simplifies — `_parse_when` already returns a UTC
datetime, so no separate `.astimezone(timezone.utc)` call is needed at the
call site anymore.

**Display** (confirmation message and `recall_reminders`' listing) converts
the stored UTC `fire_at` back to local time for output:

```python
def _format_local(fire_at_utc_iso: str) -> str:
    dt = datetime.fromisoformat(fire_at_utc_iso).astimezone(ZoneInfo(config.TIMEZONE))
    return dt.strftime("%Y-%m-%d %H:%M %Z")
```

"Reminder set for 2026-07-13 09:00 CDT." instead of a UTC timestamp the
user has to mentally convert. Storage in `reminders.py` is unchanged — it
was already timezone-agnostic (comparisons work correctly on any two
UTC-normalized ISO strings, per the existing `timespec="seconds"`
consistency requirement from the original reminders design).

## Config

```
TIMEZONE=America/Chicago
```

Default `UTC` if unset. `.env.example`/README document this with an
example.

## Testing

- `tests/test_config.py`: `TIMEZONE` defaults to `"UTC"`; an invalid zone
  name raises `RuntimeError` (test the validation logic directly — same
  "extract for testability" pattern as `_parse_email_watch`/
  `_build_whitelist`, since `config.py`'s own import-time state isn't
  re-testable under different env vars without reimporting the module).
- `tests/test_brain.py`: with `TIMEZONE` set to a non-UTC zone, the
  captured system prompt's "Today is ..." line reflects that zone's
  current local time, not UTC (mirrors the existing
  `test_register_plugins_included_in_prompt`-style captured-prompt test).
- `tests/test_reminder_plugin.py`: `_parse_when()` with a naive datetime
  string and a non-UTC configured `TIMEZONE` correctly localizes then
  converts to the right UTC instant; `_format_local()` converts a stored
  UTC ISO string back to the correct local wall-clock time and zone
  abbreviation.

## Out of scope

- Per-owner timezones.
- Daylight-saving-time edge cases beyond what `zoneinfo` already handles
  correctly (it does — this is exactly what it's for).
- Auto-detecting timezone from Discord locale/IP — explicit config only.
