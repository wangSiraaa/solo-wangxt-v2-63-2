"""SQLite helpers: connection factory and an explicit transaction scope.

The connection runs in autocommit mode (``isolation_level=None``); every
multi-write operation must go through :func:`tx` so a failure rolls the
whole unit back -- this is what guarantees a failed approval never leaves
half a stopwatch (a pause row without its interval row, or vice versa).
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    """Run the wrapped statements atomically (BEGIN IMMEDIATE ... COMMIT)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"
