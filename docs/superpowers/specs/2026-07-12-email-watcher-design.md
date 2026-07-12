# Email-Arrival Watcher — Design

## Goal

Wren should notice when an email arrives from a specific person and DM you
about it on Discord — "send me a message when an email from `<person>`
arrives." This is the first sub-project under "external integrations," and
the first real consumer of the plugin system's `start()` background-task
hook and `discord_utils.notify()` helper (both built in the plugin-system
sub-project specifically for this kind of use case).

Node-RED and Grafana integration, also named in the original request, are
explicitly deferred — neither has a concrete trigger spec yet ("possible
integration"), and the direction (push to Wren vs. Wren polling them) is
unresolved. That remains a separate, later sub-project.

## Fixing the plugin system for its first real event-only consumer

Two things flagged as forward-looking notes during the plugin-system
review become real now that a plugin actually uses them:

1. **Relaxed plugin contract.** `email_plugin.py` is event-only — it has no
   user-invoked intents, so it shouldn't need vestigial `INTENTS = []`,
   `PROMPT_GUIDELINES = ""`, `handle()` stubs. `plugins.py`'s
   `all_intents()`/`all_guidelines()`/`INTENT_HANDLERS` construction changes
   from direct attribute access to `getattr(plugin, "INTENTS", [])` /
   `getattr(plugin, "PROMPT_GUIDELINES", "")`, so a plugin can simply omit
   what it doesn't need.
2. **Non-blocking `start_all()`.** `email_plugin.start()` is an infinite
   poll loop. `plugins.start_all()` currently `await`s each plugin's
   `start()` in sequence — a loop that never returns would block bot
   startup (the "Wren online" log) forever and prevent any later plugin's
   `start()` from ever running. Fix: `start_all()` wraps each call in
   `asyncio.create_task(plugin.start(client))` instead of awaiting it
   directly, so each plugin's background work runs independently and
   `on_ready` completes immediately.

## `email_plugin.py`

Stdlib-only (`imaplib`, `email` — no new dependency):

```python
def _connect() -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(config.IMAP_HOST)
    conn.login(config.IMAP_USER, config.IMAP_PASSWORD)
    conn.select("INBOX")
    return conn

def _sender_address(raw_from: str) -> str:
    """Extract and lowercase the bare email address from a raw From: header."""

async def _poll_once(client, imap_conn) -> None:
    """One pass: search UNSEEN, notify on sender match, mark every UNSEEN
    message \\Seen (matched or not) so it's never re-processed."""

async def start(client) -> None:
    """If EMAIL_WATCH is empty, return immediately (feature is opt-in).
    Otherwise loop forever: connect, _poll_once, logout, sleep
    EMAIL_POLL_SECONDS. Any exception during a poll is logged as a warning
    and the loop continues — a bad connection never crashes the bot."""
```

`_poll_once` takes the IMAP connection as a parameter (dependency injection)
specifically so it's testable with a mocked connection, rather than
`start()`'s `_connect()` call being the only entry point.

**Matching:** exact, case-insensitive match on the sender's bare email
address (via `email.utils.parseaddr`) against `config.EMAIL_WATCH`. No
fuzzy/display-name matching.

**Notification text:** `f"Email from {sender}: {subject}"` via
`discord_utils.notify(client, contact_name, text)`.

**Dedup:** every UNSEEN message gets marked `\Seen` after processing
(matched or not) — IMAP's own read-state is Wren's dedup mechanism, no new
persistence. Tradeoff (accepted): if this inbox is also read in a normal
mail client, Wren marking messages read there too is a visible side effect.

## `config.py` additions

```python
IMAP_HOST = os.environ.get("IMAP_HOST", "")
IMAP_USER = os.environ.get("IMAP_USER", "")
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "")
EMAIL_POLL_SECONDS = int(os.environ.get("EMAIL_POLL_SECONDS", "60"))

EMAIL_WATCH: dict[str, str] = {}
for pair in os.environ.get("EMAIL_WATCH", "").split(","):
    pair = pair.strip()
    if not pair:
        continue
    addr, name = pair.split(":", 1)
    EMAIL_WATCH[addr.strip().lower()] = name.strip().lower()
```

No fail-fast validation: if `EMAIL_WATCH` is set but IMAP credentials are
missing/wrong, the feature doesn't crash at startup — every poll attempt
fails, is caught, logged as a warning, and retried next interval. This
matches the "log and retry" choice for poll errors generally; a
persistently-failing watcher is visible in logs, not in a crash.

`.env.example` gets a new commented-out block documenting `IMAP_HOST`/
`IMAP_USER`/`IMAP_PASSWORD`/`EMAIL_POLL_SECONDS`/`EMAIL_WATCH`.

## Registration

`plugins.py`'s `PLUGINS` list grows to `[notes_plugin, shopping_plugin,
email_plugin]`.

## Testing

- `tests/test_config.py` (new — config.py has no dedicated tests yet):
  `EMAIL_WATCH` parsing (empty env var → `{}`; one pair; multiple pairs;
  whitespace tolerance).
- `tests/test_email_plugin.py`:
  - `_sender_address()`: plain address, `"Display Name <addr>"` form,
    mixed-case address normalizes to lowercase.
  - `_poll_once()`: mocked `imap_conn` (a `MagicMock` with `search`/`fetch`/
    `store` returning canned IMAP-shaped tuples) — a matching sender
    triggers `discord_utils.notify` (mocked) with the expected contact/text;
    a non-matching sender does not notify but is still marked `\Seen`;
    multiple UNSEEN messages in one poll are all processed.
  - `start()`: with `config.EMAIL_WATCH` empty, `asyncio.run(start(None))`
    returns promptly without attempting to connect (verified via a mocked
    `_connect` that would raise if called).
- `tests/test_plugins.py`: update for the relaxed contract — a fake plugin
  module defining only `start()` (no `INTENTS`/`PROMPT_GUIDELINES`/`handle`)
  is correctly handled by `all_intents()`/`all_guidelines()`/
  `INTENT_HANDLERS` (contributes nothing to any of them, doesn't raise);
  and a new test that `start_all()` returns promptly even when a plugin's
  `start()` never returns (proving the `asyncio.create_task` fix actually
  works — e.g. a fake plugin with `async def start(client): await
  asyncio.sleep(3600)`, asserting `start_all()` completes well under that).
- Still no `tests/test_bot.py` (pre-existing condition, unrelated to this
  sub-project).

## Out of scope

- Node-RED/Grafana webhook receiver — separate future sub-project, needs
  its own brainstorm once the trigger direction is concrete.
- Multiple mailboxes/folders — INBOX only.
- Gmail API / push notifications — IMAP polling only.
- Per-sender custom notification templates — one fixed message format.
