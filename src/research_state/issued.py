"""Fragments the server has handed to a model — the evidence side of a citation.

# ANCHOR: issued
# Role: every fragment returned by fragments_for is stored here, so a brief can
# later be checked against what was actually issued.
# In: url, the page's fetch time, one fragment dict from fragments.extract().
# Out: the same dict plus {fragment_id} — 16 hex chars of sha256 over
# url + char_start + fetched_at.
# The stored shape follows W3C Web Annotation Selectors: an exact text snapshot,
# prefix/suffix for re-anchoring, and offsets. The snapshot is deliberate — pages
# change, and a bare hash can only ever say "no match"
# (docs/research/2026-07-26-fragment-provenance.md).
"""

from __future__ import annotations

import hashlib
import sqlite3

import structlog

from . import db

log = structlog.get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS issued_fragments (
    fragment_id TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    exact       TEXT NOT NULL,
    prefix      TEXT NOT NULL DEFAULT '',
    suffix      TEXT NOT NULL DEFAULT '',
    char_start  INTEGER NOT NULL,
    char_end    INTEGER NOT NULL,
    fetched_at  INTEGER NOT NULL,
    issued_at   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS issued_fragments_url_idx ON issued_fragments(url);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    db.write(conn, lambda c: c.executescript(SCHEMA))


def make_id(url: str, char_start: int, fetched_at: int) -> str:
    """Same page, same offset, same fetch — same id. Any change gives a new one."""
    raw = f"{url}\x00{char_start}\x00{fetched_at}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def record(conn: sqlite3.Connection, url: str, fetched_at: int, fragment: dict) -> dict:
    """Store one issued fragment and return it with its id attached."""
    fragment_id = make_id(url, fragment["char_start"], fetched_at)
    row = (
        fragment_id,
        url,
        fragment["text"],
        fragment.get("prefix", ""),
        fragment.get("suffix", ""),
        fragment["char_start"],
        fragment["char_end"],
        fetched_at,
    )
    db.write(
        conn,
        lambda c: c.execute(
            "INSERT INTO issued_fragments"
            " (fragment_id, url, exact, prefix, suffix, char_start, char_end,"
            "  fetched_at, issued_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%s','now'))"
            " ON CONFLICT(fragment_id) DO NOTHING",
            row,
        ),
    )
    return {**fragment, "fragment_id": fragment_id}


def get(conn: sqlite3.Connection, fragment_id: str) -> dict | None:
    """The stored snapshot of one fragment, or None if we never issued it."""
    row = conn.execute(
        "SELECT * FROM issued_fragments WHERE fragment_id = ?", (fragment_id,)
    ).fetchone()
    return dict(row) if row is not None else None
