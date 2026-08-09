import asyncio

from wren.channel import Ctx, CollectingChannel


def test_send_accumulates_in_order():
    ch = CollectingChannel()

    async def go():
        await ch.send("first")
        await ch.send("second")
        await ch.send("third")

    asyncio.run(go())
    assert ch.sent == ["first", "second", "third"]


def test_send_starts_empty():
    assert CollectingChannel().sent == []


def test_send_file_accumulates_bytes_and_filename():
    ch = CollectingChannel()

    async def go():
        await ch.send_file(b"one", "a.txt")
        await ch.send_file(b"two", "b.png")

    asyncio.run(go())
    assert ch.files == [(b"one", "a.txt"), (b"two", "b.png")]


def test_send_file_starts_empty():
    assert CollectingChannel().files == []


def test_send_and_send_file_use_separate_lists():
    ch = CollectingChannel()

    async def go():
        await ch.send("text")
        await ch.send_file(b"data", "f.txt")

    asyncio.run(go())
    assert ch.sent == ["text"]
    assert ch.files == [(b"data", "f.txt")]


def test_ack_accumulates_states_in_order():
    ch = CollectingChannel()

    async def go():
        await ch.ack("seen")
        await ch.ack("done")

    asyncio.run(go())
    assert ch.acks == ["seen", "done"]


def test_ack_starts_empty():
    assert CollectingChannel().acks == []


def test_history_returns_none():
    assert asyncio.run(CollectingChannel().history()) is None


def test_history_returns_none_with_explicit_limit():
    assert asyncio.run(CollectingChannel().history(limit=50)) is None


def test_collecting_channels_do_not_share_state():
    a, b = CollectingChannel(), CollectingChannel()

    async def go():
        await a.send("only a")
        await a.send_file(b"x", "x.txt")
        await a.ack("done")

    asyncio.run(go())
    assert b.sent == []
    assert b.files == []
    assert b.acks == []


def test_ctx_defaults():
    ctx = Ctx(user_id=1, channel=CollectingChannel())
    assert ctx.content == ""
    assert ctx.tags == []
    assert ctx.person is None
    assert ctx.when is None


def test_ctx_keeps_required_fields():
    ch = CollectingChannel()
    ctx = Ctx(user_id=42, channel=ch)
    assert ctx.user_id == 42
    assert ctx.channel is ch


def test_ctx_accepts_explicit_values():
    ctx = Ctx(
        user_id=7,
        channel=CollectingChannel(),
        content="buy milk",
        tags=["grocery"],
        person="hubby",
        when="tomorrow at 5pm",
    )
    assert ctx.content == "buy milk"
    assert ctx.tags == ["grocery"]
    assert ctx.person == "hubby"
    assert ctx.when == "tomorrow at 5pm"


def test_ctx_tags_default_is_not_shared_between_instances():
    # regression: a bare `tags: list[str] = []` default would make every Ctx
    # share one list, so one plugin's tags would leak into the next request.
    first = Ctx(user_id=1, channel=CollectingChannel())
    second = Ctx(user_id=2, channel=CollectingChannel())
    first.tags.append("grocery")
    assert first.tags == ["grocery"]
    assert second.tags == []


def test_ctx_tags_default_is_fresh_for_later_instances():
    first = Ctx(user_id=1, channel=CollectingChannel())
    first.tags.append("grocery")
    assert Ctx(user_id=3, channel=CollectingChannel()).tags == []
