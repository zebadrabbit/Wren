import os
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
