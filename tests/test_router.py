import os
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

import asyncio
import logging

import pytest

from wren import config, router


class FakeSurface:
    """Stands in for wren.communication.discord_plugin / .http_plugin —
    anything with an `async notify(user_id, text) -> bool`."""

    def __init__(self, result=True, raises=None):
        self.calls = []
        self.result = result
        self.raises = raises

    async def notify(self, user_id, text):
        self.calls.append((user_id, text))
        if self.raises is not None:
            raise self.raises
        return self.result


@pytest.fixture(autouse=True)
def clean_registry():
    # the registry is module-level global state; wipe it either side of every
    # test so registrations cannot leak between them.
    router.reset()
    yield
    router.reset()


def test_registered_is_empty_by_default():
    assert router.registered() == []


def test_register_then_registered_lists_the_name():
    router.register("discord", FakeSurface())
    assert router.registered() == ["discord"]


def test_registered_is_sorted():
    router.register("http", FakeSurface())
    router.register("discord", FakeSurface())
    assert router.registered() == ["discord", "http"]


def test_register_same_name_twice_replaces():
    first, second = FakeSurface(), FakeSurface()
    router.register("discord", first)
    router.register("discord", second)
    assert router.registered() == ["discord"]


def test_reset_clears_the_registry():
    router.register("discord", FakeSurface())
    router.reset()
    assert router.registered() == []


def test_notify_returns_false_when_surface_not_registered(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_VIA", "discord")
    assert asyncio.run(router.notify(1, "hello")) is False


def test_notify_logs_a_warning_when_surface_not_registered(monkeypatch, caplog):
    monkeypatch.setattr(config, "NOTIFY_VIA", "discord")
    with caplog.at_level(logging.WARNING):
        asyncio.run(router.notify(1, "hello"))
    assert "discord" in caplog.text
    assert "not registered" in caplog.text


def test_notify_warning_names_the_registered_surfaces(monkeypatch, caplog):
    monkeypatch.setattr(config, "NOTIFY_VIA", "discord")
    router.register("http", FakeSurface())
    with caplog.at_level(logging.WARNING):
        asyncio.run(router.notify(1, "hello"))
    assert "http" in caplog.text


def test_notify_does_not_use_an_unconfigured_surface(monkeypatch):
    # registering "http" must not satisfy a NOTIFY_VIA of "discord"
    monkeypatch.setattr(config, "NOTIFY_VIA", "discord")
    other = FakeSurface()
    router.register("http", other)
    asyncio.run(router.notify(1, "hello"))
    assert other.calls == []


def test_notify_dispatches_to_the_registered_surface(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_VIA", "discord")
    surface = FakeSurface()
    router.register("discord", surface)
    asyncio.run(router.notify(99, "reminder time"))
    assert surface.calls == [(99, "reminder time")]


def test_notify_returns_the_surfaces_value(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_VIA", "discord")
    router.register("discord", FakeSurface(result=True))
    assert asyncio.run(router.notify(1, "hello")) is True


def test_notify_propagates_a_false_result_from_the_surface(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_VIA", "discord")
    router.register("discord", FakeSurface(result=False))
    assert asyncio.run(router.notify(1, "hello")) is False


def test_notify_propagates_transient_failures_instead_of_swallowing_them(monkeypatch):
    # Regression guard. router.notify used to `except Exception: return False`,
    # which turned a Discord 503 or a network blip into a permanent-failure
    # signal. Callers CONSUME the message on False (reminder_skill marks the
    # reminder fired, gmail_plugin flags the mail \Seen, github_plugin advances
    # its watermark), so swallowing destroyed the message instead of retrying
    # it on the next poll. False must mean "permanent"; transient must raise.
    monkeypatch.setattr(config, "NOTIFY_VIA", "discord")
    router.register("discord", FakeSurface(raises=RuntimeError("Discord 503")))
    with pytest.raises(RuntimeError, match="Discord 503"):
        asyncio.run(router.notify(1, "hello"))


def test_notify_honours_a_different_notify_surface(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_VIA", "http")
    discord, http = FakeSurface(), FakeSurface()
    router.register("discord", discord)
    router.register("http", http)
    asyncio.run(router.notify(5, "hello"))
    assert discord.calls == []
    assert http.calls == [(5, "hello")]


def test_notify_name_resolves_an_alias_via_whitelist(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_VIA", "discord")
    monkeypatch.setattr(config, "whitelist", lambda: {"owner": 1, "hubby": 222})
    surface = FakeSurface()
    router.register("discord", surface)
    assert asyncio.run(router.notify_name("hubby", "dinner's ready")) is True
    assert surface.calls == [(222, "dinner's ready")]


def test_notify_name_is_case_insensitive(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_VIA", "discord")
    monkeypatch.setattr(config, "whitelist", lambda: {"hubby": 222})
    surface = FakeSurface()
    router.register("discord", surface)
    asyncio.run(router.notify_name("Hubby", "hi"))
    assert surface.calls == [(222, "hi")]


def test_notify_name_returns_false_for_unknown_alias(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_VIA", "discord")
    monkeypatch.setattr(config, "whitelist", lambda: {"owner": 1})
    router.register("discord", FakeSurface())
    assert asyncio.run(router.notify_name("nobody", "hi")) is False


def test_notify_name_does_not_deliver_for_unknown_alias(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_VIA", "discord")
    monkeypatch.setattr(config, "whitelist", lambda: {"owner": 1})
    surface = FakeSurface()
    router.register("discord", surface)
    asyncio.run(router.notify_name("nobody", "hi"))
    assert surface.calls == []


def test_notify_name_returns_false_for_empty_alias(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_VIA", "discord")
    monkeypatch.setattr(config, "whitelist", lambda: {"owner": 1})
    surface = FakeSurface()
    router.register("discord", surface)
    assert asyncio.run(router.notify_name("", "hi")) is False
    assert surface.calls == []


def test_notify_name_returns_false_when_surface_not_registered(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_VIA", "discord")
    monkeypatch.setattr(config, "whitelist", lambda: {"hubby": 222})
    assert asyncio.run(router.notify_name("hubby", "hi")) is False
