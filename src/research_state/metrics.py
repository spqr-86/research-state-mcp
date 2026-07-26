"""How much page text never reached the client's context.

# ANCHOR: metrics
# Role: one row per fragments_for call; stats() sums them.
# In: totals and returned amounts for one page. Out: cumulative figures.
# Why from day one: this is the only non-rhetorical evidence that the project is
# worth anything, and it cannot be backfilled after the fact.
"""

from __future__ import annotations

import sqlite3

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS fetch_metrics (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    url                 TEXT NOT NULL,
    paragraphs_total    INTEGER NOT NULL,
    paragraphs_returned INTEGER NOT NULL,
    chars_total         INTEGER NOT NULL,
    chars_returned      INTEGER NOT NULL,
    at                  INTEGER NOT NULL
);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    db.write(conn, lambda c: c.executescript(SCHEMA))


def record_fetch(
    conn: sqlite3.Connection,
    url: str,
    paragraphs_total: int,
    paragraphs_returned: int,
    chars_total: int,
    chars_returned: int,
) -> None:
    db.write(
        conn,
        lambda c: c.execute(
            "INSERT INTO fetch_metrics"
            " (url, paragraphs_total, paragraphs_returned, chars_total, chars_returned, at)"
            " VALUES (?, ?, ?, ?, ?, strftime('%s','now'))",
            (url, paragraphs_total, paragraphs_returned, chars_total, chars_returned),
        ),
    )


def stats(conn: sqlite3.Connection) -> dict:
    """Cumulative saved-context figures across every fragments_for call."""
    row = conn.execute(
        "SELECT COUNT(*) AS fetches,"
        " COALESCE(SUM(chars_total), 0) AS chars_total,"
        " COALESCE(SUM(chars_returned), 0) AS chars_returned"
        " FROM fetch_metrics"
    ).fetchone()
    total, returned = row["chars_total"], row["chars_returned"]
    return {
        "fetches": row["fetches"],
        "chars_total": total,
        "chars_returned": returned,
        "chars_saved": total - returned,
        "saved_ratio": round((total - returned) / total, 4) if total else 0.0,
    }
