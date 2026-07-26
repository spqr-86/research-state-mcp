"""FastMCP adapters — deliberately thin.

# ANCHOR: server
# Role: expose state.py / fragments.py as MCP tools over stdio. No logic here
# beyond argument shaping and error-to-payload conversion, so the same modules
# can be called directly if this ever moves to code-execution form.
# Paths come from config.py (env-overridable) so nothing here assumes a
# particular home directory.
"""

from __future__ import annotations

import sqlite3

import structlog
from fastmcp import FastMCP

from datetime import date

from . import briefs, config, db, fragments, issued, metrics, state

log = structlog.get_logger(__name__)

mcp = FastMCP("research-state")

_conn: sqlite3.Connection | None = None


def connection() -> sqlite3.Connection:
    """One lazily-opened connection per process."""
    global _conn
    if _conn is None:
        _conn = db.connect(config.state_db_path())
        state.init_schema(_conn)
        issued.init_schema(_conn)
        metrics.init_schema(_conn)
        briefs.init_schema(_conn)
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
    same topic — if one of them already answers the question, read that file
    and stop: a repeat question is supposed to cost nothing.

    Args:
        topic: The research question in the user's own words.
    """
    conn = connection()
    job = state.start_job(conn, topic)
    return {**job, "similar_briefs": briefs.search(conn, topic)}


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
    page = fragments.cached_page(config.search_cache_path(), url)
    if page is None:
        return {
            "error": "not_cached",
            "url": url,
            "hint": "fetch this URL with the search MCP first, then call fragments_for again",
        }
    found = fragments.extract(page["content"], query, k=k, neighbours=neighbours)
    conn = connection()
    found = [
        issued.record(conn, url=url, fetched_at=page["fetched_at"], fragment=f) for f in found
    ]
    paragraphs_total = len(fragments.split_paragraphs(page["content"]))
    metrics.record_fetch(
        conn,
        url=url,
        paragraphs_total=paragraphs_total,
        paragraphs_returned=len(found),
        chars_total=len(page["content"]),
        chars_returned=sum(len(f["text"]) for f in found),
    )
    return {
        "url": url,
        "title": page["title"],
        "fetched_at": page["fetched_at"],
        "cache_path": page["cache_path"],
        "paragraphs_total": paragraphs_total,
        "fragments": found,
    }


@mcp.tool
def research_gaps(job_id: str) -> dict:
    """Which subquestions of this job are still open.

    Call this before finishing. Anything still open must either be closed with
    `research_mark` or written into `gaps` when you finish — research does not
    get to quietly stop at the point the model feels done.

    Args:
        job_id: From `research_start`.
    """
    try:
        return state.gaps(connection(), job_id)
    except state.UnknownJob:
        return {"error": "unknown_job", "job_id": job_id}


@mcp.tool
def research_finish(
    job_id: str,
    summary: str,
    claims: list[dict],
    gaps: list[str] | None = None,
) -> dict:
    """Store the finished brief. Rejects any factual claim without a real quote.

    Work your conclusions out first, in prose, and only then call this to record
    them. Assembling findings while filling in fields measurably costs reasoning
    quality, so treat this as the packing step, not the thinking step.

    Args:
        job_id: From `research_start`.
        summary: The synthesis, in prose. Not claim-checked.
        claims: One dict per claim: `{text, kind, fragment_id, quote}`. `kind` is
            "fact" or "assumption". A fact needs a `fragment_id` from
            `fragments_for` and a `quote` copied verbatim out of that fragment —
            the server checks the quote is really there, so pointing at an
            unrelated fragment fails. An assumption needs neither and is printed
            in the brief as an assumption.
        gaps: What you could not answer. Required while any subquestion is open.
    """
    conn = connection()
    gaps = gaps or []
    try:
        job = state.get_job(conn, job_id)
    except state.UnknownJob:
        return {"error": "unknown_job", "job_id": job_id}

    still_open = state.gaps(conn, job_id)["open"]
    if still_open and not gaps:
        return {
            "error": "unclosed_subquestions",
            "open": still_open,
            "hint": "close them with research_mark, or say what you could not find in `gaps`",
        }

    try:
        return briefs.save(
            conn,
            job_id=job_id,
            topic=job["topic"],
            summary=summary,
            claims=claims,
            gaps=gaps,
            brief_dir=config.brief_dir(),
            today=date.today().isoformat(),
        )
    except briefs.InvalidBrief as exc:
        return {"error": "invalid_brief", "problems": exc.problems}


@mcp.tool
def brief_search(query: str, limit: int = 3) -> dict:
    """Search briefs from past research. Returns paths and snippets, not bodies.

    Args:
        query: The topic in natural language.
        limit: How many briefs to return.
    """
    return {"briefs": briefs.search(connection(), query, limit=limit)}


@mcp.tool
def verify_claim(claim: str, url: str) -> dict:
    """Fetch the fragments of a page most relevant to a claim, for you to judge.

    `verdict` is always "unverified" today: this server has no model, so the
    judgement is yours. The field exists so that when a local entailment model
    moves in behind this tool, nothing calling it has to change.

    Args:
        claim: The statement to check.
        url: The page it supposedly came from.
    """
    found = fragments_for(url=url, query=claim, k=3)
    if "error" in found:
        return found
    return {**found, "verdict": "unverified", "confidence": None, "method": "quote-match"}


@mcp.tool
def research_state_stats() -> dict:
    """How much page text this server has kept out of the context so far."""
    return metrics.stats(connection())


def run() -> None:
    """stdio entry point."""
    log.info(
        "server.start",
        state_db=str(config.state_db_path()),
        cache=str(config.search_cache_path()),
    )
    mcp.run()


if __name__ == "__main__":
    run()
