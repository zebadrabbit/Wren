from dataclasses import dataclass, field
from typing import Protocol


class Channel(Protocol):
    """How a surface talks back. Implemented by each surface; capability
    plugins only ever see this, never a discord.Message."""

    async def send(self, text: str) -> None: ...

    async def send_file(self, data: bytes, filename: str) -> None: ...

    async def history(self, limit: int = 10) -> list[dict] | None: ...

    async def ack(self, state: str) -> None:
        """state is "seen" | "done" | "error". Discord draws these as
        reactions; surfaces with no such concept no-op."""
        ...


@dataclass
class Ctx:
    """Everything a plugin handler needs. Replaces the old
    (message, client, user_id, content, tags, person, when) argument list."""

    user_id: int
    channel: Channel
    content: str = ""
    tags: list[str] = field(default_factory=list)
    person: str | None = None
    when: str | None = None


class CollectingChannel:
    """Accumulates output instead of sending it. Used by the HTTP surface to
    build a JSON response, and by tests to assert on plain lists rather than
    mocking Discord."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.files: list[tuple[bytes, str]] = []
        self.acks: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def send_file(self, data: bytes, filename: str) -> None:
        self.files.append((data, filename))

    async def history(self, limit: int = 10) -> list[dict] | None:
        # ponytail: no conversation history off Discord — voice/HTTP requests
        # are one-shot. Add a history table if multi-turn ever matters.
        return None

    async def ack(self, state: str) -> None:
        self.acks.append(state)
