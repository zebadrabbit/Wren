import contextlib
import os
import sqlite3

DEFAULT_PATH = "wren.db"

def path() -> str:
    # read at call time, not import time, so tests (and anything else that
    # sets WREN_DB after import) actually get the database they asked for
    return os.environ.get("WREN_DB") or DEFAULT_PATH

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
