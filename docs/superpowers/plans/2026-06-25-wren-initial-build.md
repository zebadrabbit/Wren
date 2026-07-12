# Wren Initial Build Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a private Discord DM bot that saves notes to SQLite, recalls them via local LLM, and forwards them to whitelisted contacts.

**Architecture:** Every inbound DM passes a whitelist gate in `bot.py`, then goes to `brain.py` for LLM intent classification, then routes to `notes.py` for storage/retrieval or back to Discord for sending. Four intents: save_note, recall_notes, send_to_person, chat.

**Tech Stack:** Python 3.11+, discord.py, openai (for LLM client), sqlite3 (stdlib), python-dotenv

## Global Constraints

- Python 3.11+
- LLM endpoint: `http://192.168.1.70:30068/v1`, model: `gemma4-e4b-131k:latest`
- Discord bot listens to DMs only — no guild/channel messages
- Whitelist enforced on every message; silently ignore non-whitelisted users
- Wren personality: short, structured, ready — no filler, no affirmations
- SQLite DB file: `wren.db` at project root, created at runtime
- Sensitive values (DISCORD_TOKEN, user IDs) in `.env`, never hardcoded

---

## File Map

| File | Responsibility |
|------|---------------|
| `config.py` | Load env vars, define WHITELIST and LLM settings |
| `notes.py` | SQLite CRUD: save, search, list_recent |
| `brain.py` | LLM calls: detect_intent, recall, chat |
| `bot.py` | Discord client, DM handler, routing |
| `tests/test_notes.py` | Unit tests for notes.py |
| `tests/test_brain.py` | Unit tests for brain.py (mock LLM) |
| `.env.example` | Template for required env vars |
| `requirements.txt` | Pinned dependencies |

---

### Task 1: Project scaffold + config

**Files:**
- Create: `config.py`
- Create: `.env.example`
- Create: `requirements.txt`
- Create: `.env` (from .env.example, not committed)
- Create: `.gitignore`

**Interfaces:**
- Produces:
  - `config.DISCORD_TOKEN: str`
  - `config.LLM_BASE_URL: str`
  - `config.LLM_MODEL: str`
  - `config.WHITELIST: dict[str, int]` — `{"owner": discord_user_id, "husband": discord_user_id}`
  - `config.ID_TO_NAME: dict[int, str]` — reverse lookup of WHITELIST

- [ ] **Step 1: Create `.gitignore`**

```
.env
wren.db
__pycache__/
*.pyc
.pytest_cache/
```

- [ ] **Step 2: Create `.env.example`**

```
DISCORD_TOKEN=your_bot_token_here
OWNER_ID=111111111111111111
HUSBAND_ID=222222222222222222
```

- [ ] **Step 3: Create `requirements.txt`**

```
discord.py==2.3.2
openai==1.30.5
python-dotenv==1.0.1
pytest==8.2.2
```

- [ ] **Step 4: Install dependencies**

```bash
pip install -r requirements.txt
```

- [ ] **Step 5: Create `config.py`**

```python
import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
LLM_BASE_URL = "http://192.168.1.70:30068/v1"
LLM_MODEL = "gemma4-e4b-131k:latest"

WHITELIST: dict[str, int] = {
    "owner": int(os.environ["OWNER_ID"]),
    "husband": int(os.environ["HUSBAND_ID"]),
}

ID_TO_NAME: dict[int, str] = {v: k for k, v in WHITELIST.items()}
```

- [ ] **Step 6: Create `.env` from `.env.example` and fill in real values**

```bash
cp .env.example .env
# edit .env with your actual DISCORD_TOKEN, OWNER_ID, HUSBAND_ID
```

- [ ] **Step 7: Verify config loads**

```bash
python -c "import config; print(config.WHITELIST)"
```

Expected: prints dict with real IDs, no errors.

- [ ] **Step 8: Commit**

```bash
git init
git add config.py .env.example requirements.txt .gitignore
git commit -m "feat: project scaffold and config"
```

---

### Task 2: Notes (SQLite CRUD)

**Files:**
- Create: `notes.py`
- Create: `tests/test_notes.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `notes.init_db() -> None` — creates table if not exists, call at startup
  - `notes.save(owner_id: int, content: str, tags: list[str]) -> int` — returns note id
  - `notes.search(owner_id: int, tags: list[str] | None = None) -> list[dict]` — each dict: `{id, content, tags, created_at}`
  - `notes.list_recent(owner_id: int, n: int = 10) -> list[dict]`

- [ ] **Step 1: Write failing tests**

Create `tests/__init__.py` (empty), then `tests/test_notes.py`:

```python
import os, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("HUSBAND_ID", "2")

import notes

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(notes, "DB_PATH", str(tmp_path / "test.db"))
    notes.init_db()

def test_save_and_list_recent():
    note_id = notes.save(1, "buy milk", ["grocery"])
    assert isinstance(note_id, int)
    results = notes.list_recent(1)
    assert len(results) == 1
    assert results[0]["content"] == "buy milk"
    assert results[0]["tags"] == "grocery"

def test_search_by_tag():
    notes.save(1, "buy eggs", ["grocery"])
    notes.save(1, "fix the fence", ["plans", "home"])
    results = notes.search(1, tags=["grocery"])
    assert len(results) == 1
    assert "eggs" in results[0]["content"]

def test_search_no_tag_returns_all():
    notes.save(1, "note one", ["a"])
    notes.save(1, "note two", ["b"])
    results = notes.search(1)
    assert len(results) == 2

def test_owner_isolation():
    notes.save(1, "my note", ["personal"])
    notes.save(2, "their note", ["personal"])
    assert len(notes.list_recent(1)) == 1
    assert len(notes.list_recent(2)) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_notes.py -v
```

Expected: ImportError or AttributeError — `notes` module doesn't exist yet.

- [ ] **Step 3: Implement `notes.py`**

```python
import sqlite3
from datetime import datetime, timezone

DB_PATH = "wren.db"

def _conn():
    return sqlite3.connect(DB_PATH)

def init_db() -> None:
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id   TEXT NOT NULL,
                content    TEXT NOT NULL,
                tags       TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)

def save(owner_id: int, content: str, tags: list[str]) -> int:
    ts = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO notes (owner_id, content, tags, created_at) VALUES (?,?,?,?)",
            (str(owner_id), content, ",".join(tags), ts),
        )
        return cur.lastrowid

def search(owner_id: int, tags: list[str] | None = None) -> list[dict]:
    with _conn() as con:
        con.row_factory = sqlite3.Row
        if tags:
            like_clauses = " OR ".join("tags LIKE ?" for _ in tags)
            params = [f"%{t}%" for t in tags] + [str(owner_id)]
            rows = con.execute(
                f"SELECT * FROM notes WHERE ({like_clauses}) AND owner_id=? ORDER BY created_at DESC",
                params,
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM notes WHERE owner_id=? ORDER BY created_at DESC",
                (str(owner_id),),
            ).fetchall()
    return [dict(r) for r in rows]

def list_recent(owner_id: int, n: int = 10) -> list[dict]:
    with _conn() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM notes WHERE owner_id=? ORDER BY created_at DESC LIMIT ?",
            (str(owner_id), n),
        ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_notes.py -v
```

Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add notes.py tests/
git commit -m "feat: notes SQLite CRUD"
```

---

### Task 3: Brain (LLM intent detection + recall + chat)

**Files:**
- Create: `brain.py`
- Create: `tests/test_brain.py`

**Interfaces:**
- Consumes: `config.LLM_BASE_URL`, `config.LLM_MODEL`
- Produces:
  - `brain.detect_intent(user_id: int, text: str) -> dict` — returns `{"intent": str, "content": str, "tags": list[str], "person": str | None}`
  - `brain.recall(notes: list[dict], query: str) -> str` — returns natural language summary
  - `brain.chat(text: str) -> str` — returns Wren's reply

- [ ] **Step 1: Write failing tests**

`tests/test_brain.py`:

```python
import os, json, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("HUSBAND_ID", "2")

from unittest.mock import patch, MagicMock
import brain

def _mock_completion(content: str):
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp

def test_detect_intent_save():
    payload = json.dumps({
        "intent": "save_note",
        "content": "buy milk",
        "tags": ["grocery"],
        "person": None
    })
    with patch.object(brain._client.chat.completions, "create", return_value=_mock_completion(payload)):
        result = brain.detect_intent(1, "remind me to buy milk")
    assert result["intent"] == "save_note"
    assert result["tags"] == ["grocery"]

def test_detect_intent_bad_json_falls_back_to_chat():
    with patch.object(brain._client.chat.completions, "create", return_value=_mock_completion("not json")):
        result = brain.detect_intent(1, "hello")
    assert result["intent"] == "chat"

def test_recall_returns_string():
    sample_notes = [{"content": "buy eggs", "tags": "grocery", "created_at": "2026-06-25T10:00:00+00:00"}]
    with patch.object(brain._client.chat.completions, "create", return_value=_mock_completion("You need eggs.")):
        result = brain.recall(sample_notes, "what groceries do I need?")
    assert isinstance(result, str)
    assert len(result) > 0

def test_chat_returns_string():
    with patch.object(brain._client.chat.completions, "create", return_value=_mock_completion("Hello.")):
        result = brain.chat("hey")
    assert isinstance(result, str)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_brain.py -v
```

Expected: ImportError — `brain` doesn't exist yet.

- [ ] **Step 3: Implement `brain.py`**

```python
import json
from datetime import datetime, timezone
from openai import OpenAI
import config

_client = OpenAI(base_url=config.LLM_BASE_URL, api_key="local")

_SYSTEM = """You are Wren, a private personal assistant. You are short, structured, and ready. No filler, no affirmations.

Today is {date}.

When classifying intent, respond ONLY with valid JSON matching this schema:
{{
  "intent": "save_note" | "recall_notes" | "send_to_person" | "chat",
  "content": "<extracted note or message content>",
  "tags": ["<tag1>", "<tag2>"],
  "person": "<name from whitelist or null>"
}}

Known contacts: {contacts}

Guidelines:
- save_note: user is capturing something for later (grocery item, plan, idea, reminder)
- recall_notes: user wants to retrieve or search past notes
- send_to_person: user wants to send a message or note to someone
- chat: anything else (questions, casual conversation)
- tags: 1-3 lowercase single-word tags relevant to the content
- person: only set for send_to_person intent, use the contact name as given
"""

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%A %B %d %Y %H:%M UTC")

def _contacts() -> str:
    return ", ".join(config.WHITELIST.keys())

def detect_intent(user_id: int, text: str) -> dict:
    system = _SYSTEM.format(date=_now(), contacts=_contacts())
    try:
        resp = _client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            temperature=0.1,
        )
        raw = resp.choices[0].message.content.strip()
        # strip markdown code fences if model wraps JSON
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except Exception:
        return {"intent": "chat", "content": text, "tags": [], "person": None}

def recall(notes: list[dict], query: str) -> str:
    notes_text = "\n".join(
        f"- [{n['created_at'][:10]}] {n['content']} (tags: {n['tags']})"
        for n in notes
    )
    prompt = f"User's notes:\n{notes_text}\n\nUser asked: {query}\n\nAnswer directly using only what's in the notes."
    resp = _client.chat.completions.create(
        model=config.LLM_MODEL,
        messages=[
            {"role": "system", "content": f"You are Wren. {_SYSTEM.split(chr(10))[1]}"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
    )
    return resp.choices[0].message.content.strip()

def chat(text: str) -> str:
    resp = _client.chat.completions.create(
        model=config.LLM_MODEL,
        messages=[
            {"role": "system", "content": f"You are Wren, a personal assistant. Short, structured, ready. No filler. Today is {_now()}."},
            {"role": "user", "content": text},
        ],
        temperature=0.7,
    )
    return resp.choices[0].message.content.strip()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_brain.py -v
```

Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add brain.py tests/test_brain.py
git commit -m "feat: brain LLM intent detection and recall"
```

---

### Task 4: Bot (Discord client + routing)

**Files:**
- Create: `bot.py`

**Interfaces:**
- Consumes:
  - `config.DISCORD_TOKEN`, `config.WHITELIST`, `config.ID_TO_NAME`
  - `brain.detect_intent(user_id: int, text: str) -> dict`
  - `brain.recall(notes: list[dict], query: str) -> str`
  - `brain.chat(text: str) -> str`
  - `notes.init_db() -> None`
  - `notes.save(owner_id: int, content: str, tags: list[str]) -> int`
  - `notes.search(owner_id: int, tags: list[str] | None) -> list[dict]`
- Produces: running Discord bot process

- [ ] **Step 1: Implement `bot.py`**

```python
import logging
import discord
import config
import brain
import notes

logging.basicConfig(level=logging.INFO)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    notes.init_db()
    logging.info(f"Wren online as {client.user}")

@client.event
async def on_message(message: discord.Message):
    # ignore own messages and non-DMs
    if message.author == client.user:
        return
    if not isinstance(message.channel, discord.DMChannel):
        return

    user_id = message.author.id

    # whitelist gate
    if user_id not in config.ID_TO_NAME:
        return

    text = message.content.strip()
    if not text:
        return

    result = brain.detect_intent(user_id, text)
    intent = result.get("intent", "chat")
    content = result.get("content", text)
    tags = result.get("tags", [])
    person = result.get("person")

    if intent == "save_note":
        notes.save(user_id, content, tags)
        await message.channel.send("Saved.")

    elif intent == "recall_notes":
        matches = notes.search(user_id, tags=tags if tags else None)
        if not matches:
            await message.channel.send("No notes found.")
        else:
            summary = brain.recall(matches, content)
            await message.channel.send(summary)

    elif intent == "send_to_person":
        target_name = (person or "").lower()
        target_id = config.WHITELIST.get(target_name)
        if not target_id:
            await message.channel.send("I don't know how to reach them.")
            return
        notes.save(user_id, content, tags)
        target_user = await client.fetch_user(target_id)
        await target_user.send(f"From {config.ID_TO_NAME.get(user_id, 'someone')}: {content}")
        await message.channel.send(f"Sent to {target_name}.")

    else:  # chat
        reply = brain.chat(text)
        await message.channel.send(reply)

client.run(config.DISCORD_TOKEN)
```

- [ ] **Step 2: Run Wren locally and send yourself a DM**

```bash
python bot.py
```

Expected: logs `Wren online as <bot name>`. Send a DM from your Discord account.

- [ ] **Step 3: Test each intent manually via DM**

Send these DMs and verify responses:
1. `"remind me we need to fix the fence"` → Wren replies `"Saved."`
2. `"add milk and eggs to the grocery list"` → Wren replies `"Saved."`
3. `"what groceries do I need?"` → Wren replies with a summary of grocery notes
4. `"send my husband we need to fix the fence this weekend"` → husband gets a DM, you get `"Sent to husband."`
5. `"what time is it?"` → Wren replies conversationally

- [ ] **Step 4: Commit**

```bash
git add bot.py
git commit -m "feat: discord bot DM handler with intent routing"
```

---

### Task 5: Systemd service (optional, run Wren on boot)

> Skip this task if you're running Wren manually or via another process manager.

**Files:**
- Create: `wren.service`

- [ ] **Step 1: Create service file**

Replace `/home/winter/work/Wren` and `winter` with your actual path and username.

```ini
[Unit]
Description=Wren Discord Bot
After=network.target

[Service]
Type=simple
User=winter
WorkingDirectory=/home/winter/work/Wren
ExecStart=/usr/bin/python3 /home/winter/work/Wren/bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Install and enable**

```bash
sudo cp wren.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable wren
sudo systemctl start wren
sudo systemctl status wren
```

Expected: `Active: active (running)`

- [ ] **Step 3: Commit**

```bash
git add wren.service
git commit -m "ops: systemd service for Wren"
```
