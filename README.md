# research-state-mcp

[![CI](https://github.com/spqr-86/research-state-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/spqr-86/research-state-mcp/actions/workflows/ci.yml)

**Model-free MCP infrastructure for research state, context compression, and citation provenance.**

Most research agents spend context on raw pages and then rely on the model to remember what was asked, what remains open, and where each claim came from. This server moves those responsibilities into a small deterministic layer.

**No LLM. No embeddings. No API key.** The MCP client reasons; this server owns state, fragment retrieval, provenance, citation checks, brief storage, and freshness metadata.

**Current verification:** 272 tests passing locally, including two end-to-end smoke scenarios through the real FastMCP client/server protocol.

## Why this project exists

A research workflow needs more than search:

- durable state across sessions;
- a visible plan with open/closed subquestions;
- a way to return a few relevant fragments instead of an entire page;
- provenance for the text actually shown to the model;
- deterministic rejection of unsupported citations;
- reusable research briefs with explicit freshness semantics.

The design goal is not to build another research agent. It is to give an agent a **small, inspectable state/context layer** and keep model-dependent reasoning outside the server.

## Architecture

```text
MCP client / research agent
        |
        |  plan, mark, fragments, finish, search
        v
research-state-mcp
   |-- research state ----------> SQLite
   |-- fragment ranking --------> FTS5 / BM25
   |-- issued-fragment ledger --> provenance
   |-- citation validation -----> deterministic rules
   |-- brief library -----------> local Markdown + SQLite index
   |
   +-- reads page cache <-------- free-search-mcp
```

Search/fetching remains the responsibility of [free-search-mcp](https://github.com/sweetcornna/free-search-mcp). This server reads its local page cache and never writes to it.

## Measured trade-offs

The repository includes evaluation scripts over cached FRAMES/Wikipedia pages. The point of these experiments is not a single headline score; they test whether a design decision actually improves the context/provenance layer.

| Experiment | Result | Engineering decision |
|---|---|---|
| Fragment budget | per-source `k=5`: **77.8% recall** where the answer is present, while returning **17.2%** of page text | allocate fragment budget per source rather than across a multi-source question |
| Structured vs fixed-size slicing | at similar ~17% text budget: **51.1% vs 40.0% recall** | preserve paragraph structure when available |
| Citation fabrication | fabricated/tampered quote families: **100% rejected** in the 500-fragment experiment | keep citation provenance deterministic |
| Ellipsis handling | faithful ellipsis rejection **100% → 0%** after ordered-piece support; fabrication families stayed at 100% rejection | relax an overly strict invariant only after measuring its failure mode |
| Minimum quote length | 5 words: **4.2% ambiguity / 4.2% honest-block rate** in the measured corpus | use a measured five-word floor instead of an arbitrary threshold |
| Table-shaped pages | at ~56% returned text: structured cutting **86.7% recall vs 80.0%** for blind slices | cut on table/cell boundaries instead of fixed character offsets |

Full methodology, denominators and caveats: **[eval/RESULTS.md](eval/RESULTS.md)**.

### Important limitation

FRAMES is a multi-hop benchmark. In the first 100 evaluated examples, the literal gold answer appeared on the linked pages in only **45/100** cases. Fragment recall is therefore reported over those answer-present examples, not over all 100. The eval documentation keeps this denominator explicit rather than presenting the result as end-to-end research accuracy.

## Failure → measurement → change

One example captures the engineering approach behind the project.

The first citation invariant required a quote to appear verbatim in an issued fragment. It rejected fabricated text, but evaluation showed it also rejected **every faithful ellipsis-abbreviated quote**.

Instead of removing the invariant, the implementation was changed to accept ellipsis-separated pieces only when every piece occurs **in order, without overlap, inside the same issued fragment**.

On the same evaluation set:

- ellipsis false rejection: **100% → 0%**;
- five fabrication mutation families: **100% rejection remained 100%**;
- remaining measured false rejects were typography differences.

A later experiment exposed another hole: tiny verbatim snippets could pass while carrying little evidence. A quote-length experiment priced that trade-off and selected a five-word minimum.

That loop — **hypothesis → measurement → failure → design change → regression measurement** — is the core of the repository.

## MCP tools

| Tool | Responsibility |
|---|---|
| `research_start` | open a durable research job and surface similar prior briefs |
| `research_plan` | persist subquestions without resetting previous progress |
| `research_mark` | close a subquestion |
| `research_status` | inspect full job state |
| `research_gaps` | make unfinished research explicit |
| `fragments_for` | return ranked page fragments and record what was issued |
| `research_finish` | validate claims/citations and store a reusable brief |
| `brief_search` | search prior briefs with freshness metadata |
| `verify_claim` | retrieve evidence for client-side judgement |
| `research_state_stats` | report how much raw page text was kept out of context |

`verify_claim` deliberately returns `verdict="unverified"`: there is no hidden model in the server. Evidence is retrieved deterministically; semantic judgement remains with the client.

## Reliability boundaries

The server intentionally owns the parts that do not require model judgement:

- research jobs and subquestion state survive session restarts;
- factual brief claims must reference a fragment that was actually issued;
- citation quotes are checked against that fragment;
- open subquestions cannot silently disappear at finish time;
- fact freshness is stored per claim (`timeless`, `dataset`, `vendor`, `world`) rather than inferred from the age of a whole brief;
- the external search cache is read-only.

The smoke suite exercises the real FastMCP client/server protocol, including state persistence, fragment retrieval, citation rejection, gap handling, brief storage/search and stats.

## Why FTS5 instead of embeddings?

The search spaces are deliberately small: one page at a time for fragments and a local library of briefs.

SQLite FTS5:

- has no model/runtime dependency;
- works locally and offline;
- handles Russian with the `unicode61` tokenizer;
- is easy to inspect and reproduce;
- is sufficient until evaluation shows otherwise.

If lexical retrieval becomes the measured bottleneck, dense retrieval or RRF can be added then. They are not included merely because this is an AI-adjacent project.

## Reproduce

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest
```

Run the measured experiments:

```bash
uv run python eval/frames_recall.py --limit 100 --k 5
uv run python eval/citation_rejection.py --limit 500
uv run python eval/quote_floor.py --limit 500
```

Run the server:

```bash
uv run research-state-mcp
```

Example Claude Code registration:

```bash
claude mcp add research-state -s user -- uv run --directory /path/to/research-state-mcp research-state-mcp
```

Both databases are local SQLite files by default:

- state: `~/.local/share/research-state-mcp/state.sqlite`
- page cache: `~/.cache/search-mcp/cache.sqlite` (owned by `free-search-mcp`)

## Repository map

```text
src/research_state/   server, state, fragments, briefs, provenance, metrics
tests/                unit + MCP smoke tests
eval/                 reproducible evaluation scripts and measured results
agent/                example research-agent integration
skill/                workflow instructions for an MCP client
docs/research/        design research
```

## Current limitations

- Fragment retrieval is lexical; semantic mismatch can still lose evidence.
- Citation validation proves provenance, not semantic entailment between quote and claim.
- Some typography normalization remains intentionally conservative.
- The FRAMES evaluation has a small answer-present denominator for fragment recall.
- Freshness horizons are conventions, not measured decay rates.
- Search/fetching is an external dependency rather than part of this server.

## Design principle

> Add model complexity only when a measured failure requires it.

The project is intentionally small: **SQLite + FTS5 + deterministic provenance rules + MCP contracts** around a model that lives somewhere else.

## License

MIT
