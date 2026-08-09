# Wren — Design Spec
_2026-06-25_

## Overview

Wren is a private Discord DM bot. It serves a small whitelist of users (owner + family/friends), runs against a locally-hosted LLM, and acts as a nimble personal assistant for capturing notes, recalling them, and forwarding them to contacts.

Personality: short, structured, ready. No filler.

---

## Architecture

```
wren/
  bot.py      # Discord client, DM handler, whitelist gate
  brain.py    # LLM calls — intent detection, search, chat
  notes.py    # SQLite CRUD
  config.py   # tokens, whitelist, LLM config
  wren.db     # created at runtime
```

Entry point: `bot.py`. Every inbound DM flows: whitelist check → brain → action → reply.

---

## Components

### bot.py
- Discord client using `discord.py`
- Listens only to DMs
- Rejects messages from users not in `WHITELIST`
- Passes `(user_id, message_text)` to `brain.intent()` 
- Routes result to `notes.py` or sends a Discord DM
- Replies to the sender with Wren's response

### brain.py
- Two LLM calls:
  1. **Intent detection** — classify message, extract structured data, return JSON
  2. **Recall/chat** — inject note context, respond conversationally
- System prompt includes current date/time and Wren's personality
- Model: `gemma4-e4b-131k:latest` at `http://192.168.1.70:30068/v1`
- Returns one of four intents:

```json
{"intent": "save_note", "content": "...", "tags": ["...", "..."], "person": null}
{"intent": "recall_notes", "content": "...", "tags": ["..."], "person": null}
{"intent": "send_to_person", "content": "...", "tags": [], "person": "husband"}
{"intent": "chat", "content": "...", "tags": [], "person": null}
```

### notes.py
- SQLite via stdlib `sqlite3`
- Single table:

```sql
CREATE TABLE notes (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id   TEXT NOT NULL,
  content    TEXT NOT NULL,
  tags       TEXT NOT NULL DEFAULT '',  -- comma-separated
  created_at TEXT NOT NULL              -- ISO 8601
);
```

- Operations: `save(owner_id, content, tags)`, `search(owner_id, tags=None, query=None)`, `list_recent(owner_id, n=10)`, `delete(note_id)`
- Search: tag filter first (SQL LIKE), then passes matches to LLM for natural language ranking/summarization

### config.py
- `DISCORD_TOKEN` — bot token
- `LLM_BASE_URL = "http://192.168.1.70:30068/v1"`
- `LLM_MODEL = "gemma4-e4b-131k:latest"`
- `WHITELIST` — dict mapping name → Discord user ID
- `OWNER_ID` — primary user's Discord ID (used as default note owner)

```python
WHITELIST = {
    "owner": 111111111111111111,
    "husband": 222222222222222222,
}
```

---

## Data Flow

```
User DM → bot.py
  → whitelist check (reject if not in WHITELIST)
  → brain.intent(user_id, text)
    → LLM: classify + extract JSON
  → route by intent:
      save_note    → notes.save() → reply "Saved."
      recall_notes → notes.search() → brain.recall(notes, query) → reply summary
      send_to_person → notes.save() + discord.DM(target_id, content) → reply "Sent to [name]."
      chat         → brain.chat(text) → reply response
```

---

## Sending Notes

- `send_to_person` saves the note to the sender's notes AND forwards it via Discord DM to the target
- Target resolved from `WHITELIST` by name extracted from message
- If name not found: reply "I don't know how to reach them."
- Note is also saved so the sender has a record

---

## Error Handling

- LLM timeout or bad JSON response → reply "Something went wrong, try again."
- Unknown intent from LLM → fall back to `chat`
- DB errors → log + reply with error notice

---

## Out of Scope (this version)

- Note deletion via chat (can be added later)
- Multi-user note sharing / shared lists
- Reminders / scheduled messages
- Web UI
