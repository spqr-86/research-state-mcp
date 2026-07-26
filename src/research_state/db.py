"""SQLite connection setup and the write-retry layer.

# ANCHOR: db
# Role: every connection to our own state DB is opened here, nowhere else.
# In: path to the sqlite file. Out: a configured sqlite3.Connection.
# Key params: WAL (concurrent readers), busy_timeout, CONNECT_TIMEOUT_S,
# and write() which retries on "database is locked".
# Why: Claude Code calls tools in parallel; WAL alone does not remove the
# single-writer lock, so writes need a retry layer on top of it.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import structlog

BUSY_TIMEOUT_MS = 5000
CONNECT_TIMEOUT_S = 10.0
WRITE_ATTEMPTS = 5
WRITE_BACKOFF_S = 0.05

# FastMCP runs sync tools in a worker thread, and a client can have several in
# flight, so one connection is shared across threads. sqlite3 tolerates that
# (threadsafety 3), but an implicit transaction does not: two interleaved
# write() calls would commit each other's half-finished work. One writer at a
# time, enforced here.
_write_lock = threading.RLock()

log = structlog.get_logger(__name__)

T = TypeVar("T")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open our state DB with the pragmas this project depends on."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=CONNECT_TIMEOUT_S, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def write(conn: sqlite3.Connection, fn: Callable[[sqlite3.Connection], T]) -> T:
    """Run `fn` as one transaction, retrying only on lock contention.

    Never do network I/O inside `fn`: a long write transaction blocks WAL
    checkpointing.
    """
    with _write_lock:
        return _write_locked(conn, fn)


def _write_locked(conn: sqlite3.Connection, fn: Callable[[sqlite3.Connection], T]) -> T:
    for attempt in range(1, WRITE_ATTEMPTS + 1):
        try:
            result = fn(conn)
        except sqlite3.OperationalError as exc:
            conn.rollback()
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            if attempt == WRITE_ATTEMPTS:
                log.error("db.write.giving_up", attempts=attempt, error=str(exc))
                raise
            log.warning("db.write.locked", attempt=attempt)
            time.sleep(WRITE_BACKOFF_S * attempt)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            return result
    raise AssertionError("unreachable")
