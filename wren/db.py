import contextlib
import contextvars
import os
import shutil
import sqlite3
import tempfile

DEFAULT_PATH = "wren.db"

# A ContextVar, not a global: it follows one asyncio task (and, because
# asyncio.to_thread copies the context, that task's worker threads) and
# nothing else. A Discord message arriving during a dry run on the HTTP
# surface still writes to the real file.
_override: contextvars.ContextVar[str | None] = contextvars.ContextVar("wren_db_override", default=None)


def path() -> str:
    # read at call time, not import time, so tests (and anything else that
    # sets WREN_DB after import) actually get the database they asked for
    return _override.get() or os.environ.get("WREN_DB") or DEFAULT_PATH


def in_dry_run() -> bool:
    return _override.get() is not None


@contextlib.contextmanager
def dry_run():
    """Run the body against a throwaway snapshot of the current database.

    Reads see everything the real file held at entry; writes go to the copy,
    which is deleted on exit. The snapshot is taken with sqlite's backup API
    so a write in flight on another task cannot leave it half-copied.
    """
    real = path()
    tmpdir = tempfile.mkdtemp(prefix="wren-dry-")
    snapshot = os.path.join(tmpdir, "snapshot.db")
    if os.path.exists(real):
        src, dst = sqlite3.connect(real), sqlite3.connect(snapshot)
        try:
            src.backup(dst)
        finally:
            src.close(); dst.close()
    token = _override.set(snapshot)
    try:
        yield
    finally:
        _override.reset(token)
        shutil.rmtree(tmpdir, ignore_errors=True)

@contextlib.contextmanager
def conn():
    """Yields a connection, commits on success, rolls back on error, and
    always closes.

    sqlite3's own context manager commits/rolls back but does NOT close, so
    `with sqlite3.connect(...) as c:` leaks the handle until GC. Every storage
    module opens a connection per call, so on a long-running service that
    adds up.
    """
    connection = sqlite3.connect(path())
    try:
        with connection:
            yield connection
    finally:
        connection.close()
