import os
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")
os.environ.setdefault("TIMEZONE", "UTC")

import logging
from datetime import date
from unittest.mock import patch

import pytest

# F1: httpx logs "HTTP Request: GET <full url> ..." at INFO, and CALENDAR_URLS
# is a capability URL -- letting that line through at INFO writes the secret
# straight into the journal. wren.run must raise the httpx logger to WARNING
# at import, before anything else can log through it.
import wren.run  # noqa: E402  (import triggers the logging setup under test)

from wren import config
from wren.skills import calendar_skill


def test_httpx_logger_is_raised_to_warning_at_import():
    assert logging.getLogger("httpx").level == logging.WARNING


def test_a_secret_calendar_url_never_reaches_the_captured_log(monkeypatch, caplog):
    # Mimics what httpx itself does on a successful request: logs the full
    # URL at INFO on its own "httpx" logger. If that logger's level were not
    # raised, this line alone would put the private feed URL in the journal.
    def fake_get(url, **kwargs):
        logging.getLogger("httpx").info("HTTP Request: GET %s \"HTTP/1.1 200 OK\"", url)
        resp = type("Resp", (), {})()
        resp.text = "BEGIN:VCALENDAR\nEND:VCALENDAR\n"
        resp.raise_for_status = lambda: None
        return resp

    monkeypatch.setattr(config, "CALENDAR_URLS", ["https://cal.example/private-DEADBEEF/basic.ics"])
    calendar_skill._cache.clear()
    with patch.object(calendar_skill.httpx, "get", side_effect=fake_get):
        with caplog.at_level(logging.INFO):
            calendar_skill.events_between(date(2026, 8, 26), 1)
    for record in caplog.records:
        assert "private-DEADBEEF" not in record.getMessage()
