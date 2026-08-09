import os, asyncio
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from unittest.mock import MagicMock, AsyncMock, patch
from wren import config
from wren import router
from wren.communication import gmail_plugin as email_plugin

def _raw_email(from_addr: str, subject: str) -> bytes:
    return f"From: {from_addr}\r\nSubject: {subject}\r\n\r\nBody text.".encode()

def test_sender_address_plain():
    assert email_plugin._sender_address("alice@example.com") == "alice@example.com"

def test_sender_address_display_name_form():
    assert email_plugin._sender_address("Alice <Alice@Example.com>") == "alice@example.com"

def test_poll_once_notifies_on_match(monkeypatch):
    monkeypatch.setattr(config, "EMAIL_WATCH", {"alice@example.com": "owner"})
    imap_conn = MagicMock()
    imap_conn.search.return_value = ("OK", [b"1"])
    imap_conn.fetch.return_value = ("OK", [(b"1 (RFC822 {size})", _raw_email("alice@example.com", "Hello"))])

    with patch.object(router, "notify_name", new=AsyncMock(return_value=True)) as mock_notify:
        asyncio.run(email_plugin._poll_once(imap_conn))

    mock_notify.assert_awaited_once_with("owner", "Email from alice@example.com: Hello")
    imap_conn.store.assert_called_once_with(b"1", "+FLAGS", "\\Seen")

def test_poll_once_no_match_still_marks_seen(monkeypatch):
    monkeypatch.setattr(config, "EMAIL_WATCH", {"alice@example.com": "owner"})
    imap_conn = MagicMock()
    imap_conn.search.return_value = ("OK", [b"1"])
    imap_conn.fetch.return_value = ("OK", [(b"1 (RFC822 {size})", _raw_email("stranger@example.com", "Hi"))])

    with patch.object(router, "notify_name", new=AsyncMock()) as mock_notify:
        asyncio.run(email_plugin._poll_once(imap_conn))

    mock_notify.assert_not_called()
    imap_conn.store.assert_called_once_with(b"1", "+FLAGS", "\\Seen")

def test_poll_once_processes_multiple_messages(monkeypatch):
    monkeypatch.setattr(config, "EMAIL_WATCH", {"alice@example.com": "owner", "bob@example.com": "husband"})
    imap_conn = MagicMock()
    imap_conn.search.return_value = ("OK", [b"1 2"])
    imap_conn.fetch.side_effect = [
        ("OK", [(b"1 (RFC822 {size})", _raw_email("alice@example.com", "One"))]),
        ("OK", [(b"2 (RFC822 {size})", _raw_email("bob@example.com", "Two"))]),
    ]

    with patch.object(router, "notify_name", new=AsyncMock(return_value=True)) as mock_notify:
        asyncio.run(email_plugin._poll_once(imap_conn))

    assert mock_notify.await_count == 2

def test_start_returns_immediately_when_email_watch_empty(monkeypatch):
    monkeypatch.setattr(config, "EMAIL_WATCH", {})

    def _fail_connect():
        raise AssertionError("_connect should not be called when EMAIL_WATCH is empty")

    monkeypatch.setattr(email_plugin, "_connect", _fail_connect)

    asyncio.run(asyncio.wait_for(email_plugin.start(), timeout=1))
