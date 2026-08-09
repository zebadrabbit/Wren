# Progress Helper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A standalone, tested `Progress` helper class that sends one Discord message and edits it in place for progress updates (no repeated notification pings), ready for a future long-running operation to adopt — not wired into any existing plugin in this plan.

**Architecture:** One new module, `wren/progress.py`, with a single `Progress` class (`start`/`update`/`fail`) wrapping a `discord.Message`. No changes to any existing file.

**Tech Stack:** Python 3.11+, `discord.py` (already a dependency), `pytest`. No new dependencies.

## Global Constraints

- No wiring into `read_page`, `web_search`, `chat`, or any other existing intent — this ships as inert, tested infrastructure only.
- No "thinking mode" detection, config flag, or decision logic about *when* to use `Progress` — out of scope for this plan.
- `update()`/`fail()` take plain text only — no progress bars, percentages, or ETAs.
- No new dependencies.

---

### Task 1: `wren/progress.py` — the `Progress` helper

**Files:**
- Create: `wren/progress.py`
- Test: `tests/test_progress.py`

**Interfaces:**
- Produces:
  - `Progress.__init__(self, message: discord.Message)` — wraps an already-sent message.
  - `Progress.start(cls, channel, text: str) -> Progress` (async classmethod) — sends `text` via `channel.send(text)`, returns a `Progress` wrapping the result.
  - `Progress.update(self, text: str) -> None` (async) — edits the wrapped message's content to `text`.
  - `Progress.fail(self, text: str) -> None` (async) — edits the wrapped message's content to `text` (same mechanism as `update`, distinct name for call-site clarity).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_progress.py`:

```python
import os, asyncio
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import MagicMock, AsyncMock
from wren.progress import Progress

def _channel():
    sent_message = MagicMock()
    sent_message.edit = AsyncMock()
    channel = MagicMock()
    channel.send = AsyncMock(return_value=sent_message)
    return channel, sent_message

def test_start_sends_initial_text_and_returns_progress():
    channel, sent_message = _channel()
    progress = asyncio.run(Progress.start(channel, "Thinking…"))
    channel.send.assert_awaited_once_with("Thinking…")
    assert isinstance(progress, Progress)

def test_update_edits_the_same_message():
    channel, sent_message = _channel()
    progress = asyncio.run(Progress.start(channel, "Thinking…"))
    asyncio.run(progress.update("Almost done…"))
    sent_message.edit.assert_awaited_once_with(content="Almost done…")
    channel.send.assert_awaited_once()  # no second send from update()

def test_fail_edits_the_same_message():
    channel, sent_message = _channel()
    progress = asyncio.run(Progress.start(channel, "Thinking…"))
    asyncio.run(progress.fail("Something went wrong."))
    sent_message.edit.assert_awaited_once_with(content="Something went wrong.")
    channel.send.assert_awaited_once()  # no second send from fail()

def test_update_then_fail_both_target_same_message():
    channel, sent_message = _channel()
    progress = asyncio.run(Progress.start(channel, "Thinking…"))
    asyncio.run(progress.update("Almost done…"))
    asyncio.run(progress.fail("Something went wrong."))
    assert sent_message.edit.await_count == 2
    sent_message.edit.assert_awaited_with(content="Something went wrong.")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_progress.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'wren.progress'`)

- [ ] **Step 3: Write the implementation**

Create `wren/progress.py`:

```python
import discord

class Progress:
    def __init__(self, message: discord.Message):
        self._message = message

    @classmethod
    async def start(cls, channel, text: str) -> "Progress":
        msg = await channel.send(text)
        return cls(msg)

    async def update(self, text: str) -> None:
        await self._message.edit(content=text)

    async def fail(self, text: str) -> None:
        await self._message.edit(content=text)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_progress.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: PASS (no regressions — this task touches no existing file)

- [ ] **Step 6: Commit**

```bash
git add wren/progress.py tests/test_progress.py
git commit -m "feat: add Progress helper for edit-in-place status messages"
```
