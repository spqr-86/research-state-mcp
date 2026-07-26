# Stage 2 — Briefs and Citations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a research run produce a durable brief that the server refuses to store unless every factual claim carries a verbatim quote taken from a fragment the server itself handed out.

**Architecture:** Three new plain modules behind the existing thin FastMCP layer — `issued.py` (fragments are persisted when handed out, using W3C Web Annotation-style selectors), `briefs.py` (claim validation, markdown rendering, FTS5 library), `metrics.py` (saved-context counter). `server.py` only adapts. Nothing here calls a model or the network.

**Tech Stack:** Python 3.12, FastMCP 3, stdlib `sqlite3` with FTS5, structlog, pytest.

Spec: `docs/superpowers/specs/2026-07-26-stage-2-briefs-and-citations-design.md`.
Evidence for every decision: `docs/research/`.

---

## File structure

| File | Responsibility |
|---|---|
| `src/research_state/issued.py` | **create** — schema and persistence of fragments handed to the model; id generation; lookup by id |
| `src/research_state/briefs.py` | **create** — claim validation, markdown rendering, brief storage, FTS5 search |
| `src/research_state/metrics.py` | **create** — record and sum saved-context numbers |
| `src/research_state/fragments.py` | modify — `extract` returns offsets, prefix and suffix so a fragment can be re-anchored later |
| `src/research_state/config.py` | **create** — one place for the three paths (state db, search cache, brief dir), all env-overridable |
| `src/research_state/server.py` | modify — new tool adapters, use `config.py` |
| `tests/test_issued.py`, `tests/test_briefs.py`, `tests/test_metrics.py` | **create** |
| `tests/test_fragments.py`, `tests/test_smoke.py` | modify |

Order matters: `issued.py` before `briefs.py` (validation needs stored fragments), `config.py` first (everything else imports it).

---

### Task 1: Config in one place

**Files:**
- Create: `src/research_state/config.py`
- Test: `tests/test_config.py`
- Modify: `src/research_state/server.py:29-40`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
from pathlib import Path

from research_state import config


def test_defaults_live_under_home():
    assert config.state_db_path() == Path.home() / ".local/share/research-state-mcp/state.sqlite"
    assert config.search_cache_path() == Path.home() / ".cache/search-mcp/cache.sqlite"
    assert config.brief_dir() == Path.home() / "knowledge/research"


def test_every_path_is_env_overridable(monkeypatch, tmp_path):
    monkeypatch.setenv("RESEARCH_STATE_DB", str(tmp_path / "s.sqlite"))
    monkeypatch.setenv("SEARCH_MCP_CACHE", str(tmp_path / "c.sqlite"))
    monkeypatch.setenv("RESEARCH_BRIEF_DIR", str(tmp_path / "briefs"))
    assert config.state_db_path() == tmp_path / "s.sqlite"
    assert config.search_cache_path() == tmp_path / "c.sqlite"
    assert config.brief_dir() == tmp_path / "briefs"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'research_state.config'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/research_state/config.py
"""Every filesystem path the server uses, in one place and env-overridable.

# ANCHOR: config
# Role: nothing else may hardcode a path. Tests override via env vars.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_STATE_DB = Path.home() / ".local" / "share" / "research-state-mcp" / "state.sqlite"
DEFAULT_SEARCH_CACHE = Path.home() / ".cache" / "search-mcp" / "cache.sqlite"
DEFAULT_BRIEF_DIR = Path.home() / "knowledge" / "research"


def state_db_path() -> Path:
    return Path(os.environ.get("RESEARCH_STATE_DB", DEFAULT_STATE_DB))


def search_cache_path() -> Path:
    return Path(os.environ.get("SEARCH_MCP_CACHE", DEFAULT_SEARCH_CACHE))


def brief_dir() -> Path:
    return Path(os.environ.get("RESEARCH_BRIEF_DIR", DEFAULT_BRIEF_DIR))
```

- [ ] **Step 4: Point the server at it**

In `src/research_state/server.py`, delete `DEFAULT_STATE_DB`, `DEFAULT_SEARCH_CACHE`, `state_db_path()` and `search_cache_path()`, add `from . import config`, and replace the two call sites (`connection()` and `fragments_for`) with `config.state_db_path()` and `config.search_cache_path()`. Keep `run()`'s log line, using the config functions.

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -q`
Expected: PASS — 41 tests (39 existing + 2 new)

- [ ] **Step 6: Commit**

```bash
git add src/research_state/config.py tests/test_config.py src/research_state/server.py
git commit -m "refactor: put every path in config.py"
```

---

### Task 2: Fragments carry their anchors

`extract` currently returns `{start, end, paragraph_index, score, text}`. Re-anchoring a
quote after a page changes needs the fragment's own text plus surrounding context —
27% of anchors orphan when pages change (`docs/research/2026-07-26-fragment-provenance.md`).

**Files:**
- Modify: `src/research_state/fragments.py` (`_windows`, and add `ANCHOR_CONTEXT`)
- Test: `tests/test_fragments.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_fragments.py
def test_fragment_carries_offsets_and_context():
    text = "alpha one\n\nbeta two\n\ngamma three"
    result = fragments.extract(text, "beta", k=1, neighbours=0)
    frag = result[0]
    assert text[frag["char_start"] : frag["char_end"]] == frag["text"]
    assert frag["prefix"].endswith("alpha one\n\n")
    assert frag["suffix"].startswith("\n\ngamma three")


def test_context_is_capped():
    text = ("x" * 200) + "\n\nneedle here\n\n" + ("y" * 200)
    frag = fragments.extract(text, "needle", k=1, neighbours=0)[0]
    assert len(frag["prefix"]) == fragments.ANCHOR_CONTEXT
    assert len(frag["suffix"]) == fragments.ANCHOR_CONTEXT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_fragments.py -k "offsets or capped" -v`
Expected: FAIL — `KeyError: 'char_start'`

- [ ] **Step 3: Implement**

`split_paragraphs` currently discards positions. Add a positional variant and use it in
`extract`; keep `split_paragraphs` as-is because other code and tests use it.

```python
# src/research_state/fragments.py
ANCHOR_CONTEXT = 32


def split_paragraphs_with_spans(text: str) -> list[tuple[str, int, int]]:
    """Paragraphs plus their exact character span in the original text."""
    spans: list[tuple[str, int, int]] = []
    for match in re.finditer(r"[^\n](?:.*?)(?=\n\s*\n|\Z)", text or "", re.DOTALL):
        chunk = match.group()
        stripped = chunk.strip()
        if not stripped:
            continue
        start = match.start() + chunk.index(stripped)
        spans.append((stripped, start, start + len(stripped)))
    return spans
```

In `extract`, replace `paragraphs = split_paragraphs(text)` with:

```python
    spans = split_paragraphs_with_spans(text)
    paragraphs = [s[0] for s in spans]
```

and pass `spans` and `text` into `_windows`, whose body becomes:

```python
def _windows(
    spans: list[tuple[str, int, int]],
    text: str,
    hits: list[tuple[int, float]],
    neighbours: int,
) -> list[dict]:
    """Widen each hit by `neighbours` paragraphs and merge overlapping windows."""
    windows: list[dict] = []
    for index, score in hits:  # hits arrive best-first
        start = max(0, index - neighbours)
        end = min(len(spans) - 1, index + neighbours)
        for w in windows:
            if start <= w["end"] + 1 and end >= w["start"] - 1:
                w["start"] = min(w["start"], start)
                w["end"] = max(w["end"], end)
                break
        else:
            windows.append({"start": start, "end": end, "paragraph_index": index, "score": score})

    for w in windows:
        char_start = spans[w["start"]][1]
        char_end = spans[w["end"]][2]
        w["text"] = text[char_start:char_end]
        w["char_start"] = char_start
        w["char_end"] = char_end
        w["prefix"] = text[max(0, char_start - ANCHOR_CONTEXT) : char_start]
        w["suffix"] = text[char_end : char_end + ANCHOR_CONTEXT]
    return windows
```

Note the behaviour change: `text` is now the literal slice of the page, so a merged
window keeps the original blank lines between paragraphs instead of a normalised `\n\n`.

- [ ] **Step 4: Run the fragment tests**

Run: `uv run pytest tests/test_fragments.py -v`
Expected: PASS. If `test_extract_merges_adjacent_hits_into_one_fragment` fails on
whitespace, assert with `in` rather than equality — the slice is now verbatim.

- [ ] **Step 5: Commit**

```bash
git add src/research_state/fragments.py tests/test_fragments.py
git commit -m "feat: fragments carry offsets and surrounding context for re-anchoring"
```

---

### Task 3: Persist every fragment handed out

**Files:**
- Create: `src/research_state/issued.py`
- Test: `tests/test_issued.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_issued.py
import pytest

from research_state import db, issued


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "state.sqlite")
    issued.init_schema(c)
    return c


FRAG = {
    "text": "The constant k defaults to 60.",
    "char_start": 100,
    "char_end": 130,
    "prefix": "before ",
    "suffix": " after",
    "paragraph_index": 3,
    "score": 4.2,
}


def test_record_returns_a_stable_id(conn):
    first = issued.record(conn, url="https://e.com/a", fetched_at=1753500000, fragment=FRAG)
    second = issued.record(conn, url="https://e.com/a", fetched_at=1753500000, fragment=FRAG)
    assert first["fragment_id"] == second["fragment_id"]
    assert len(first["fragment_id"]) == 16


def test_id_changes_with_url_offset_or_fetch_time(conn):
    base = issued.record(conn, url="https://e.com/a", fetched_at=1753500000, fragment=FRAG)
    other_url = issued.record(conn, url="https://e.com/b", fetched_at=1753500000, fragment=FRAG)
    later = issued.record(conn, url="https://e.com/a", fetched_at=1753600000, fragment=FRAG)
    moved = issued.record(
        conn, url="https://e.com/a", fetched_at=1753500000, fragment={**FRAG, "char_start": 200}
    )
    ids = {base["fragment_id"], other_url["fragment_id"], later["fragment_id"], moved["fragment_id"]}
    assert len(ids) == 4


def test_get_returns_the_stored_snapshot(conn):
    fid = issued.record(conn, url="https://e.com/a", fetched_at=1753500000, fragment=FRAG)["fragment_id"]
    stored = issued.get(conn, fid)
    assert stored["exact"] == FRAG["text"]
    assert stored["url"] == "https://e.com/a"
    assert stored["prefix"] == "before "
    assert stored["char_start"] == 100


def test_get_returns_none_for_unknown_id(conn):
    assert issued.get(conn, "deadbeefdeadbeef") is None


def test_record_is_idempotent(conn):
    issued.record(conn, url="https://e.com/a", fetched_at=1753500000, fragment=FRAG)
    issued.record(conn, url="https://e.com/a", fetched_at=1753500000, fragment=FRAG)
    assert conn.execute("SELECT COUNT(*) FROM issued_fragments").fetchone()[0] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_issued.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'research_state.issued'`

- [ ] **Step 3: Implement**

```python
# src/research_state/issued.py
"""Fragments the server has handed to a model — the evidence side of a citation.

# ANCHOR: issued
# Role: every fragment returned by fragments_for is stored here, so a later brief
# can be checked against what was actually issued.
# In: url, the page's fetch time, one fragment dict from fragments.extract().
# Out: {fragment_id, ...} — a 16-hex id derived from url + offset + fetch time.
# The stored shape follows W3C Web Annotation Selectors: exact text (snapshot),
# prefix/suffix for re-anchoring, and offsets. The snapshot is deliberate: pages
# change, and a bare hash can only say "no match" (see docs/research/).
"""

from __future__ import annotations

import hashlib
import sqlite3

import structlog

from . import db

log = structlog.get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS issued_fragments (
    fragment_id TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    exact       TEXT NOT NULL,
    prefix      TEXT NOT NULL DEFAULT '',
    suffix      TEXT NOT NULL DEFAULT '',
    char_start  INTEGER NOT NULL,
    char_end    INTEGER NOT NULL,
    fetched_at  INTEGER NOT NULL,
    issued_at   INTEGER NOT NULL
);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    db.write(conn, lambda c: c.executescript(SCHEMA))


def make_id(url: str, char_start: int, fetched_at: int) -> str:
    raw = f"{url}\x00{char_start}\x00{fetched_at}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def record(conn: sqlite3.Connection, url: str, fetched_at: int, fragment: dict) -> dict:
    """Store one issued fragment and return it with its id attached."""
    fragment_id = make_id(url, fragment["char_start"], fetched_at)
    row = (
        fragment_id,
        url,
        fragment["text"],
        fragment.get("prefix", ""),
        fragment.get("suffix", ""),
        fragment["char_start"],
        fragment["char_end"],
        fetched_at,
    )
    db.write(
        conn,
        lambda c: c.execute(
            "INSERT INTO issued_fragments"
            " (fragment_id, url, exact, prefix, suffix, char_start, char_end, fetched_at, issued_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%s','now'))"
            " ON CONFLICT(fragment_id) DO NOTHING",
            row,
        ),
    )
    return {**fragment, "fragment_id": fragment_id}


def get(conn: sqlite3.Connection, fragment_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM issued_fragments WHERE fragment_id = ?", (fragment_id,)
    ).fetchone()
    return dict(row) if row is not None else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_issued.py -v`
Expected: PASS — 5 tests

- [ ] **Step 5: Commit**

```bash
git add src/research_state/issued.py tests/test_issued.py
git commit -m "feat: persist issued fragments with W3C-style anchors"
```

---

### Task 4: `fragments_for` issues ids

**Files:**
- Modify: `src/research_state/server.py` (`fragments_for`, `connection`)
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Write the failing test**

```python
# append inside test_full_stage_one_path, after the existing `frags` assertions
            assert all(len(f["fragment_id"]) == 16 for f in frags["fragments"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest -m smoke -v`
Expected: FAIL — `KeyError: 'fragment_id'`

- [ ] **Step 3: Implement**

In `connection()`, add `issued.init_schema(_conn)` next to `state.init_schema(_conn)`.
In `fragments_for`, replace the `found = ...` line and the return with:

```python
    found = fragments.extract(page["content"], query, k=k, neighbours=neighbours)
    conn = connection()
    found = [
        issued.record(conn, url=url, fetched_at=page["fetched_at"], fragment=f) for f in found
    ]
    metrics.record_fetch(
        conn,
        url=url,
        paragraphs_total=len(fragments.split_paragraphs(page["content"])),
        paragraphs_returned=len(found),
        chars_total=len(page["content"]),
        chars_returned=sum(len(f["text"]) for f in found),
    )
```

`metrics` does not exist yet — write Task 5 first if you are executing strictly in
order, or leave the `metrics.record_fetch` call out here and add it in Task 5. Prefer
the latter: one behaviour per commit.

- [ ] **Step 4: Run the suite**

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/research_state/server.py tests/test_smoke.py
git commit -m "feat: fragments_for issues and persists fragment ids"
```

---

### Task 5: Saved-context counter

**Files:**
- Create: `src/research_state/metrics.py`
- Test: `tests/test_metrics.py`
- Modify: `src/research_state/server.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_metrics.py
import pytest

from research_state import db, metrics


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "state.sqlite")
    metrics.init_schema(c)
    return c


def test_stats_on_an_empty_db(conn):
    assert metrics.stats(conn) == {
        "fetches": 0,
        "chars_total": 0,
        "chars_returned": 0,
        "chars_saved": 0,
        "saved_ratio": 0.0,
    }


def test_stats_sum_across_fetches(conn):
    metrics.record_fetch(
        conn, url="https://e.com/a", paragraphs_total=60, paragraphs_returned=2,
        chars_total=1000, chars_returned=100,
    )
    metrics.record_fetch(
        conn, url="https://e.com/b", paragraphs_total=20, paragraphs_returned=3,
        chars_total=1000, chars_returned=300,
    )
    s = metrics.stats(conn)
    assert s["fetches"] == 2
    assert s["chars_total"] == 2000
    assert s["chars_returned"] == 400
    assert s["chars_saved"] == 1600
    assert s["saved_ratio"] == 0.8
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_metrics.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'research_state.metrics'`

- [ ] **Step 3: Implement**

```python
# src/research_state/metrics.py
"""How much page text never reached the client's context.

# ANCHOR: metrics
# Role: one row per fragments_for call; stats() sums them.
# Why it exists from day one: this is the only non-rhetorical evidence that the
# project is worth anything, and it cannot be backfilled later.
"""

from __future__ import annotations

import sqlite3

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS fetch_metrics (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    url                 TEXT NOT NULL,
    paragraphs_total    INTEGER NOT NULL,
    paragraphs_returned INTEGER NOT NULL,
    chars_total         INTEGER NOT NULL,
    chars_returned      INTEGER NOT NULL,
    at                  INTEGER NOT NULL
);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    db.write(conn, lambda c: c.executescript(SCHEMA))


def record_fetch(
    conn: sqlite3.Connection,
    url: str,
    paragraphs_total: int,
    paragraphs_returned: int,
    chars_total: int,
    chars_returned: int,
) -> None:
    db.write(
        conn,
        lambda c: c.execute(
            "INSERT INTO fetch_metrics"
            " (url, paragraphs_total, paragraphs_returned, chars_total, chars_returned, at)"
            " VALUES (?, ?, ?, ?, ?, strftime('%s','now'))",
            (url, paragraphs_total, paragraphs_returned, chars_total, chars_returned),
        ),
    )


def stats(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS fetches, COALESCE(SUM(chars_total), 0) AS chars_total,"
        " COALESCE(SUM(chars_returned), 0) AS chars_returned FROM fetch_metrics"
    ).fetchone()
    total, returned = row["chars_total"], row["chars_returned"]
    return {
        "fetches": row["fetches"],
        "chars_total": total,
        "chars_returned": returned,
        "chars_saved": total - returned,
        "saved_ratio": round((total - returned) / total, 4) if total else 0.0,
    }
```

- [ ] **Step 4: Wire it into the server**

Add `metrics.init_schema(_conn)` in `connection()`, add the `metrics.record_fetch(...)`
call in `fragments_for` exactly as shown in Task 4 Step 3, and add the tool:

```python
@mcp.tool
def research_state_stats() -> dict:
    """How much page text this server has kept out of the context so far."""
    return metrics.stats(connection())
```

- [ ] **Step 5: Run the suite**

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/research_state/metrics.py tests/test_metrics.py src/research_state/server.py
git commit -m "feat: count how much page text never reaches the context"
```

---

### Task 6: Claim validation — the invariant

The core of the stage. A `fact` needs a `fragment_id` **and** a `quote` literally present
in that fragment. Existence of a citation alone is worth nothing: measured link validity
is >94% while factual support is 39–77% (`docs/research/2026-07-26-citation-enforcement.md`).

**Files:**
- Create: `src/research_state/briefs.py`
- Test: `tests/test_briefs.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_briefs.py
import pytest

from research_state import briefs, db, issued


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "state.sqlite")
    issued.init_schema(c)
    briefs.init_schema(c)
    return c


@pytest.fixture
def fid(conn):
    return issued.record(
        conn,
        url="https://e.com/a",
        fetched_at=1753500000,
        fragment={
            "text": "The constant k defaults to 60 in Elasticsearch.",
            "char_start": 10,
            "char_end": 56,
            "prefix": "",
            "suffix": "",
        },
    )["fragment_id"]


def test_a_fact_with_a_verbatim_quote_passes(conn, fid):
    problems = briefs.validate_claims(
        conn, [{"text": "k is 60", "kind": "fact", "fragment_id": fid, "quote": "k defaults to 60"}]
    )
    assert problems == []


def test_a_quote_absent_from_the_fragment_is_rejected(conn, fid):
    problems = briefs.validate_claims(
        conn,
        [{"text": "k is 42", "kind": "fact", "fragment_id": fid, "quote": "k defaults to 42"}],
    )
    assert problems[0]["reason"] == "quote_not_found"


def test_one_character_off_is_still_rejected(conn, fid):
    problems = briefs.validate_claims(
        conn,
        [{"text": "k is 60", "kind": "fact", "fragment_id": fid, "quote": "k defaults to 6O"}],
    )
    assert problems[0]["reason"] == "quote_not_found"


def test_whitespace_and_case_differences_are_tolerated(conn, fid):
    problems = briefs.validate_claims(
        conn,
        [{"text": "k is 60", "kind": "fact", "fragment_id": fid, "quote": "K  defaults\nto 60"}],
    )
    assert problems == []


def test_unknown_fragment_is_rejected(conn):
    problems = briefs.validate_claims(
        conn, [{"text": "x", "kind": "fact", "fragment_id": "0" * 16, "quote": "x"}]
    )
    assert problems[0]["reason"] == "unknown_fragment"


def test_a_fact_without_a_citation_is_rejected(conn):
    problems = briefs.validate_claims(conn, [{"text": "x", "kind": "fact"}])
    assert problems[0]["reason"] == "missing_citation"


def test_an_assumption_needs_no_citation(conn):
    assert briefs.validate_claims(conn, [{"text": "probably x", "kind": "assumption"}]) == []


def test_an_unknown_kind_is_rejected(conn):
    problems = briefs.validate_claims(conn, [{"text": "x", "kind": "guess"}])
    assert problems[0]["reason"] == "unknown_kind"


def test_every_bad_claim_is_reported_not_just_the_first(conn):
    problems = briefs.validate_claims(
        conn, [{"text": "a", "kind": "fact"}, {"text": "b", "kind": "fact"}]
    )
    assert len(problems) == 2
    assert [p["index"] for p in problems] == [0, 1]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_briefs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'research_state.briefs'`

- [ ] **Step 3: Implement validation only** (storage comes in Task 7)

```python
# src/research_state/briefs.py
"""Briefs: claim validation, markdown rendering, and the searchable library.

# ANCHOR: briefs
# Role: the invariant lives here — a factual claim without a verbatim quote from
# a fragment this server issued is not storable.
# In: a connection, a list of claim dicts. Out: a list of problems (empty = valid).
# Quote matching is literal after whitespace/case normalisation only: checking
# "a citation exists" is worth nothing on its own (see docs/research/).
"""

from __future__ import annotations

import re
import sqlite3

import structlog

from . import issued

log = structlog.get_logger(__name__)

KINDS = ("fact", "assumption")
_WS = re.compile(r"\s+")


def init_schema(conn: sqlite3.Connection) -> None:
    """Placeholder until Task 7 adds the brief tables."""
    return None


def _normalise(text: str) -> str:
    return _WS.sub(" ", text or "").strip().casefold()


def validate_claims(conn: sqlite3.Connection, claims: list[dict]) -> list[dict]:
    """Return one problem dict per unacceptable claim. Empty list means valid."""
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
            problems.append({"index": index, "reason": "missing_citation", "text": claim.get("text")})
            continue
        fragment = issued.get(conn, fragment_id)
        if fragment is None:
            problems.append(
                {"index": index, "reason": "unknown_fragment", "fragment_id": fragment_id}
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_briefs.py -v`
Expected: PASS — 9 tests

- [ ] **Step 5: Commit**

```bash
git add src/research_state/briefs.py tests/test_briefs.py
git commit -m "feat: reject any factual claim without a verbatim quote"
```

---

### Task 7: Store a brief as a file, index it in FTS5

**Files:**
- Modify: `src/research_state/briefs.py`
- Test: `tests/test_briefs.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_briefs.py
def test_save_writes_a_markdown_file_and_returns_its_path(conn, fid, tmp_path):
    result = briefs.save(
        conn,
        job_id="job1",
        topic="How does RRF work",
        summary="RRF fuses ranked lists.",
        claims=[{"text": "k is 60", "kind": "fact", "fragment_id": fid, "quote": "k defaults to 60"}],
        gaps=["nothing on Russian-language sources"],
        brief_dir=tmp_path,
        today="2026-07-26",
    )
    path = tmp_path / "2026-07-26-how-does-rrf-work.md"
    assert result["path"] == str(path)
    body = path.read_text()
    assert "RRF fuses ranked lists." in body
    assert "k is 60" in body
    assert "https://e.com/a" in body
    assert "nothing on Russian-language sources" in body


def test_assumptions_are_labelled_in_the_file(conn, tmp_path):
    briefs.save(
        conn, job_id="j", topic="T", summary="s",
        claims=[{"text": "likely true", "kind": "assumption"}],
        gaps=[], brief_dir=tmp_path, today="2026-07-26",
    )
    body = (tmp_path / "2026-07-26-t.md").read_text()
    assert "assumption" in body.lower()


def test_save_refuses_an_invalid_claim_and_writes_nothing(conn, tmp_path):
    with pytest.raises(briefs.InvalidBrief) as exc:
        briefs.save(
            conn, job_id="j", topic="T", summary="s",
            claims=[{"text": "x", "kind": "fact"}],
            gaps=[], brief_dir=tmp_path, today="2026-07-26",
        )
    assert exc.value.problems[0]["reason"] == "missing_citation"
    assert list(tmp_path.iterdir()) == []


def test_saving_the_same_topic_twice_does_not_overwrite(conn, tmp_path):
    for _ in range(2):
        briefs.save(
            conn, job_id="j", topic="T", summary="s", claims=[], gaps=[],
            brief_dir=tmp_path, today="2026-07-26",
        )
    assert len(list(tmp_path.iterdir())) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_briefs.py -k save -v`
Expected: FAIL — `AttributeError: module 'research_state.briefs' has no attribute 'save'`

- [ ] **Step 3: Implement**

Replace the placeholder `init_schema` and add the rest:

```python
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
    topic, summary, claims, tokenize='unicode61'
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


class InvalidBrief(ValueError):
    """The brief has claims that cannot be stored. `problems` says which."""

    def __init__(self, problems: list[dict]) -> None:
        super().__init__(f"{len(problems)} unacceptable claim(s)")
        self.problems = problems


def init_schema(conn: sqlite3.Connection) -> None:
    db.write(conn, lambda c: c.executescript(SCHEMA))


def _slug(topic: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", topic, flags=re.UNICODE).strip().lower()
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug[:60] or "brief"


def _unique_path(brief_dir: Path, today: str, topic: str) -> Path:
    brief_dir.mkdir(parents=True, exist_ok=True)
    base = f"{today}-{_slug(topic)}"
    path = brief_dir / f"{base}.md"
    counter = 2
    while path.exists():
        path = brief_dir / f"{base}-{counter}.md"
        counter += 1
    return path


def render(topic: str, summary: str, claims: list[dict], gaps: list[str], sources: dict) -> str:
    lines = [f"# {topic}", "", summary, "", "## Claims", ""]
    for claim in claims:
        if claim.get("kind") == "assumption":
            lines.append(f"- {claim['text']} _(assumption — no source)_")
            continue
        url = sources.get(claim["fragment_id"], "")
        lines.append(f"- {claim['text']}")
        lines.append(f'  > {claim["quote"]}')
        lines.append(f"  — <{url}> `{claim['fragment_id']}`")
    if gaps:
        lines += ["", "## Gaps", ""] + [f"- {g}" for g in gaps]
    lines += ["", "## Sources", ""] + [f"- <{u}>" for u in sorted(set(sources.values()))]
    return "\n".join(lines) + "\n"


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
    """Validate, write the markdown file, index it. Raises InvalidBrief."""
    problems = validate_claims(conn, claims)
    if problems:
        raise InvalidBrief(problems)

    sources = {}
    for claim in claims:
        if claim.get("fragment_id"):
            fragment = issued.get(conn, claim["fragment_id"])
            sources[claim["fragment_id"]] = fragment["url"] if fragment else ""

    path = _unique_path(brief_dir, today, topic)
    path.write_text(render(topic, summary, claims, gaps, sources), encoding="utf-8")

    brief_id = hashlib.sha256(str(path).encode()).hexdigest()[:16]
    claim_blob = "\n".join(c["text"] for c in claims)

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
        c.execute(
            "INSERT INTO briefs_fts (rowid, topic, summary, claims) VALUES"
            " ((SELECT COALESCE(MAX(rowid), 0) + 1 FROM briefs_fts), ?, ?, ?)",
            (topic, summary, claim_blob),
        )

    db.write(conn, op)
    log.info("briefs.saved", brief_id=brief_id, claims=len(claims), gaps=len(gaps))
    return {"brief_id": brief_id, "path": str(path), "claims": len(claims), "gaps": len(gaps)}
```

Add to the imports at the top of `briefs.py`: `import hashlib`, `from pathlib import Path`,
and `from . import db, issued`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_briefs.py -v`
Expected: PASS — 13 tests

- [ ] **Step 5: Commit**

```bash
git add src/research_state/briefs.py tests/test_briefs.py
git commit -m "feat: store a validated brief as markdown and index it"
```

---

### Task 8: Search the brief library

**Files:**
- Modify: `src/research_state/briefs.py`
- Test: `tests/test_briefs.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_briefs.py
def test_search_finds_a_brief_by_topic(conn, tmp_path):
    briefs.save(
        conn, job_id="j", topic="Reciprocal rank fusion", summary="RRF fuses lists.",
        claims=[], gaps=[], brief_dir=tmp_path, today="2026-07-26",
    )
    hits = briefs.search(conn, "rank fusion")
    assert len(hits) == 1
    assert hits[0]["topic"] == "Reciprocal rank fusion"
    assert hits[0]["path"].endswith(".md")
    assert hits[0]["age_days"] == 0


def test_search_returns_nothing_for_an_unrelated_query(conn, tmp_path):
    briefs.save(
        conn, job_id="j", topic="Reciprocal rank fusion", summary="s",
        claims=[], gaps=[], brief_dir=tmp_path, today="2026-07-26",
    )
    assert briefs.search(conn, "квантовая хромодинамика") == []


def test_search_survives_operator_characters(conn, tmp_path):
    briefs.save(
        conn, job_id="j", topic="RRF", summary="s", claims=[], gaps=[],
        brief_dir=tmp_path, today="2026-07-26",
    )
    assert briefs.search(conn, 'NEAR "AND" (rrf) *') is not None


def test_search_never_returns_the_brief_body(conn, tmp_path):
    briefs.save(
        conn, job_id="j", topic="RRF", summary="s" * 5000, claims=[], gaps=[],
        brief_dir=tmp_path, today="2026-07-26",
    )
    hit = briefs.search(conn, "rrf")[0]
    assert len(hit["snippet"]) < 500
    assert "body" not in hit
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_briefs.py -k search -v`
Expected: FAIL — `AttributeError: module 'research_state.briefs' has no attribute 'search'`

- [ ] **Step 3: Implement**

`fragments._fts_query` already turns a raw query into a safe MATCH expression — reuse it
rather than writing a second one.

```python
def search(conn: sqlite3.Connection, query: str, limit: int = 3) -> list[dict]:
    """Rank past briefs. Returns metadata and a snippet, never the brief body."""
    match = fragments_module._fts_query(query)
    if not match:
        return []
    try:
        rows = conn.execute(
            "SELECT f.topic AS topic,"
            " snippet(briefs_fts, 1, '', '', '…', 20) AS snippet,"
            " bm25(briefs_fts) AS rank"
            " FROM briefs_fts f WHERE briefs_fts MATCH ? ORDER BY rank LIMIT ?",
            (match, limit),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        log.warning("briefs.search_failed", error=str(exc))
        return []

    hits = []
    for row in rows:
        meta = conn.execute(
            "SELECT path, created, summary FROM briefs WHERE topic = ? ORDER BY created DESC LIMIT 1",
            (row["topic"],),
        ).fetchone()
        if meta is None:
            continue
        age_days = int((int(time.time()) - meta["created"]) // 86400)
        hits.append(
            {
                "topic": row["topic"],
                "path": meta["path"],
                "age_days": age_days,
                "snippet": row["snippet"][:400],
            }
        )
    return hits
```

Add imports: `import time` and `from . import fragments as fragments_module`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_briefs.py -v`
Expected: PASS — 17 tests

- [ ] **Step 5: Commit**

```bash
git add src/research_state/briefs.py tests/test_briefs.py
git commit -m "feat: search past briefs with FTS5, returning metadata not bodies"
```

---

### Task 9: Gaps must be accounted for

**Files:**
- Modify: `src/research_state/state.py`
- Test: `tests/test_state.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_state.py
def test_open_subquestions_are_reported(conn):
    job_id = state.start_job(conn, "topic")["job_id"]
    state.set_plan(conn, job_id, ["a", "b"])
    state.mark(conn, job_id, 1, "answered")
    gaps = state.gaps(conn, job_id)
    assert gaps["open"] == [{"subq_id": 2, "text": "b"}]
    assert gaps["closed"] == 1
    assert gaps["total"] == 2


def test_gaps_on_a_fully_closed_job(conn):
    job_id = state.start_job(conn, "topic")["job_id"]
    state.set_plan(conn, job_id, ["a"])
    state.mark(conn, job_id, 1, "answered")
    assert state.gaps(conn, job_id)["open"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_state.py -k gap -v`
Expected: FAIL — `AttributeError: module 'research_state.state' has no attribute 'gaps'`

- [ ] **Step 3: Implement**

```python
# src/research_state/state.py
def gaps(conn: sqlite3.Connection, job_id: str) -> dict:
    """Which subquestions are still open — the input to an honest finish."""
    job = get_job(conn, job_id)
    open_subqs = [
        {"subq_id": s["subq_id"], "text": s["text"]}
        for s in job["subquestions"]
        if s["status"] == "open"
    ]
    return {
        "job_id": job_id,
        "topic": job["topic"],
        "open": open_subqs,
        "closed": len(job["subquestions"]) - len(open_subqs),
        "total": len(job["subquestions"]),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_state.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/research_state/state.py tests/test_state.py
git commit -m "feat: report open subquestions as gaps"
```

---

### Task 10: `research_finish`, `brief_search`, `verify_claim` as tools

**Files:**
- Modify: `src/research_state/server.py`
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_smoke.py — a second smoke test, same `wired` fixture
@pytest.mark.smoke
def test_brief_lifecycle(wired, tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_BRIEF_DIR", str(tmp_path / "briefs"))

    async def scenario() -> None:
        async with Client(server.mcp) as client:
            job_id = (await _call(client, "research_start", {"topic": "how does RRF work"}))["job_id"]
            await _call(client, "research_plan", {"job_id": job_id, "subquestions": ["default k"]})
            frags = await _call(
                client, "fragments_for",
                {"url": "https://example.com/rrf", "query": "default value of k", "k": 1},
            )
            fid = frags["fragments"][0]["fragment_id"]

            # a fabricated quote must be refused, and nothing written
            refused = await _call(
                client, "research_finish",
                {
                    "job_id": job_id, "summary": "s",
                    "claims": [{"text": "k is 42", "kind": "fact", "fragment_id": fid,
                                "quote": "k defaults to 42"}],
                    "gaps": ["none"],
                },
            )
            assert refused["error"] == "invalid_brief"
            assert refused["problems"][0]["reason"] == "quote_not_found"

            # an honest finish is refused while a subquestion is open and unaccounted for
            await _call(client, "research_mark", {"job_id": job_id, "subq_id": 1, "answer": "60"})

            saved = await _call(
                client, "research_finish",
                {
                    "job_id": job_id, "summary": "RRF fuses ranked lists.",
                    "claims": [{"text": "k defaults to 60", "kind": "fact", "fragment_id": fid,
                                "quote": "constant k defaults to 60"}],
                    "gaps": [],
                },
            )
            assert saved["path"].endswith(".md")
            assert "brief" not in saved or len(str(saved)) < 2000

            hits = await _call(client, "brief_search", {"query": "RRF"})
            assert hits["briefs"][0]["path"] == saved["path"]

            stats = await _call(client, "research_state_stats", {})
            assert stats["chars_saved"] > 0

    asyncio.run(scenario())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest -m smoke -v`
Expected: FAIL — unknown tool `research_finish`

- [ ] **Step 3: Implement**

In `connection()` add `briefs.init_schema(_conn)`. Then:

```python
@mcp.tool
def research_gaps(job_id: str) -> dict:
    """Which subquestions of this job are still open.

    Call this before finishing. Anything still open must either be closed with
    `research_mark` or written into `gaps` when you finish — a research run does
    not get to quietly stop when the model feels done.

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

    Work out your conclusions first, in prose, and only then call this to record
    them — assembling findings while filling in fields measurably costs reasoning
    quality.

    Args:
        job_id: From `research_start`.
        summary: The synthesis, in prose. Not claim-checked.
        claims: `{text, kind, fragment_id, quote}` per claim. `kind` is "fact" or
            "assumption". A fact needs `fragment_id` from `fragments_for` and a
            `quote` copied verbatim out of that fragment — the server checks the
            quote is really there, so pointing at an unrelated fragment fails.
            An assumption needs neither and is printed as an assumption.
        gaps: What you could not answer. Required if any subquestion is still open.
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
            "hint": "close them with research_mark, or list what you could not find in `gaps`",
        }

    try:
        result = briefs.save(
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
    return result


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
    return {
        **found,
        "verdict": "unverified",
        "confidence": None,
        "method": "quote-match",
    }
```

Change `research_start` to return real neighbours:

```python
    return {**job, "similar_briefs": briefs.search(connection(), topic)}
```

Add imports: `from datetime import date` and `briefs` to the `from . import` line.

- [ ] **Step 4: Run the suite**

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/research_state/server.py tests/test_smoke.py
git commit -m "feat: research_finish, brief_search and verify_claim tools"
```

---

### Task 11: The skill

**Files:**
- Create: `~/.claude/skills/deep-research/SKILL.md`

Not a code task; no tests. The skill encodes the process the server cannot enforce.

- [ ] **Step 1: Write the skill**

Frontmatter `name: deep-research`, description covering "исследовать тему", "ресёрч",
"разобраться в вопросе". Body sections:

1. **Always start with `research_start`** — if a returned brief answers the question, read
   the file and stop. A repeat question must cost nothing.
2. **Plan 3–5 subquestions** (`research_plan`), 5–8 for deep. Narrow the search area:
   `include_domains` for vacancies (hh.ru), regulations (pravo.gov.ru, consultant.ru,
   garant.ru), vendor docs; `category="paper"` for science; `category="news"` with
   `freshness="day"` for news. Always name the year in the query.
3. **Delegate fetching** to the `research` subagent — it returns urls, never page text.
   One simple question does not get a subagent.
4. **`fragments_for` per url and subquestion.** Never `fetch` a page into this context.
5. **`research_mark` per closed subquestion; `research_gaps` before finishing.**
6. **Write the synthesis in prose first, then call `research_finish`.** Copy quotes
   verbatim out of the fragments — an approximate quote is rejected.
7. Depth table: quick (no plan, snippets only, 3–5 lines), normal (3–5 subquestions, ≤5
   pages, ≤1500 tokens), deep (5–8 subquestions, second pass over what stayed open, ≤15
   pages, full dump to file plus a gaps section).

- [ ] **Step 2: Verify the skill loads**

Run: `ls ~/.claude/skills/deep-research/SKILL.md`
Then in a new session confirm it appears in the skill list.

- [ ] **Step 3: Commit** (the skill lives outside the repo; commit a copy for the public repo)

```bash
mkdir -p skill && cp ~/.claude/skills/deep-research/SKILL.md skill/SKILL.md
git add skill/SKILL.md && git commit -m "docs: ship the research skill with the server"
```

---

### Task 12: Rewrite the `research` subagent — GATED

**Files:**
- Modify: `~/.claude/agents/research.md`

**Do not start this task without Petr's explicit consent from the console.** It is in the
project constitution and in the Definition of Done for a reason.

- [ ] **Step 1: Ask for consent, in the console, naming the file**
- [ ] **Step 2: Rewrite** — the subagent becomes a fetcher: it searches, fetches pages into
      the free-search-mcp cache, and returns a list of `{url, title}` with one line each on
      why the page looks relevant. It must not summarise page content and must not return
      page text. Remove the stale line about Tavily credits with a date in it.
- [ ] **Step 3: Run one real research end-to-end** and check the main context never
      received page text.
- [ ] **Step 4: Commit** a copy under `agent/research.md` in this repo.

---

### Task 13: Close out the stage

- [ ] **Step 1: Update `PLAN.md`** §6 status, §7 checkboxes, §10 Definition of Done, and
      move anything still unresolved into §8.
- [ ] **Step 2: Run the full suite one last time**

Run: `uv run pytest -q`
Expected: PASS, no skips

- [ ] **Step 3: Check the saved-context number is real**

Run: `uv run python -c "from research_state import server; print(server.research_state_stats())"`
Expected: `chars_saved` well above zero after a real research run.

- [ ] **Step 4: Commit**

```bash
git add PLAN.md && git commit -m "docs: stage 2 complete"
```

---

## Self-review notes

- Spec coverage: hard invariant → Task 6; verbatim quote → Task 6; two-step brief → the
  `research_finish` docstring in Task 10 plus the skill in Task 11; fragment anchors →
  Tasks 2–3; brief file as source of truth → Task 7; FTS5 index derived → Tasks 7–8;
  gaps → Tasks 9–10; metrics → Task 5; orchestration → Tasks 11–12; error payloads →
  Task 10; three re-anchoring statuses → **not implemented in stage 2**, deliberately:
  nothing re-verifies old fragments yet, and the data needed for it (snapshot, prefix,
  suffix) is stored from Task 3 so the check can be added without a migration.
- Type consistency: `fragment_id` (16 hex) everywhere; `kind` ∈ {fact, assumption};
  problems always `{index, reason, ...}`; `briefs.save` raises `InvalidBrief`, the tool
  converts it to `{"error": "invalid_brief", "problems": [...]}`.
