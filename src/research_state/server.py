"""FastMCP adapters — deliberately thin.

# ANCHOR: server
# Role: expose state.py / fragments.py as MCP tools over stdio. No logic here
# beyond argument shaping and error-to-payload conversion, so the same modules
# can be called directly if this ever moves to code-execution form.
# Paths come from env (RESEARCH_STATE_DB, SEARCH_MCP_CACHE) so nothing in the
# code assumes a particular home directory.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import structlog
from fastmcp import FastMCP

from . import db, fragments, state

log = structlog.get_logger(__name__)

DEFAULT_STATE_DB = (
    Path.home() / ".local" / "share" / "research-state-mcp" / "state.sqlite"
)
DEFAULT_SEARCH_CACHE = Path.home() / ".cache" / "search-mcp" / "cache.sqlite"

mcp = FastMCP("research-state")

_conn: sqlite3.Connection | None = None


def state_db_path() -> Path:
    return Path(os.environ.get("RESEARCH_STATE_DB", DEFAULT_STATE_DB))


def search_cache_path() -> Path:
    return Path(os.environ.get("SEARCH_MCP_CACHE", DEFAULT_SEARCH_CACHE))


def connection() -> sqlite3.Connection:
    """One lazily-opened connection per process."""
    global _conn
    if _conn is None:
        _conn = db.connect(state_db_path())
        state.init_schema(_conn)
    return _conn


def reset_connection() -> None:
    """Drop the cached connection — used by tests that switch DB paths."""
    global _conn
    if _conn is not None:
        _conn.close()
    _conn = None


@mcp.tool
def research_start(topic: str) -> dict:
    """Open a research job and get back its id.

    Call this first, before searching. The job_id is what makes the plan
    survive a session restart. `similar_briefs` will list past briefs on the
    same topic once the brief library lands (stage 2); it is empty for now.

    Args:
        topic: The research question in the user's own words.
    """
    job = state.start_job(connection(), topic)
    return {**job, "similar_briefs": []}


@mcp.tool
def research_plan(job_id: str, subquestions: list[str]) -> dict:
    """Record the subquestions this research has to close.

    Appends — calling it again with more subquestions raises the depth of a
    running job without losing what is already answered. Subquestions already
    in the plan are skipped, so re-sending the whole list is safe.

    Args:
        job_id: From `research_start`.
        subquestions: 3-5 for a normal run, 5-8 for a deep one.
    """
    try:
        return state.set_plan(connection(), job_id, subquestions)
    except state.UnknownJob:
        return {"error": "unknown_job", "job_id": job_id}


@mcp.tool
def research_mark(job_id: str, subq_id: int, answer: str) -> dict:
    """Close one subquestion, and see what is still open.

    Args:
        job_id: From `research_start`.
        subq_id: The 1-based id shown in the plan.
        answer: What you found — one or two sentences, not the raw source.
    """
    try:
        return state.mark(connection(), job_id, subq_id, answer)
    except state.UnknownJob:
        return {"error": "unknown_job", "job_id": job_id}
    except state.UnknownSubquestion:
        return {"error": "unknown_subquestion", "job_id": job_id, "subq_id": subq_id}


@mcp.tool
def research_status(job_id: str) -> dict:
    """Full state of a job: topic and every subquestion with its status.

    Args:
        job_id: From `research_start`.
    """
    try:
        return state.get_job(connection(), job_id)
    except state.UnknownJob:
        return {"error": "unknown_job", "job_id": job_id}


@mcp.tool
def fragments_for(url: str, query: str, k: int = 5, neighbours: int = 1) -> dict:
    """Return the few paragraphs of a page that answer `query` — not the page.

    Prefer this over reading a whole page: full page text costs a large part of
    the context window and measurably degrades accuracy long before the limit.

    The page must already be in the local `free-search-mcp` cache — call that
    server's `fetch(url)` (or `research`) first. On a miss this returns
    `error="not_cached"` instead of fetching, because fetching is not this
    server's job.

    Args:
        url: The exact URL that was fetched.
        query: What you are looking for on the page — the subquestion, not the
            whole topic. Terms from the subquestion sharpen the ranking.
        k: Maximum number of fragments (2-5 is the useful range).
        neighbours: Paragraphs of context kept on each side of a hit.
    """
    page = fragments.cached_page(search_cache_path(), url)
    if page is None:
        return {
            "error": "not_cached",
            "url": url,
            "hint": "fetch this URL with the search MCP first, then call fragments_for again",
        }
    found = fragments.extract(page["content"], query, k=k, neighbours=neighbours)
    return {
        "url": url,
        "title": page["title"],
        "fetched_at": page["fetched_at"],
        "cache_path": page["cache_path"],
        "paragraphs_total": len(fragments.split_paragraphs(page["content"])),
        "fragments": found,
    }


def run() -> None:
    """stdio entry point."""
    log.info(
        "server.start", state_db=str(state_db_path()), cache=str(search_cache_path())
    )
    mcp.run()


if __name__ == "__main__":
    run()
