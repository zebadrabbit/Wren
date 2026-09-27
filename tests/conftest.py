import os
import sys
import tempfile

import pytest

# Set before any `wren.*` import so that even a module-level or import-time
# database touch cannot land on the real wren.db. The per-test fixture below
# then narrows this to a fresh file per test.
#
# This is deliberately the ONLY env var conftest sets: test modules configure
# LLM_PROVIDERS/WREN_OWNER_ID/etc. themselves with setdefault, and some of them
# (test_providers.py) need values that differ from the rest of the suite.
os.environ["WREN_DB"] = os.path.join(tempfile.gettempdir(), "wren-tests-import-guard.db")


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Every test gets its own database. Nothing in the suite may ever open
    the real wren.db."""
    monkeypatch.setenv("WREN_DB", str(tmp_path / "wren.db"))
    yield


@pytest.fixture(autouse=True)
def no_env_locked_settings(monkeypatch):
    """No setting starts out locked by .env.

    python-dotenv searches parent directories, so the suite loads the LIVE
    .env (from a worktree too) and config.ENV_DEFINED would lock whatever
    keys it happens to define -- tests passing or failing by the owner's
    .env. A test that wants a lock sets ENV_DEFINED itself. Only patched if
    config is already imported: importing it here would run it before a test
    module's own os.environ.setdefault(...) lines.
    """
    config = sys.modules.get("wren.config")
    if config is not None:
        monkeypatch.setattr(config, "ENV_DEFINED", frozenset())
    yield
