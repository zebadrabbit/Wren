# LLM Provider Fallback — Design

## Goal

Wren currently talks to exactly one LLM endpoint, hardcoded in `config.py`
(`LLM_BASE_URL`, `LLM_MODEL`), via a single module-level `openai.OpenAI`
client in `brain.py`. If that endpoint is down, every LLM call in the bot
fails (intent detection, recall, chat).

We want to support multiple providers — LM Studio / other local
OpenAI-compatible servers, Ollama, OpenAI, Claude (Anthropic), and
OpenRouter — configured as an ordered priority list, so that if the primary
fails, Wren automatically tries the next one and keeps operating instead of
going down.

## Providers

All target providers speak the OpenAI chat-completions wire format:

- **lmstudio** — any local/self-hosted OpenAI-compatible server (LM Studio,
  llama.cpp server, etc). No API key required, just a base URL.
- **ollama** — local Ollama server's OpenAI-compatible endpoint
  (`http://localhost:11434/v1` by default). No API key required.
- **openai** — api.openai.com. Requires `OPENAI_API_KEY`.
- **claude** — Anthropic's OpenAI-compatible endpoint
  (`https://api.anthropic.com/v1`). Requires `ANTHROPIC_API_KEY`.
- **openrouter** — openrouter.ai. Requires `OPENROUTER_API_KEY`.

Because they're all OpenAI-compatible, one client shape (`openai.OpenAI`)
covers all five — no new dependency.

## Config

`providers.py` (new) holds a `PROVIDER_DEFAULTS` dict, one entry per
provider name, each specifying:
- default `base_url` (where one makes sense, e.g. ollama, claude, openrouter)
- the env var name to read `base_url` from (for providers with no sane
  default, e.g. lmstudio — every self-hosted URL is different)
- the env var name to read `api_key` from (`None` for providers that don't
  need one)
- the env var name to read `model` from

`config.py` reads `LLM_PROVIDERS` from `.env` — a comma-separated ordered
list, e.g.:

```
LLM_PROVIDERS=lmstudio,openai,claude
LMSTUDIO_BASE_URL=http://192.168.1.70:30068/v1
LMSTUDIO_MODEL=gemma4-e4b-131k:latest
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
ANTHROPIC_API_KEY=sk-ant-...
CLAUDE_MODEL=claude-sonnet-5
```

For each name in `LLM_PROVIDERS`, config resolves its base_url/api_key/model
from env (falling back to `PROVIDER_DEFAULTS`). **If a required env var
(api_key, or base_url when there's no default) is missing, that provider is
silently skipped** — this lets `LLM_PROVIDERS` name all 6 providers while
only some are actually credentialed. If the resulting list is empty,
`config.py` raises at startup (same as today's missing-env-var behavior) —
an empty chain is a real misconfiguration, not a partial one.

`config.LLM_CHAIN` is the resolved ordered list of provider configs
(name, base_url, api_key, model) that `brain.py` consumes.

## Fallback behavior

`brain.py` replaces the single module-level `_client` with:

- `_clients: dict[str, OpenAI]` — lazily built and cached per provider name,
  so each provider's client is constructed once.
- `_complete(messages, temperature) -> str` — walks `config.LLM_CHAIN` in
  order, gets/builds that provider's client, calls
  `chat.completions.create(...)`. On **any exception**, logs a warning
  (`"provider %s failed: %s"`) and tries the next provider. If every
  provider in the chain raises, re-raises the last exception.

`detect_intent`, `recall`, and `chat` all call `_complete` instead of
touching a client directly. `detect_intent` already wraps its call in
try/except and falls back to a `chat` intent on any failure (including a
fully-exhausted chain). `recall`/`chat` let an exhausted-chain exception
propagate to bot.py's existing catch-all in `on_message`, which already
replies "Something went wrong, try again." — no change needed there.

No retry/backoff within a single provider — one attempt each, then move on.
This is a liveness fallback (keep operating on a different endpoint), not a
transient-error retry mechanism.

## Bug fix: notes tag search

While touching this code: `notes.search()` filters tags with
`tags LIKE '%tag%'` against a comma-joined string column. This
substring-matches, so searching for tag `"home"` also matches a note tagged
`"homework"`. Fix: match tags as exact comma-delimited tokens (e.g.
`LIKE '%,tag,%'` against a `,`-padded stored value, or filter in Python
against `stored.split(",")`). Python-side filtering is simpler and the note
volume here is trivially small, so: fetch by owner, filter by
`set(tags) & set(row_tags)` in Python.

## Testing

- `tests/test_brain.py` currently patches
  `brain._client.chat.completions.create`. Update to patch through
  `brain._clients` / a client-factory seam instead, so the fallback loop
  itself is exercised.
- New test: primary provider's client raises → second provider's client is
  called and its response is what's returned.
- New test: all providers raise → `_complete` raises (and `detect_intent`
  catches it, falling back to chat intent — already covered by the existing
  bad-JSON-falls-back-to-chat-shaped test, but add one that raises instead
  of returning bad JSON).
- `tests/test_notes.py`: add a test that searching tag `"home"` does not
  match a note tagged `"homework"`.

## Out of scope

- No retry/backoff, no circuit breaker / cooldown for a failed provider
  within a run (always starts from the top of the chain on the next
  message).
- No per-request provider override (e.g. user picks provider via Discord
  command) — chain order is fixed at startup from `.env`.
- No streaming responses — unchanged from current behavior.
