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

    async def send_card(self, kind: str, data: dict, text: str,
                        *, intent: str = "", params: dict | None = None) -> None:
        """Structured data a surface may draw as an interactive card.

        `text` is the prose the skill would otherwise have sent, verbatim.
        Surfaces that cannot draw a card send exactly that, so a skill adopting
        a card is never a regression on Discord or Telegram. `data` is the rows
        to draw; it is NOT persisted anywhere — see the design doc on why cards
        are live.

        `intent` and `params` are how the card refreshes itself: they are what a
        surface stores so it can re-run this same read later. Only the skill
        knows them, which is why they are arguments and not something the web
        surface could infer from `kind`. Keyword-only with defaults, so a
        surface that only prints prose never has to think about them.
        """
        ...


@dataclass
class Ctx:
    """Everything a plugin handler needs. Replaces the old
    (message, client, user_id, content, tags, person, when) argument list."""

    user_id: int
    channel: Channel
    content: str = ""
    # the user's words, unmodified — `content` is the model's extraction of them
    text: str = ""
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
        self.cards: list[dict] = []

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

    async def send_card(self, kind: str, data: dict, text: str,
                        *, intent: str = "", params: dict | None = None) -> None:
        # A dict, not a tuple, so intent/params are visible to a skill test.
        # They are the part a skill is most likely to get wrong -- a card with
        # the wrong intent looks perfect until the page reloads and cannot
        # refresh it.
        self.cards.append({"kind": kind, "data": data,
                           "intent": intent, "params": params or {}, "text": text})
        # both, deliberately: a test may assert on the structure, and every
        # existing test that asserts on prose keeps passing unchanged
        await self.send(text)
