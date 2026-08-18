import io
import logging
import sys

import discord

from .. import config
from .. import core
from .. import router
from . import discord_utils

_ACK_EMOJI = {"seen": "👀", "done": "✅", "error": "❌"}

ROLE = "chat"   # chat: full input + output, DM-based conversation
PLUGIN_NAME = "Discord"
CAN_NOTIFY = True

_client: discord.Client | None = None


class DiscordChannel:
    """Channel implementation backed by a discord.Message."""

    def __init__(self, message: discord.Message, client: discord.Client):
        self._message = message
        self._client = client

    async def send(self, text: str) -> None:
        await self._message.channel.send(text)

    async def send_file(self, data: bytes, filename: str) -> None:
        await self._message.channel.send(file=discord.File(io.BytesIO(data), filename=filename))

    async def send_card(self, kind: str, data: dict, text: str,
                        *, intent: str = "", params: dict | None = None) -> None:
        # Discord could draw an embed, but a card is interactive and an embed is
        # not; prose is the honest degradation rather than a half-card.
        await self.send(text)

    async def history(self, limit: int = 10) -> list[dict] | None:
        try:
            raw = [m async for m in self._message.channel.history(limit=limit, before=self._message)]
            return discord_utils.history_to_messages(raw, self._client.user.id)
        except Exception as e:
            logging.warning(f"Could not fetch history: {e}")
            return None

    async def ack(self, state: str) -> None:
        emoji = _ACK_EMOJI.get(state)
        if not emoji:
            return
        try:
            await self._message.add_reaction(emoji)
        except Exception as e:
            logging.warning(f"Could not react with {emoji}: {e}")


def build_client() -> discord.Client:
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        logging.info(f"Wren online as {client.user}")

    @client.event
    async def on_message(message: discord.Message):
        if message.author == client.user:
            return
        if not isinstance(message.channel, discord.DMChannel):
            return
        await core.handle_message(
            message.author.id, message.content, DiscordChannel(message, client)
        )

    return client


async def notify(user_id: int, text: str) -> bool:
    if _client is None:
        logging.warning("Discord surface not started; cannot notify.")
        return False
    # A reminder can come due before the gateway handshake finishes. Without
    # this the first poll after boot would fail to deliver and still mark the
    # reminder fired.
    await _client.wait_until_ready()
    return await discord_utils.notify_id(_client, user_id, text)


async def start() -> None:
    global _client
    if not config.DISCORD_TOKEN:
        raise RuntimeError(
            "COMMUNICATION_PLUGINS includes 'discord' but DISCORD_TOKEN is not set. "
            "Set it, or drop 'discord' from COMMUNICATION_PLUGINS."
        )
    _client = build_client()
    router.register("discord", sys.modules[__name__])
    await _client.start(config.DISCORD_TOKEN)
