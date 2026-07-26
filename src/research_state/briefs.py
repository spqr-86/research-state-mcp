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

import hashlib
import re
import sqlite3
import time
from pathlib import Path

import structlog

from . import db, issued
from . import fragments as fragments_module

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

CREATE VIRTUAL TABLE IF NOT EXISTS briefs_fts USING fts5(
    brief_id UNINDEXED,
    topic,
    summary,
    claims,
    tokenize='unicode61'
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


class InvalidBrief(ValueError):
    """The brief has claims that cannot be stored. `problems` says which and why."""

    def __init__(self, problems: list[dict]) -> None:
        super().__init__(f"{len(problems)} unacceptable claim(s)")
        self.problems = problems


def _slug(topic: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", topic, flags=re.UNICODE).strip().lower()
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug[:60] or "brief"


def _unique_path(brief_dir: Path, today: str, topic: str) -> Path:
    """Never overwrite an existing brief — a second run is a second document."""
    brief_dir.mkdir(parents=True, exist_ok=True)
    base = f"{today}-{_slug(topic)}"
    path = brief_dir / f"{base}.md"
    counter = 2
    while path.exists():
        path = brief_dir / f"{base}-{counter}.md"
        counter += 1
    return path


def render(
    topic: str, summary: str, claims: list[dict], gaps: list[str], sources: dict
) -> str:
    """The markdown a human reads. Generated, never hand-written."""
    lines = [f"# {topic}", "", summary, ""]
    if claims:
        lines += ["## Claims", ""]
        for claim in claims:
            if claim.get("kind") == "assumption":
                lines.append(f"- {claim['text']} _(assumption — no source)_")
                continue
            url = sources.get(claim["fragment_id"], "")
            lines.append(f"- {claim['text']}")
            lines.append(f"  > {claim['quote']}")
            lines.append(f"  — <{url}> `{claim['fragment_id']}`")
        lines.append("")
    if gaps:
        lines += ["## Gaps", ""] + [f"- {gap}" for gap in gaps] + [""]
    if sources:
        lines += ["## Sources", ""] + [
            f"- <{url}>" for url in sorted(set(sources.values()))
        ]
    return "\n".join(lines).rstrip() + "\n"


def save(
    conn: sqlite3.Connection,
    job_id: str,
    topic: str,
    summary: str,
    claims: list[dict],
    gaps: list[str],
    brief_dir: Path,
    today: str,
) -> dict:
    """Validate, write the markdown file, index it. Raises InvalidBrief on a bad claim.

    Validation happens before anything is written, so a rejected brief leaves no
    half-written file behind.
    """
    problems = validate_claims(conn, claims)
    if problems:
        raise InvalidBrief(problems)

    sources: dict[str, str] = {}
    for claim in claims:
        fragment_id = claim.get("fragment_id")
        if fragment_id:
            fragment = issued.get(conn, fragment_id)
            sources[fragment_id] = fragment["url"] if fragment else ""

    path = _unique_path(Path(brief_dir), today, topic)
    path.write_text(render(topic, summary, claims, gaps, sources), encoding="utf-8")

    brief_id = hashlib.sha256(str(path).encode()).hexdigest()[:16]
    claim_blob = "\n".join(claim["text"] for claim in claims)

    def op(c: sqlite3.Connection) -> None:
        c.execute(
            "INSERT OR REPLACE INTO briefs (brief_id, job_id, topic, summary, path, created)"
            " VALUES (?, ?, ?, ?, ?, strftime('%s','now'))",
            (brief_id, job_id, topic, summary, str(path)),
        )
        c.execute("DELETE FROM brief_claims WHERE brief_id = ?", (brief_id,))
        for index, claim in enumerate(claims):
            c.execute(
                "INSERT INTO brief_claims (brief_id, idx, text, kind, fragment_id, quote)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    brief_id,
                    index,
                    claim["text"],
                    claim.get("kind", "fact"),
                    claim.get("fragment_id"),
                    claim.get("quote"),
                ),
            )
        c.execute("DELETE FROM briefs_fts WHERE brief_id = ?", (brief_id,))
        c.execute(
            "INSERT INTO briefs_fts (brief_id, topic, summary, claims) VALUES (?, ?, ?, ?)",
            (brief_id, topic, summary, claim_blob),
        )

    db.write(conn, op)
    log.info("briefs.saved", brief_id=brief_id, claims=len(claims), gaps=len(gaps))
    return {
        "brief_id": brief_id,
        "path": str(path),
        "claims": len(claims),
        "gaps": len(gaps),
    }


def search(conn: sqlite3.Connection, query: str, limit: int = 3) -> list[dict]:
    """Rank past briefs. Returns metadata and a snippet — never the brief body."""
    match = fragments_module._fts_query(query)
    if not match:
        return []
    try:
        rows = conn.execute(
            "SELECT brief_id, topic,"
            " snippet(briefs_fts, 2, '', '', '…', 20) AS snippet"
            " FROM briefs_fts WHERE briefs_fts MATCH ? ORDER BY bm25(briefs_fts) LIMIT ?",
            (match, limit),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        log.warning("briefs.search_failed", error=str(exc))
        return []

    hits = []
    for row in rows:
        meta = conn.execute(
            "SELECT path, created FROM briefs WHERE brief_id = ?", (row["brief_id"],)
        ).fetchone()
        if meta is None:
            continue
        hits.append(
            {
                "topic": row["topic"],
                "path": meta["path"],
                "age_days": int((int(time.time()) - meta["created"]) // 86400),
                "snippet": row["snippet"][:400],
            }
        )
    return hits
