# Conversational History for Chat — Design

## Goal

`brain.chat()` (Wren's open-ended fallback reply, for anything that isn't a
recognized command) gains recent conversational context, so follow-ups
like "what did you mean by that" or "say more" work naturally. Uses the
bot's `READ_MESSAGE_HISTORY` OAuth permission, granted in the invite but
unused until now.

Intent detection (`brain.detect_intent`) is untouched — this cannot change
how a message gets routed to a command, only how a `chat`-routed reply is
generated.

## `wren/discord_utils.py`: converting Discord history to LLM messages

New pure function, kept here (not `bot.py`, which has no test coverage in
this repo) so it's independently testable:

```python
def history_to_messages(messages: list[discord.Message], bot_user_id: int) -> list[dict]:
    result = []
    for m in messages:
        if not m.content.strip():
            continue
        role = "assistant" if m.author.id == bot_user_id else "user"
        result.append({"role": role, "content": m.content})
    return list(reversed(result))
```

Discord's `channel.history()` yields newest-first; this reverses to
chronological (oldest-first) order, which is what an LLM messages array
expects. Messages with empty/whitespace-only content (e.g. attachment-only
messages, which this bot doesn't currently send but a user might) are
skipped rather than passed through as blank turns.

## `wren/brain.py`: `chat()` gains an optional history parameter

```python
def chat(text: str, history: list[dict] | None = None) -> str:
    messages = [
        {"role": "system", "content": f"You are Wren, a personal assistant. Short, structured, ready. No filler. Answer directly, no reasoning or thinking process shown. /no_think Today is {_now()}."},
    ]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": text})
    return _complete(messages, temperature=0.7, max_tokens=400)
```

`history` defaults to `None` so any other future caller of `chat()` is
unaffected. When present, history turns are inserted between the system
message and the final user message — standard OpenAI-style multi-turn
shape, matching how `_complete()` already consumes a plain `messages: list[dict]`.

## `wren/bot.py`: fetching history before the chat call

The existing `else:  # chat` branch in `on_message` becomes:

```python
else:  # chat
    try:
        raw_history = [m async for m in message.channel.history(limit=10, before=message)]
        history = discord_utils.history_to_messages(raw_history, client.user.id)
    except Exception as e:
        logging.warning(f"Could not fetch history: {e}")
        history = None
    reply = brain.chat(text, history)
    await message.channel.send(reply)
```

A history-fetch failure (missing permission, rate limit, transient
Discord API error) degrades to a no-history `chat()` call rather than
failing the whole reply — the same defensive shape already used by
`_react()` elsewhere in this file (log and continue, never let a
secondary concern break the primary one).

`limit=10` (last 10 messages before the triggering one) balances giving
the LLM a few real back-and-forth turns against prompt size and per-reply
latency (an extra Discord API round-trip before every chat reply).

## Testing

- `tests/test_discord_utils.py`: `history_to_messages` — role assignment
  (`author.id == bot_user_id` → `"assistant"`, otherwise `"user"`),
  chronological reordering (input newest-first, output oldest-first),
  empty-content messages skipped, empty input list returns empty list.
- `tests/test_brain.py`: `chat()` called with `history=None` (default)
  produces a messages list identical to today's shape (system + user, no
  extra entries); called with a non-empty `history` list, the entries
  appear between the system message and the final user message in the
  call sent to the mocked LLM client, in the given order.
- `bot.py`'s history-fetch-and-dispatch wiring has no automated test
  coverage in this repo (pre-existing condition — module-level
  `client.run()` makes it unimportable without a real token), consistent
  with every other `bot.py` change in this project's history.

## Out of scope

- Feeding history into `detect_intent` (explicitly scoped out — a bigger,
  riskier change to the routing logic every message goes through).
- Persisting or summarizing history across restarts (each chat call
  re-fetches live from Discord; no new storage).
- A configurable history window size (hardcoded `limit=10`; revisit only
  if it proves too short/long in practice).
