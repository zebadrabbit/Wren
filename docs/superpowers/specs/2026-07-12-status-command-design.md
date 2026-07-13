# Status Command — Design

## Goal

"show model" / "what backend are you using" / "show status" — a single
consolidated status reply: active LLM backend/model/endpoint, process
uptime, and token usage since last restart. A "core" intent (like `help`),
not a domain plugin — it reports on the bot process itself.

## `brain.py`: track what's actually being used, not just configured

`config.LLM_CHAIN[0]` is the *configured* primary, but if it's down and
Wren has been silently falling back, that's misleading. Track the
**last successfully-used provider** instead — updated on every successful
`_complete()` call, so status reflects what's actually serving requests
right now, including after a fallback.

New module-level state:

```python
_last_provider: dict | None = None
_token_usage = {"prompt": 0, "completion": 0, "total": 0}
```

`_complete()` gains two side effects on a successful call, before
returning: set `_last_provider = provider` (the chain entry that
succeeded), and accumulate token counts from `resp.usage` if the backend's
response includes it (`getattr(resp, "usage", None)` — defensive, since
not every OpenAI-compatible server necessarily populates this field).

New function:

```python
def status() -> dict:
    """{"provider": <last successful provider dict, or config.LLM_CHAIN[0]
    if nothing has succeeded yet>, "tokens": {"prompt": int, "completion":
    int, "total": int}}"""
```

No persistence — token usage and "last provider" both reset on restart,
matching the "session" framing already agreed on.

## `bot.py`: uptime + the `status` core intent

A module-level `_START_TIME = time.monotonic()` recorded once at import
(monotonic, not wall-clock, so it's immune to system clock adjustments — a
process-uptime measurement, not a calendar timestamp). A small formatter:

```python
def _format_uptime(seconds: float) -> str:
    """123 -> "2m", 3800 -> "1h 3m", 90000 -> "1d 1h 0m" """
```

New `elif intent == "status":` branch (alongside the existing `help`/
`send_to_person` core branches), formatting `brain.status()`'s output plus
the computed uptime into one multi-line reply:

```
Backend: ollama (qwen2.5:7b-instruct)
Endpoint: http://192.168.1.70:30068/v1
Uptime: 2h 15m
Tokens this session: 1,234 (856 prompt / 378 completion)
```

## `brain.py`'s prompt

`"status"` added to the intent enum (alongside `"help"`) and one new
guideline bullet: "user wants to know Wren's operational status — active
LLM backend/model/endpoint, uptime, token usage."

## Testing

- `tests/test_brain.py`: `_complete()` accumulates `_token_usage` correctly
  when a mocked response has a `.usage` attribute (prompt/completion/total
  token counts); handles a response with no `.usage` gracefully (no crash,
  no increment); `_last_provider` updates to the successful provider,
  including the *secondary* provider after a primary-fails-then-succeeds
  fallback (already-established fallback-chain test pattern from prior
  work); `status()`'s return shape, both before any call has succeeded
  (falls back to `config.LLM_CHAIN[0]`) and after one has. Tests
  `monkeypatch.setattr(brain, "_last_provider", None)` /
  `monkeypatch.setattr(brain, "_token_usage", {...})` to avoid cross-test
  state pollution, same pattern as the existing `register_plugins`-related
  tests resetting `_plugin_intents`/`_plugin_guidelines`.
- `_format_uptime`/the `status` dispatch branch live in `bot.py`, which has
  no automated test coverage in this repo (pre-existing condition —
  module-level `client.run()` makes it unimportable without a real
  token). Verified by manual read-back plus computing expected output for
  a few known second counts by hand against the formatter's logic, same
  precedent as all prior `bot.py` changes.

## Out of scope

- Persisting token usage/uptime across restarts.
- Per-provider historical stats (only "last successfully used" is
  tracked, not a log of every fallback event).
- Cost estimation from token counts (would need per-provider pricing data
  Wren doesn't have).
