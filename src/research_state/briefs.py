"""Briefs: claim validation, markdown rendering, and the searchable library.

# ANCHOR: briefs
# Role: the invariant lives here — a factual claim without a verbatim quote from
# a fragment this server issued cannot be stored.
# In: a connection plus claim dicts {text, kind, fragment_id, quote}.
# Out: validate_claims -> list of problems (empty means valid).
# Quote matching is literal after whitespace/case normalisation only. Checking
# merely that "a citation exists" is worth almost nothing: measured link validity
# runs above 94% while actual factual support is 39-77%
# (docs/research/2026-07-26-citation-enforcement.md).
"""

from __future__ import annotations

import re
import sqlite3

import structlog

from . import db, issued

log = structlog.get_logger(__name__)

KINDS = ("fact", "assumption")
_WS = re.compile(r"\s+")

SCHEMA = """
CREATE TABLE IF NOT EXISTS briefs (
    brief_id TEXT PRIMARY KEY,
    job_id   TEXT NOT NULL,
    topic    TEXT NOT NULL,
    summary  TEXT NOT NULL,
    path     TEXT NOT NULL,
    created  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS brief_claims (
    brief_id    TEXT NOT NULL REFERENCES briefs(brief_id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,
    text        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    fragment_id TEXT,
    quote       TEXT,
    PRIMARY KEY (brief_id, idx)
);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    db.write(conn, lambda c: c.executescript(SCHEMA))


def _normalise(text: str) -> str:
    """Whitespace and case only — never punctuation. The check must stay literal."""
    return _WS.sub(" ", text or "").strip().casefold()


def validate_claims(conn: sqlite3.Connection, claims: list[dict]) -> list[dict]:
    """Return one problem per unacceptable claim. An empty list means valid."""
    problems: list[dict] = []
    for index, claim in enumerate(claims):
        kind = claim.get("kind", "fact")
        if kind not in KINDS:
            problems.append({"index": index, "reason": "unknown_kind", "kind": kind})
            continue
        if kind == "assumption":
            continue
        fragment_id, quote = claim.get("fragment_id"), claim.get("quote")
        if not fragment_id or not quote:
            problems.append(
                {
                    "index": index,
                    "reason": "missing_citation",
                    "text": claim.get("text"),
                }
            )
            continue
        fragment = issued.get(conn, fragment_id)
        if fragment is None:
            problems.append(
                {
                    "index": index,
                    "reason": "unknown_fragment",
                    "fragment_id": fragment_id,
                }
            )
            continue
        if _normalise(quote) not in _normalise(fragment["exact"]):
            problems.append(
                {
                    "index": index,
                    "reason": "quote_not_found",
                    "fragment_id": fragment_id,
                    "quote": quote,
                }
            )
    return problems
