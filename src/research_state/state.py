"""Research jobs and their subquestion plans — the memory that survives restarts.

# ANCHOR: state
# Role: owns the `jobs` / `subquestions` tables. No MCP imports, no network.
# In: a sqlite3.Connection from db.connect(). Out: plain dicts.
# Key behaviour: set_plan APPENDS (raising the depth level mid-run must not
# restart the job) and de-duplicates by normalised text; subq_id is per-job
# and 1-based so the client can say "close number 2".
"""

from __future__ import annotations

import sqlite3
import time
import uuid

import structlog

from . import db

log = structlog.get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id  TEXT PRIMARY KEY,
    topic   TEXT NOT NULL,
    status  TEXT NOT NULL DEFAULT 'open',
    created INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS subquestions (
    job_id    TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    subq_id   INTEGER NOT NULL,
    text      TEXT NOT NULL,
    norm      TEXT NOT NULL,
    status    TEXT NOT NULL DEFAULT 'open',
    answer    TEXT,
    closed_at INTEGER,
    PRIMARY KEY (job_id, subq_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS subquestions_norm_idx
    ON subquestions(job_id, norm);
"""


class UnknownJob(KeyError):
    """No job with this id."""


class UnknownSubquestion(KeyError):
    """No such subquestion in this job."""


def init_schema(conn: sqlite3.Connection) -> None:
    db.write(conn, lambda c: c.executescript(SCHEMA))


def _normalise(text: str) -> str:
    return " ".join(text.lower().split())


def _require_job(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        raise UnknownJob(job_id)
    return row


def start_job(conn: sqlite3.Connection, topic: str) -> dict:
    """Open a new research job. Returns {job_id, topic}."""
    topic = topic.strip()
    if not topic:
        raise ValueError("topic must not be empty")
    job_id = uuid.uuid4().hex[:12]
    created = int(time.time())
    db.write(
        conn,
        lambda c: c.execute(
            "INSERT INTO jobs (job_id, topic, created) VALUES (?, ?, ?)",
            (job_id, topic, created),
        ),
    )
    log.info("state.job_started", job_id=job_id)
    return {"job_id": job_id, "topic": topic, "created": created}


def set_plan(conn: sqlite3.Connection, job_id: str, subquestions: list[str]) -> dict:
    """Append subquestions to the job's plan, skipping ones already there."""
    _require_job(conn, job_id)
    candidates = [(s.strip(), _normalise(s)) for s in subquestions if s.strip()]

    def op(c: sqlite3.Connection) -> dict:
        existing = {
            r["norm"]
            for r in c.execute(
                "SELECT norm FROM subquestions WHERE job_id = ?", (job_id,)
            )
        }
        next_id = (
            c.execute(
                "SELECT COALESCE(MAX(subq_id), 0) FROM subquestions WHERE job_id = ?",
                (job_id,),
            ).fetchone()[0]
            + 1
        )
        added = 0
        for text, norm in candidates:
            if norm in existing:
                continue
            c.execute(
                "INSERT INTO subquestions (job_id, subq_id, text, norm) VALUES (?, ?, ?, ?)",
                (job_id, next_id, text, norm),
            )
            existing.add(norm)
            next_id += 1
            added += 1
        return {"added": added, "skipped": len(candidates) - added}

    result = db.write(conn, op)
    log.info("state.plan_updated", job_id=job_id, **result)
    return {**result, "subquestions": get_job(conn, job_id)["subquestions"]}


def mark(conn: sqlite3.Connection, job_id: str, subq_id: int, answer: str) -> dict:
    """Close one subquestion and report what is still open."""
    _require_job(conn, job_id)
    answer = answer.strip()
    if not answer:
        raise ValueError("answer must not be empty")

    def op(c: sqlite3.Connection) -> None:
        cur = c.execute(
            "UPDATE subquestions SET status = 'closed', answer = ?, closed_at = ?"
            " WHERE job_id = ? AND subq_id = ?",
            (answer, int(time.time()), job_id, subq_id),
        )
        if cur.rowcount == 0:
            raise UnknownSubquestion(f"{job_id}/{subq_id}")

    db.write(conn, op)
    job = get_job(conn, job_id)
    closed = [s for s in job["subquestions"] if s["status"] == "closed"]
    still_open = [
        {"subq_id": s["subq_id"], "text": s["text"]}
        for s in job["subquestions"]
        if s["status"] == "open"
    ]
    log.info("state.subquestion_closed", job_id=job_id, subq_id=subq_id)
    return {
        "job_id": job_id,
        "closed": len(closed),
        "total": len(job["subquestions"]),
        "open_subquestions": still_open,
    }


def get_job(conn: sqlite3.Connection, job_id: str) -> dict:
    """Full state of one job: topic, status and every subquestion in order."""
    row = _require_job(conn, job_id)
    subqs = [
        {
            "subq_id": r["subq_id"],
            "text": r["text"],
            "status": r["status"],
            "answer": r["answer"],
        }
        for r in conn.execute(
            "SELECT subq_id, text, status, answer FROM subquestions"
            " WHERE job_id = ? ORDER BY subq_id",
            (job_id,),
        )
    ]
    return {
        "job_id": row["job_id"],
        "topic": row["topic"],
        "status": row["status"],
        "created": row["created"],
        "subquestions": subqs,
    }
