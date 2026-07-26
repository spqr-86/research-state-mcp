# PLAN — research-state-mcp

## 0. Constitution

See AGENTS.md § Constitution. Short form: no model in the server, never return whole
pages, wrap `free-search-mcp` instead of rewriting it, Python 3.12 + FastMCP 3 + SQLite
FTS5, logic outside the MCP layer, tool contract frozen from day one, TDD, public repo.

## 1. Goal

Replace the `research` subagent with a tool that **remembers, checks itself and does not
flood the context**. A research run gets an explicit plan of subquestions with statuses
that survives session restarts; page text arrives as 2–5 relevant paragraphs plus a path;
finished briefs go into a searchable library so a repeat question costs almost nothing.

## 2. Inputs

- Search results and page text: `free-search-mcp` (MIT), keyless engines
  DuckDuckGo / Mojeek / Google News / Bing. Its page cache is read-only for us.
- Briefs written by the client, stored under `~/knowledge/research/` (path configurable).
- No datasets, no scraping of our own, no licensed corpora.

## 3. Architecture

Three parts with separated roles:

- **MCP server (this repo)** — hands and memory. State, fragments, brief library.
- **Skill** (stage 2) — the process: plan → search → fill gaps → verify → brief.
- **`research` subagent** — stays a thin wrapper so raw material never reaches the
  main context.

Inside the server:

```
src/research_state/
  db.py         # sqlite connection: WAL, busy_timeout, write retry
  state.py      # jobs, subquestions, statuses  (no MCP imports)
  fragments.py  # BM25 over paragraphs of a cached page (no MCP imports)
  briefs.py     # FTS5 library                   (stage 2)
  server.py     # FastMCP adapters — thin
```

Own DB (`~/.local/share/research-state-mcp/state.sqlite`): jobs, subquestions, briefs.
Foreign DB (`~/.cache/search-mcp/cache.sqlite`): read-only source of page text.

Ranking: FTS5 (`unicode61`) built on the fly over the paragraphs of one page — no
`rank-bm25`, no preprocessing pipeline, works on Russian out of the box. Query is
expanded with the subquestion's terms; neighbouring paragraphs of a hit are returned
with it so a match never arrives without its context.

Concurrency: Claude Code calls tools in parallel. WAL gives concurrent readers but a
single writer, so writes go through a retry layer, and **no network call ever happens
inside a transaction**.

## 4. Why not the alternatives

- Own deep-research server (v1): needs a paid API key. MCP sampling is deprecated
  (SEP-2577) and was never implemented in Claude Code; `claude -p` as a backend is a
  ToS grey zone — unacceptable for a public repo.
- Ready-made memory MCPs (`local-memory-mcp`, `ai-memory-mcp`, …): none of them has the
  two things that matter here — a fetch that returns paragraphs instead of full text,
  and `verify_claim`.
- `mcp-server-deep-research`: closest in intent but stateless — a prompt orchestrator
  with no DB and no subquestion statuses.

## 5. Verification

- `pytest` on every module; smoke test (`-m smoke`) exercising the real end-to-end path
  against a temporary DB after every commit.
- Fragment quality is judged by hand on real Russian and English pages — there is no
  labelled set and inventing one would be theatre.
- Success metric (not "it runs"): a repeat research on a known topic costs ~nothing;
  no claim in a brief exists without a fragment behind it; full page text never enters
  the main context.

## 6. Current status (2026-07-26)

- Stage 0 **done** — `free-search-mcp` installed via `uvx`, registered in Claude Code
  user scope as `search`; keyless engines verified live, page cache fills.
- Stage 1 **done** — `db.py` / `state.py` / `fragments.py` / `server.py`, 39 tests green
  including the smoke test. Checked on live cached pages: a 60-paragraph English page
  returns 594 characters, a 51-paragraph Russian page returns 2.5 KB.
- Stages 2–3 not started. Not yet registered as an MCP server in Claude Code.

## 7. Next steps

- [x] Repo foundation (AGENTS.md / CLAUDE.md / PLAN.md, pyproject, smoke test)
- [x] `db.py` — WAL, busy_timeout, write retry, cross-thread safety
- [x] `state.py` — `research_start` / `research_plan` / `research_mark` + schema
- [x] `fragments.py` — paragraph split, FTS5 ranking, neighbour context
- [x] `server.py` — FastMCP adapters over the above, stdio run
- [ ] Push the repo public (needs Petr's word)
- [ ] Register in Claude Code, use it on a live research
- [ ] Stage 2: `research_finish`, `brief_search`, `verify_claim`, skill, `research.md`
- [ ] Stage 3: local NLI encoder inside `verify_claim` (measure first, decide after)

## 8. Open decisions

- Depth numbers (5 pages in `normal`, 15 in `deep`) — not confirmed by Petr.
- Where the line for "load-bearing claim" runs in selective verification. Current
  working answer: verify everything the conclusion rests on, and everything that
  contradicts expectation.
- Keep the `research` subagent as a fallback after stage 2? Current answer: yes.
- Does the v2 architecture comparison come back in semi-manual form? Undecided.
- **Russian morphology.** FTS5 `unicode61` does not stem, so a query saying "вступил"
  misses a paragraph saying "Вступление". Seen on a real Wikipedia page in stage 1.
  Cheap fixes before reaching for embeddings: a snowball stemmer, an FTS5 trigram
  tokenizer, or prefix-matching the query tokens. Not fixed yet — measure first.
- **Table-shaped paragraphs.** A Wikipedia infobox is one huge "paragraph", so a hit on
  it returns ~2 KB. Consider splitting oversized paragraphs before ranking.

## 9. Repo structure

```
AGENTS.md  CLAUDE.md  PLAN.md  README.md  LICENSE  pyproject.toml
src/research_state/{__init__,db,state,fragments,server}.py
tests/{test_db,test_state,test_fragments,test_smoke}.py
```

## 10. Definition of Done (stage 1)

- [x] `uv run pytest` green — 39 tests
- [x] `uv run pytest -m smoke` green, driving the server over a real MCP client
- [x] A plan with statuses survives a reconnect (proved by tests in two places)
- [x] `fragments()` on a real cached page returns 2–5 paragraphs and a path — never the
      whole page — checked on one Russian and one English page
- [x] README explains what the server does and how to run it
- [ ] Repo pushed, public, no secrets and no absolute home paths in code — **waiting on
      Petr's go-ahead to create the public remote**
