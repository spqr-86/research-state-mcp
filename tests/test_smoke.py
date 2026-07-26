"""End-to-end smoke test: a real MCP client talking to the real server.

Runs the whole stage-1 path over the MCP protocol (in-memory transport, no
subprocess): open a job, plan it, read fragments out of a cached page, close a
subquestion, reopen the DB and check the plan survived.
"""

import asyncio
import json
import sqlite3

import pytest
from fastmcp import Client

from research_state import server

PAGE = """Reciprocal Rank Fusion

RRF merges several ranked lists into a single list without normalising scores.

The constant k defaults to 60 in both Elasticsearch and Azure AI Search.

Совершенно посторонний абзац про котиков, нужный только как шум в странице.

Because it uses ranks rather than raw scores, RRF needs no calibration between
the engines whose results it fuses.
"""


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Point the server at a temp state DB and a fake free-search-mcp cache."""
    cache = tmp_path / "cache.sqlite"
    conn = sqlite3.connect(cache)
    conn.execute(
        "CREATE TABLE pages (url TEXT PRIMARY KEY, title TEXT, content TEXT NOT NULL,"
        " fetched INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO pages VALUES (?,?,?,?)",
        (
            "https://example.com/rrf",
            '\x01META\x01{"title": "RRF explained"}\x01',
            PAGE,
            1753500000,
        ),
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("RESEARCH_STATE_DB", str(tmp_path / "state.sqlite"))
    monkeypatch.setenv("SEARCH_MCP_CACHE", str(cache))
    server.reset_connection()
    yield
    server.reset_connection()


async def _call(client: Client, name: str, args: dict) -> dict:
    result = await client.call_tool(name, args)
    return json.loads(result.content[0].text)


@pytest.mark.smoke
def test_full_stage_one_path(wired):
    async def scenario() -> None:
        async with Client(server.mcp) as client:
            names = {t.name for t in await client.list_tools()}
            assert {
                "research_start",
                "research_plan",
                "research_mark",
                "research_status",
                "fragments_for",
            } <= names

            job_id = (
                await _call(client, "research_start", {"topic": "how does RRF work"})
            )["job_id"]

            plan = await _call(
                client,
                "research_plan",
                {
                    "job_id": job_id,
                    "subquestions": ["what is the default k", "does it normalise"],
                },
            )
            assert plan["added"] == 2

            frags = await _call(
                client,
                "fragments_for",
                {
                    "url": "https://example.com/rrf",
                    "query": "default value of k",
                    "k": 2,
                },
            )
            assert frags["title"] == "RRF explained"
            joined = "\n".join(f["text"] for f in frags["fragments"])
            assert "60" in joined
            assert "котиках" not in joined
            assert len(joined) < len(PAGE)
            assert all(len(f["fragment_id"]) == 16 for f in frags["fragments"])

            marked = await _call(
                client,
                "research_mark",
                {"job_id": job_id, "subq_id": 1, "answer": "k defaults to 60"},
            )
            assert marked["closed"] == 1
            assert marked["open_subquestions"] == [
                {"subq_id": 2, "text": "does it normalise"}
            ]

            miss = await _call(
                client,
                "fragments_for",
                {"url": "https://example.com/gone", "query": "anything"},
            )
            assert miss["error"] == "not_cached"

        # a fresh process would open a fresh connection — the plan must still be there
        server.reset_connection()
        async with Client(server.mcp) as client:
            status = await _call(client, "research_status", {"job_id": job_id})
            assert status["subquestions"][0]["status"] == "closed"
            assert status["subquestions"][1]["status"] == "open"

    asyncio.run(scenario())


@pytest.mark.smoke
def test_brief_lifecycle(wired, tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_BRIEF_DIR", str(tmp_path / "briefs"))

    async def scenario() -> None:
        async with Client(server.mcp) as client:
            job_id = (await _call(client, "research_start", {"topic": "how does RRF work"}))[
                "job_id"
            ]
            await _call(
                client,
                "research_plan",
                {"job_id": job_id, "subquestions": ["default k", "does it normalise"]},
            )
            frags = await _call(
                client,
                "fragments_for",
                {"url": "https://example.com/rrf", "query": "default value of k", "k": 1},
            )
            fid = frags["fragments"][0]["fragment_id"]

            # a fabricated quote is refused, and nothing is written
            refused = await _call(
                client,
                "research_finish",
                {
                    "job_id": job_id,
                    "summary": "s",
                    "claims": [
                        {
                            "text": "k is 42",
                            "kind": "fact",
                            "fragment_id": fid,
                            "quote": "k defaults to 42",
                        }
                    ],
                    "gaps": ["none"],
                },
            )
            assert refused["error"] == "invalid_brief"
            assert refused["problems"][0]["reason"] == "quote_not_found"

            # an open subquestion with nothing said about it blocks the finish
            blocked = await _call(
                client,
                "research_finish",
                {"job_id": job_id, "summary": "s", "claims": [], "gaps": []},
            )
            assert blocked["error"] == "unclosed_subquestions"

            await _call(client, "research_mark", {"job_id": job_id, "subq_id": 1, "answer": "60"})
            gaps = await _call(client, "research_gaps", {"job_id": job_id})
            assert gaps["open"] == [{"subq_id": 2, "text": "does it normalise"}]

            saved = await _call(
                client,
                "research_finish",
                {
                    "job_id": job_id,
                    "summary": "RRF fuses ranked lists.",
                    "claims": [
                        {
                            "text": "k defaults to 60",
                            "kind": "fact",
                            "fragment_id": fid,
                            "quote": "constant k defaults to 60",
                        }
                    ],
                    "gaps": ["did not check whether it normalises"],
                },
            )
            assert saved["path"].endswith(".md")
            assert len(str(saved)) < 2000  # a path and counts, not the brief itself

            hits = await _call(client, "brief_search", {"query": "RRF"})
            assert hits["briefs"][0]["path"] == saved["path"]

            again = await _call(client, "research_start", {"topic": "how does RRF work"})
            assert again["similar_briefs"]

            verdict = await _call(
                client,
                "verify_claim",
                {"claim": "the constant k defaults to 60", "url": "https://example.com/rrf"},
            )
            assert verdict["verdict"] == "unverified"
            assert verdict["method"] == "quote-match"

            stats = await _call(client, "research_state_stats", {})
            assert stats["chars_saved"] > 0

    asyncio.run(scenario())
