# research-state-mcp

An MCP server that gives a research workflow **memory, state and fragments** —
and deliberately contains no model.

Most "deep research" MCP servers either need an API key or hide an LLM inside.
This one does neither. The client (Claude Code, or any MCP host) does all the
thinking; the server remembers what was asked, which subquestions are still
open, and — the part that actually matters — hands back **2–5 relevant
paragraphs of a page instead of the whole page**.

That last point is the design goal. Long contexts degrade well before the
formal limit, and worst of all exactly when the needed fragment is
semantically unlike the question — which is the normal case for raw web pages.
So raw page text never enters the client's context; fragments and a cache path do.

Search and fetching are not reimplemented here: they come from
[`free-search-mcp`](https://github.com/sweetcornna/free-search-mcp) (keyless
DuckDuckGo / Mojeek / Google News / Bing), whose page cache this server reads
read-only.

## Status

Stage 1 — state and fragments. Briefs, `brief_search` and `verify_claim` land
in stage 2. See [PLAN.md](PLAN.md).

## Tools

```
research_start(topic)                  -> {job_id, similar_briefs[]}
research_plan(job_id, subquestions[])  -> plan saved (appends, never restarts)
research_mark(job_id, subq_id, answer) -> subquestion closed
fragments(url, query, k=5)             -> {fragments[], cache_path, title, fetched_at}
verify_claim(claim, url)               -> {fragments[], verdict, confidence, method}
research_finish(job_id, brief)         -> brief stored in the library
brief_search(query)                    -> ranked past briefs
```

`verify_claim` returns `verdict`/`confidence`/`method` from the first release
even though the verdict is always `"unverified"` today — the client decides
from the fragments. When a local NLI model moves in behind it, the contract
does not change.

## Install

```bash
uv sync
uv run pytest
claude mcp add research-state -s user -- uv run --directory /path/to/research-state-mcp research-state-mcp
```

Requires Python 3.12+. Both databases are local SQLite files: our own state in
`~/.local/share/research-state-mcp/state.sqlite`, the page cache in
`~/.cache/search-mcp/cache.sqlite` (owned by `free-search-mcp`, never written
to by us).

## Why FTS5 and not embeddings

The corpus is one page at a time for fragments, and 50–500 briefs for the
library. SQLite's built-in FTS5 with the `unicode61` tokenizer handles both,
works on Russian out of the box, and needs no model download. `sqlite-vec` +
reciprocal rank fusion is a cheap upgrade if that stops being true.

## License

MIT
