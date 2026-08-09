# Conversation History for Intent Classification — Design

## Goal

`brain.detect_intent()` currently classifies each DM in isolation — it never
sees prior turns. `brain.chat()` already gets the last 10 channel messages
as history, but only on the `chat` fallback path, *after* classification has
already happened. Follow-ups that depend on context ("add another one",
"cancel that") have nothing to resolve against at classification time, so
they're liable to misclassify. Give `detect_intent()` the same history
window `chat()` already uses, fetched once per message and reused by both.

## `wren/brain.py`: `detect_intent` gains a `history` param

Same shape as `chat()`'s existing parameter — a list of `{"role", "content"}`
dicts, inserted between the system prompt and the current user message:

```python
def detect_intent(user_id: int, text: str, history: list[dict] | None = None) -> dict:
    ...
    messages = [{"role": "system", "content": system}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": text})
    raw = _complete(messages, temperature=0.1, max_tokens=200)
    ...
```

## `wren/bot.py`: fetch history once, before classification

The history fetch/convert block currently lives inside the `else: # chat`
branch (`bot.py:169-175`). It moves above the `detect_intent()` call so it
runs for every message, and the same `history` value is passed to both
`detect_intent()` and — only when the intent resolves to `chat` — reused for
`brain.chat()` instead of being fetched a second time:

```python
try:
    raw_history = [m async for m in message.channel.history(limit=10, before=message)]
    history = discord_utils.history_to_messages(raw_history, client.user.id)
except Exception as e:
    logging.warning(f"Could not fetch history: {e}")
    history = None

result = brain.detect_intent(user_id, text, history)
...
else:  # chat
    reply = brain.chat(text, history)
```

Error handling is unchanged: a fetch failure still falls back to
`history = None`, which both `detect_intent` and `chat` already treat as
"no history" (skip the block).

## Cost

- One extra Discord API call per message for intents that previously
  skipped history entirely (everything except `chat`).
- More tokens per classification call now that up to 10 prior messages are
  included — no new config knob; reuses the existing window/format rather
  than adding a separate tunable size for classification-only history.

## Testing

`detect_intent` classification itself isn't unit-testable (LLM-driven,
mocked in tests — same constraint noted in `fa54b3f`). Add a test asserting
that when `history` is passed, it appears in the `messages` list handed to
the mocked client, positioned between the system message and the user
message — mirroring the existing test that covers this for `chat()`.

## Out of scope

- Persisting history anywhere (DB, cache) — it's refetched from Discord's
  own message log each time, same as today.
- Tuning the history window size (stays at 10, matching `chat()`).
- Filtering/summarizing history before it reaches the model — passed
  through as-is.
