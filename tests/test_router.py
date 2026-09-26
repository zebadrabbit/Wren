import os
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

import asyncio
import logging

import pytest

from wren import config, contacts, router


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
    # notify() consults contacts to route a person to their surface, and
    # run.py creates that table at startup; here nothing else does.
    contacts.init_db()
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


def test_notify_via_overrides_the_default_surface(monkeypatch):
    default, other = FakeSurface(), FakeSurface()
    router.register("telegram", default)
    router.register("discord", other)
    monkeypatch.setattr(config, "NOTIFY_VIA", "telegram")

    assert asyncio.run(router.notify(7, "mythics", via="discord")) is True
    assert other.calls == [(7, "mythics")]
    assert default.calls == []


def test_notify_without_via_still_uses_the_default_surface(monkeypatch):
    default = FakeSurface()
    router.register("telegram", default)
    monkeypatch.setattr(config, "NOTIFY_VIA", "telegram")

    assert asyncio.run(router.notify(7, "hello")) is True
    assert default.calls == [(7, "hello")]


def test_notify_via_an_unregistered_surface_is_a_permanent_failure(monkeypatch):
    router.register("telegram", FakeSurface())
    monkeypatch.setattr(config, "NOTIFY_VIA", "telegram")
    assert asyncio.run(router.notify(7, "hello", via="discord")) is False


def test_notify_is_a_silent_success_in_a_dry_run(monkeypatch):
    # A dry run must never DM a real person; True (not False) so a caller
    # under test still takes its "delivered" branch.
    from wren import db
    surface = FakeSurface()
    router.register("discord", surface)
    monkeypatch.setattr(config, "NOTIFY_VIA", "discord")
    with db.dry_run():
        assert asyncio.run(router.notify(1, "hi")) is True
    assert surface.calls == []


# --- notifications follow the person --------------------------------------
# With no explicit `via`, a contact who has no id on the default surface but
# is registered on another running one is reached there. The owner is not a
# contact, so the owner's routing is untouched.

from wren import contacts

@pytest.fixture
def hubby_on_telegram():
    contacts.init_db()
    contacts.add("hubby", 42, surface="telegram")
    return 42

def test_a_telegram_only_contact_is_notified_on_telegram(monkeypatch, hubby_on_telegram):
    monkeypatch.setattr(config, "NOTIFY_VIA", "discord")
    discord, telegram = FakeSurface(), FakeSurface()
    router.register("discord", discord)
    router.register("telegram", telegram)
    assert asyncio.run(router.notify(42, "bins")) is True
    assert discord.calls == []
    assert telegram.calls == [(42, "bins")]

def test_the_default_surface_wins_when_the_contact_is_on_it(monkeypatch, hubby_on_telegram):
    monkeypatch.setattr(config, "NOTIFY_VIA", "discord")
    contacts.set_id("hubby", "discord", 222)
    discord, telegram = FakeSurface(), FakeSurface()
    router.register("discord", discord)
    router.register("telegram", telegram)
    asyncio.run(router.notify(222, "bins"))
    assert discord.calls == [(222, "bins")] and telegram.calls == []

def test_an_explicit_via_is_not_second_guessed(monkeypatch, hubby_on_telegram):
    monkeypatch.setattr(config, "NOTIFY_VIA", "discord")
    discord, telegram = FakeSurface(), FakeSurface()
    router.register("discord", discord)
    router.register("telegram", telegram)
    asyncio.run(router.notify(42, "bins", via="discord"))
    assert discord.calls == [(42, "bins")] and telegram.calls == []

def test_falls_back_to_the_default_when_their_surface_is_not_running(monkeypatch, hubby_on_telegram):
    monkeypatch.setattr(config, "NOTIFY_VIA", "discord")
    discord = FakeSurface()
    router.register("discord", discord)
    asyncio.run(router.notify(42, "bins"))
    assert discord.calls == [(42, "bins")]
