"""Telegram communication plugin — STUB.

Not wired into `run.py` / `registry.py` yet. This file documents the shape
a new chat-capable communication plugin takes; fill in `start()`/`notify()`
and a `TelegramChannel` (mirroring `discord_plugin.py`) to bring it live,
then add "telegram" to `COMMUNICATION_PLUGINS` in .env.

Role: chat (input + output) — like discord_plugin.py, not gmail_plugin.py.
"""

ROLE = "chat"          # "chat" | "input" | "output"
PLUGIN_NAME = "Telegram"
CAN_NOTIFY = True

async def start() -> None:
    raise NotImplementedError("wren/communication/telegram_plugin.py is a stub — see PROJECT_PLAN.md")

async def notify(user_id: int, text: str) -> bool:
    raise NotImplementedError("wren/communication/telegram_plugin.py is a stub — see PROJECT_PLAN.md")
