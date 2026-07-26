import sqlite3
import time

import pytest

from research_state import db


def test_connect_enables_wal_and_busy_timeout(tmp_path):
    conn = db.connect(tmp_path / "state.sqlite")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db.BUSY_TIMEOUT_MS
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_connect_creates_parent_directory(tmp_path):
    path = tmp_path / "nested" / "deeper" / "state.sqlite"
    db.connect(path)
    assert path.exists()


def test_rows_are_mappings(tmp_path):
    conn = db.connect(tmp_path / "state.sqlite")
    conn.execute("CREATE TABLE t (a TEXT)")
    conn.execute("INSERT INTO t VALUES ('x')")
    assert conn.execute("SELECT a FROM t").fetchone()["a"] == "x"


def test_write_retries_while_locked(tmp_path, monkeypatch):
    calls = {"n": 0}

    def flaky(conn):
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "written"

    monkeypatch.setattr(db.time, "sleep", lambda _: None)
    conn = db.connect(tmp_path / "state.sqlite")
    assert db.write(conn, flaky) == "written"
    assert calls["n"] == 3


def test_write_gives_up_after_max_attempts(tmp_path, monkeypatch):
    def always_locked(conn):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db.time, "sleep", lambda _: None)
    conn = db.connect(tmp_path / "state.sqlite")
    with pytest.raises(sqlite3.OperationalError):
        db.write(conn, always_locked)


def test_write_does_not_retry_other_errors(tmp_path):
    def broken(conn):
        raise sqlite3.OperationalError("no such table: nope")

    conn = db.connect(tmp_path / "state.sqlite")
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        db.write(conn, broken)


def test_connection_is_usable_from_another_thread(tmp_path):
    """FastMCP runs sync tools in a worker thread — a per-module connection must survive that."""
    import threading

    conn = db.connect(tmp_path / "state.sqlite")
    conn.execute("CREATE TABLE t (a TEXT)")
    conn.commit()
    errors: list[Exception] = []

    def worker():
        try:
            db.write(conn, lambda c: c.execute("INSERT INTO t VALUES ('from-thread')"))
        except Exception as exc:
            errors.append(exc)

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert not errors
    assert conn.execute("SELECT a FROM t").fetchone()["a"] == "from-thread"


def test_concurrent_writers_do_not_interleave(tmp_path):
    """Two threads writing two rows each must never commit a half-finished transaction."""
    import threading

    conn = db.connect(tmp_path / "state.sqlite")
    conn.execute("CREATE TABLE t (tag TEXT)")
    conn.commit()

    def pair(tag: str):
        def op(c):
            c.execute("INSERT INTO t VALUES (?)", (tag,))
            time.sleep(0.01)
            c.execute("INSERT INTO t VALUES (?)", (tag,))

        db.write(conn, op)

    threads = [threading.Thread(target=pair, args=(t,)) for t in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    tags = [r["tag"] for r in conn.execute("SELECT tag FROM t")]
    assert sorted(tags) == ["a", "a", "b", "b"]
    assert tags[0] == tags[1] and tags[2] == tags[3]  # not interleaved


def test_write_commits_and_rolls_back(tmp_path):
    conn = db.connect(tmp_path / "state.sqlite")
    conn.execute("CREATE TABLE t (a TEXT)")
    conn.commit()

    db.write(conn, lambda c: c.execute("INSERT INTO t VALUES ('kept')"))

    def boom(c):
        c.execute("INSERT INTO t VALUES ('dropped')")
        raise ValueError("nope")

    with pytest.raises(ValueError):
        db.write(conn, boom)

    rows = [r["a"] for r in conn.execute("SELECT a FROM t")]
    assert rows == ["kept"]
