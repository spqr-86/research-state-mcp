# research-state-mcp — agent guide

Thin MCP server that gives a research workflow **memory, state and fragments**.
There is no LLM inside: the client (Claude Code or any MCP host) does the thinking,
this server remembers what was asked, what is still open, and hands back
2–5 relevant paragraphs instead of whole pages.

Search and fetching are **not ours** — they come from `free-search-mcp`
(tools `search` / `fetch` / `read_doc`), whose page cache we read.

## Constitution (stable — changing any of this is a deliberate decision)

1. **No model in the server.** No API keys, no LLM calls, no `claude -p`. Every
   judgement call (is this the same question? is the claim supported?) is returned
   to the client as candidates + scores.
2. **Never return whole pages.** Fragments and file paths only; full text only on an
   explicit, separate request. Context rot is the problem this project exists to solve.
3. **We wrap, we don't rewrite.** No own search engines, no own crawler, no own
   page cache — `free-search-mcp` owns those.
4. **Stack locked:** Python 3.12, FastMCP (`fastmcp>=3.0,<4`), stdlib `sqlite3`,
   SQLite FTS5 for all ranking. No embeddings, no vector store until FTS5 is
   demonstrably insufficient on a real corpus.
5. **Logic lives outside the MCP layer.** Tool functions are thin adapters over
   plain modules (`state.py`, `fragments.py`, …) so a move to code-execution /
   library form never requires a rewrite.
6. **The tool contract is fixed from day one** (see below). `verify_claim` returns
   `verdict`/`confidence`/`method` from the first commit, even while `verdict` is
   always `"unverified"` — stage 3 fills them in without breaking anyone.
7. **TDD**, conventional commits (English), type hints everywhere, `structlog`
   over `print`/`logging`, no bare `except`.
8. **Public repo from the first commit.** Nothing personal, no secrets, no absolute
   paths to Petr's home in committed code — paths come from config/env.

## Commands

```bash
uv sync                       # install (dev deps included)
uv run pytest                 # full test suite
uv run pytest -m smoke        # smoke test — end-to-end, must pass after every commit
uv run research-state-mcp     # run the server over stdio
```

## Tool contract

```
research_start(topic)                  -> {job_id, similar_briefs[]}
research_plan(job_id, subquestions[])  -> plan saved
research_mark(job_id, subq_id, answer) -> subquestion closed
fragments(url, query, k=5)             -> {fragments[], cache_path, title, fetched_at}
verify_claim(claim, url)               -> {fragments[], verdict, confidence, method}
research_finish(job_id, brief)         -> brief stored in the library
brief_search(query)                    -> ranked past briefs
```

## Boundaries — ask before doing

- Editing anything under `~/.claude/` (agents, settings, MCP registration).
- Touching `~/.cache/search-mcp/cache.sqlite` in any way other than **read-only**.
  It belongs to another project; we never write to it, never migrate it.
- Pushing to the public remote, or making the repo public/private.
- Adding a dependency that needs an API key or a paid account.

## Definition of Done

See PLAN.md §11. "Code is written" is not done.
