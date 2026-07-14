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
