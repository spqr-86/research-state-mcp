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
# A fact also carries its *binding* (what it is true of) and its *source class*,
# because a brief does not have one shelf life: a mechanism never expires, a
# benchmark number expires with the model it was measured on, a price expires
# with the price list. Binding drives `recheck_after`; source class drives
# warnings only — the server has no model and cannot judge truth.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import structlog

from . import db, issued
from . import fragments as fragments_module

log = structlog.get_logger(__name__)

KINDS = ("fact", "assumption")
_WS = re.compile(r"\s+")
_ELLIPSIS = re.compile(r"\s*(?:…|\.\.\.)\s*")

# Measured, not chosen by feel (eval/quote_floor.py over 500 real paragraphs):
# below five words a quote's opening still occurs in another fragment far too
# often to be evidence — 55% at two words, 18% at three, 7.4% at four, 4.2% at
# five — while the cost, real sentences shorter than the floor, is 4.2% at five
# and climbs steeply after. Five is where the two curves cross.
MIN_QUOTE_WORDS = 5

# What a claim is true *of*. "timeless" is a mechanism, a definition or a past
# event; "dataset" is a number measured on a named model/dataset/benchmark
# version; "vendor" is a price, a product default or a limit; "world" is a
# statement about the current state of the world or a leaderboard, which can go
# silently false overnight.
BINDINGS = ("timeless", "dataset", "vendor", "world")

# CONVENTION, not a measurement: nobody has measured how long a benchmark number
# or a price stays true, and these horizons are a starting default the author is
# expected to override when they know better. They exist so that an unmarked
# fact still gets *some* recheck date instead of none.
RECHECK_HORIZON_DAYS = {"dataset": 365, "vendor": 90, "world": 30}

# Where the claim's evidence sits on the retelling ladder: "primary" is the paper,
# the official doc or the dataset itself; "vendor" is a vendor page about its own
# product; "secondary" is a retelling, a blog post or a roundup.
SOURCE_CLASSES = ("primary", "vendor", "secondary")

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
    binding       TEXT,
    bound_to      TEXT,
    recheck_after TEXT,
    source_class  TEXT,
    PRIMARY KEY (brief_id, idx)
);
"""

# Columns added after briefs were already in the wild. Old rows keep NULL and are
# reported as "unlabelled" rather than migrated to a guess.
_ADDED_CLAIM_COLUMNS = ("binding", "bound_to", "recheck_after", "source_class")


def init_schema(conn: sqlite3.Connection) -> None:
    db.write(conn, lambda c: c.executescript(SCHEMA))
    _migrate_claim_columns(conn)


def _migrate_claim_columns(conn: sqlite3.Connection) -> None:
    """Add missing `brief_claims` columns in place. Idempotent by design."""
    present = {row["name"] for row in conn.execute("PRAGMA table_info(brief_claims)")}
    missing = [name for name in _ADDED_CLAIM_COLUMNS if name not in present]
    if not missing:
        return

    def op(c: sqlite3.Connection) -> None:
        for name in missing:
            c.execute(f"ALTER TABLE brief_claims ADD COLUMN {name} TEXT")

    db.write(conn, op)
    log.info("briefs.migrated", columns=missing)


def _normalise(text: str) -> str:
    """Whitespace and case only — never punctuation. The check must stay literal."""
    return _WS.sub(" ", text or "").strip().casefold()


def quote_words(quote: str | None) -> int:
    """Words a quote actually carries — ellipsis markers are not words."""
    return sum(len(part.split()) for part in _ELLIPSIS.split(quote or ""))


def quote_supported(exact: str, quote: str | None) -> bool:
    """True when `quote` is carried verbatim by `exact`.

    An ellipsis (`…` or `...`) may stand for elided text — standard citation
    practice, and refused in 479 of 500 real cases before this existed
    (eval/RESULTS.md). Every piece around the ellipsis must still occur
    verbatim, and the pieces must occur **in order and without reusing the same
    words**, which is what keeps a quote welded out of two distant sentences
    from passing.
    """
    haystack = _normalise(exact)
    pieces = [
        piece for piece in (_normalise(part) for part in _ELLIPSIS.split(quote or "")) if piece
    ]
    if not pieces:
        return False
    cursor = 0
    for piece in pieces:
        found = haystack.find(piece, cursor)
        if found < 0:
            return False
        cursor = found + len(piece)
    return True


def default_recheck_after(binding: str | None, today: str) -> str | None:
    """The recheck date a binding implies, counted from the brief's own date.

    None for `timeless` and for anything unmarked: a mechanism has no expiry, and
    inventing one for an unlabelled claim would be a guess dressed as data.
    """
    days = RECHECK_HORIZON_DAYS.get(binding or "")
    if days is None:
        return None
    return (date.fromisoformat(today) + timedelta(days=days)).isoformat()


def _normalise_date(value: str | None) -> str | None:
    """`YYYY-MM-DD` or None. Validation has already refused anything unparsable."""
    text = (value or "").strip()
    if not text:
        return None
    return date.fromisoformat(text).isoformat()


def _binding_problem(index: int, claim: dict) -> dict | None:
    """Binding problems. Required on a fact, optional but still checked otherwise."""
    kind = claim.get("kind", "fact")
    binding = claim.get("binding")
    bound_to = (claim.get("bound_to") or "").strip()
    if binding is None:
        if kind == "fact":
            return {"index": index, "reason": "missing_binding", "expected": list(BINDINGS)}
        return None
    if binding not in BINDINGS:
        return {
            "index": index,
            "reason": "unknown_binding",
            "binding": binding,
            "expected": list(BINDINGS),
        }
    if binding == "timeless":
        # An author who names what a "timeless" fact is bound to is contradicting
        # themselves — surface it instead of silently dropping one of the two.
        if bound_to:
            return {"index": index, "reason": "bound_to_on_timeless", "bound_to": bound_to}
        return None
    if not bound_to:
        return {"index": index, "reason": "missing_bound_to", "binding": binding}
    return None


def _recheck_after_problem(index: int, claim: dict) -> dict | None:
    """The date's format is checked whatever the binding is — even none at all.

    `freshness()` compares recheck dates as strings, which is only safe while
    every stored date is normalised ISO. An unparsable date left in the DB would
    make the fact either permanently due or permanently fresh, silently.
    """
    recheck_after = (claim.get("recheck_after") or "").strip()
    if not recheck_after:
        return None
    try:
        date.fromisoformat(recheck_after)
    except ValueError:
        return {"index": index, "reason": "bad_recheck_after", "recheck_after": recheck_after}
    if claim.get("binding") == "timeless":
        return {
            "index": index,
            "reason": "recheck_after_on_timeless",
            "recheck_after": recheck_after,
        }
    return None


def _source_class_problem(index: int, claim: dict) -> dict | None:
    source_class = claim.get("source_class")
    if source_class is None:
        if claim.get("kind", "fact") == "fact":
            return {
                "index": index,
                "reason": "missing_source_class",
                "expected": list(SOURCE_CLASSES),
            }
        return None
    if source_class not in SOURCE_CLASSES:
        return {
            "index": index,
            "reason": "unknown_source_class",
            "source_class": source_class,
            "expected": list(SOURCE_CLASSES),
        }
    return None


def _label_problem(index: int, claim: dict) -> dict | None:
    """First problem among the labels — binding, recheck date, source class."""
    for problem in (
        _binding_problem(index, claim),
        _recheck_after_problem(index, claim),
        _source_class_problem(index, claim),
    ):
        if problem is not None:
            return problem
    return None


def validate_claims(conn: sqlite3.Connection, claims: list[dict]) -> list[dict]:
    """Return one problem per unacceptable claim. An empty list means valid."""
    problems: list[dict] = []
    for index, claim in enumerate(claims):
        kind = claim.get("kind", "fact")
        if kind not in KINDS:
            problems.append({"index": index, "reason": "unknown_kind", "kind": kind})
            continue
        if kind == "assumption":
            # Labels are optional here, but a wrong label is still a wrong label.
            problem = _label_problem(index, claim)
            if problem is not None:
                problems.append(problem)
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
        words = quote_words(quote)
        if words < MIN_QUOTE_WORDS:
            problems.append(
                {
                    "index": index,
                    "reason": "quote_too_short",
                    "quote": quote,
                    "words": words,
                    "minimum": MIN_QUOTE_WORDS,
                }
            )
            continue
        if not quote_supported(fragment["exact"], quote):
            problems.append(
                {
                    "index": index,
                    "reason": "quote_not_found",
                    "fragment_id": fragment_id,
                    "quote": quote,
                }
            )
            continue
        problem = _label_problem(index, claim)
        if problem is not None:
            problems.append(problem)
    return problems


def _domain(url: str) -> str:
    """Host without `www.` — one site is one source, however it spells itself.

    The url comes from the client, so a scheme is not guaranteed, and `urlsplit`
    mis-reads schemeless input two different ways: "example.com/a" lands entirely
    in `path`, while "example.com:8080/a" is read as scheme "example.com" with
    path "8080/a". Re-parsing the remainder as "//host/..." makes both behave
    like a normal url, which keeps such a claim inside the count instead of
    dropping it (or counting the port as the host) and quietly weakening every
    domain signal below.
    """
    stripped = url.strip()
    parts = urlsplit(stripped)
    if not parts.netloc:
        _, separator, remainder = stripped.partition("//")
        parts = urlsplit("//" + (remainder if separator else stripped))
    return (parts.hostname or "").strip().lower().removeprefix("www.")


def source_warnings(conn: sqlite3.Connection, claims: list[dict]) -> list[dict]:
    """Mechanical suspicion about the evidence, counted without any model.

    Three things are countable, and none of them is a reason to refuse a brief:

    - `secondary_only` — the fact rests on a retelling.
    - `unique_domain` (per claim) — **no other fact in this brief rests on this
      domain**, i.e. there is no cross-confirmation inside the brief. It is not a
      count of sources under the claim: a claim carries one quote from one
      fragment, so that count is mechanically always 1. On a one-fact brief this
      therefore always fires, which is correct and not a bug.
    - `single_domain_brief` (per brief, `index` is None) — every fact in the brief
      comes from the same site, so the whole brief is one source wearing several
      hats. This is the case the backlog item was actually about.

    Only facts are counted: an assumption from the same domain adds no independent
    confirmation, so letting it mute the signal would be a lie.

    A fluent single-source falsehood is not detectable here at all — the rule
    "a fact that changes a decision gets read in the primary source" stays with
    the human (CLAUDE.md backlog, 27.07).
    """
    domains: dict[int, str] = {}
    for index, claim in enumerate(claims):
        fragment_id = claim.get("fragment_id")
        if claim.get("kind", "fact") != "fact" or not fragment_id:
            continue
        fragment = issued.get(conn, fragment_id)
        if fragment is None:
            continue
        domain = _domain(fragment["url"])
        if domain:
            domains[index] = domain
    counts: dict[str, int] = {}
    for domain in domains.values():
        counts[domain] = counts.get(domain, 0) + 1

    warnings: list[dict] = []
    for index, claim in enumerate(claims):
        if claim.get("kind", "fact") != "fact":
            continue
        if claim.get("source_class") == "secondary":
            warnings.append(
                {
                    "index": index,
                    "reason": "secondary_only",
                    "text": claim.get("text"),
                    "domain": domains.get(index),
                    "hint": "a retelling; read the primary source before this decides anything",
                }
            )
        domain = domains.get(index)
        if domain and counts[domain] == 1:
            warnings.append(
                {
                    "index": index,
                    "reason": "unique_domain",
                    "text": claim.get("text"),
                    "domain": domain,
                    "hint": "no other fact in this brief rests on this domain",
                }
            )
    if len(domains) >= 2 and len(counts) == 1:
        only_domain = next(iter(counts))
        warnings.append(
            {
                "index": None,
                "reason": "single_domain_brief",
                "domain": only_domain,
                "facts": len(domains),
                "hint": f"every fact in this brief comes from {only_domain} — one source",
            }
        )
    return warnings


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


def _claim_notes(claim: dict) -> list[str]:
    """The lines under a fact that say what it is true of and how solid it is."""
    notes: list[str] = []
    binding = claim.get("binding")
    if binding == "timeless":
        notes.append("_(timeless — does not expire)_")
    elif binding:
        bound_to = claim.get("bound_to") or "unspecified"
        recheck_after = claim.get("recheck_after")
        tail = f", recheck after {recheck_after}" if recheck_after else ""
        notes.append(f"_(bound to {binding}: {bound_to}{tail})_")
    if claim.get("source_class") == "secondary":
        notes.append("_(retelling — the primary source was not read)_")
    elif claim.get("source_class") == "vendor":
        notes.append("_(vendor page about its own product)_")
    return notes


def render(topic: str, summary: str, claims: list[dict], gaps: list[str], sources: dict) -> str:
    """The markdown a human reads. Generated, never hand-written."""
    lines = [f"# {topic}", "", summary, ""]
    if claims:
        lines += ["## Claims", ""]
        for claim in claims:
            if claim.get("kind") == "assumption":
                lines.append(f"- {claim['text']} _(assumption — no source)_")
                # An assumption may still be labelled, and a labelled assumption
                # that is invisible in the file is a label nobody will ever act on.
                lines += [f"  {note}" for note in _claim_notes(claim)]
                continue
            url = sources.get(claim["fragment_id"], "")
            lines.append(f"- {claim['text']}")
            lines.append(f"  > {claim['quote']}")
            lines.append(f"  — <{url}> `{claim['fragment_id']}`")
            lines += [f"  {note}" for note in _claim_notes(claim)]
        lines.append("")
    freshness = sorted(
        (
            (claim["recheck_after"], claim)
            for claim in claims
            if claim.get("recheck_after") and claim.get("kind", "fact") == "fact"
        ),
        key=lambda pair: pair[0],
    )
    if freshness:
        lines += ["## Freshness", ""]
        for recheck_after, claim in freshness:
            bound_to = claim.get("bound_to") or "unspecified"
            lines.append(
                f"- **{recheck_after}** — {claim.get('binding')} ({bound_to}) — {claim['text']}"
            )
        lines.append("")
    if gaps:
        lines += ["## Gaps", ""] + [f"- {gap}" for gap in gaps] + [""]
    if sources:
        lines += ["## Sources", ""] + [f"- <{url}>" for url in sorted(set(sources.values()))]
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

    # An author-set date wins; otherwise the binding's horizon fills it in, so the
    # stored claim always carries the date the library will later act on. The date
    # is normalised here, not stored as typed: `freshness()` compares dates as
    # strings, and "20260730" or " 2026-08-01 " both parse yet neither compares
    # correctly against an ISO date.
    claims = [
        {
            **claim,
            "recheck_after": _normalise_date(claim.get("recheck_after"))
            or default_recheck_after(claim.get("binding"), today),
        }
        for claim in claims
    ]
    warnings = source_warnings(conn, claims)

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
                "INSERT INTO brief_claims (brief_id, idx, text, kind, fragment_id, quote,"
                " binding, bound_to, recheck_after, source_class)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    brief_id,
                    index,
                    claim["text"],
                    claim.get("kind", "fact"),
                    claim.get("fragment_id"),
                    claim.get("quote"),
                    claim.get("binding"),
                    claim.get("bound_to") or None,
                    claim.get("recheck_after"),
                    claim.get("source_class"),
                ),
            )
        c.execute("DELETE FROM briefs_fts WHERE brief_id = ?", (brief_id,))
        c.execute(
            "INSERT INTO briefs_fts (brief_id, topic, summary, claims) VALUES (?, ?, ?, ?)",
            (brief_id, topic, summary, claim_blob),
        )

    db.write(conn, op)
    log.info(
        "briefs.saved",
        brief_id=brief_id,
        claims=len(claims),
        gaps=len(gaps),
        warnings=len(warnings),
    )
    return {
        "brief_id": brief_id,
        "path": str(path),
        "claims": len(claims),
        "gaps": len(gaps),
        "warnings": warnings,
    }


def search(
    conn: sqlite3.Connection, query: str, limit: int = 3, today: str | None = None
) -> list[dict]:
    """Rank past briefs. Returns metadata and a snippet — never the brief body.

    `today` only exists so tests can pin the calendar; every caller leaves it None.
    """
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
                **freshness(conn, row["brief_id"], today=today),
            }
        )
    return hits


def freshness(conn: sqlite3.Connection, brief_id: str, today: str | None = None) -> dict:
    """How stale a brief's facts are, counted — the input to "reuse it or not?".

    Age in days says nothing on its own: a six-week-old brief whose facts are all
    `timeless` is as good as new, and one `world` fact can make a two-week-old
    brief lie. Claims stored before the columns existed have NULL binding and are
    counted as `unlabelled` rather than assumed to be anything.
    """
    today = today or date.today().isoformat()
    rows = conn.execute(
        "SELECT binding, recheck_after FROM brief_claims WHERE brief_id = ? AND kind = 'fact'",
        (brief_id,),
    ).fetchall()
    binding_mix: dict[str, int] = {}
    unlabelled = recheck_total = recheck_due = 0
    for row in rows:
        if row["binding"]:
            binding_mix[row["binding"]] = binding_mix.get(row["binding"], 0) + 1
        else:
            unlabelled += 1
        if row["recheck_after"]:
            recheck_total += 1
            if row["recheck_after"] <= today:
                recheck_due += 1
    return {
        "binding_mix": binding_mix,
        "unlabelled": unlabelled,
        "recheck_total": recheck_total,
        "recheck_due": recheck_due,
    }
