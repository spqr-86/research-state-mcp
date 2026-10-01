"""Regression scorer for the deep-research pipeline over ~20 real queries.

# ANCHOR: eval/regression
# Role: tell whether a change to the skill or to research-fetch made briefs
# better or worse on the questions people actually asked, without a paid judge.
# In: a private queries file (JSON, outside this repo — real queries are personal),
#     the server's state db, and the brief files it points to.
# Out: one row per query — mechanical numbers only.
#
# Why mechanical: LLM judges score evidence badly (REFLECT, 2026: best <55%),
# so everything here is counted, not judged:
#   * claims by kind and by source_class — vendor and retelling shares;
#   * distinct source domains and the share of the biggest one — one-site briefs;
#   * the research_mark gate — answers that carry "за N / против M" and an
#     against-url, as the skill requires since 2026-10-01;
#   * gaps in the brief;
#   * anchors — regexes for the key findings of the baseline brief. A hit means
#     the new run found the same thing, not that it is right.
# Quality of reasoning stays with a human reading the diff of two briefs.
#
# Not part of the package: imports nothing from research_state, run by hand:
#   uv run python eval/regression.py --queries PATH --db PATH [--since YYYY-MM-DD]
#   uv run python eval/regression.py --queries PATH --baseline-dir DIR
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import structlog

log = structlog.get_logger(__name__)

# The tally as agents actually write it: a label may sit between "for" and its count
# ("for (positive) 3", "for risks 4"), and sources may follow the count in parentheses.
_TALLY = re.compile(
    r"\bза\b[^/\n]{0,120}?(\d+)(?:\s*\([^)\n]{0,300}\))?\s*/\s*против\s*(\d+)",
    re.IGNORECASE,
)
_AGAINST_URL = re.compile(r"против[^\n]*?https?://", re.IGNORECASE)
_WS = re.compile(r"\s+")
_SECTION = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def load_queries(path: Path) -> list[dict]:
    """Read and validate the queries file: unique ids, compilable anchors."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    queries = data["queries"]
    seen: set[str] = set()
    for item in queries:
        if item["id"] in seen:
            raise ValueError(f"duplicate query id: {item['id']}")
        seen.add(item["id"])
        for anchor in item.get("anchors", []):
            for pattern in anchor["any"]:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"bad regex in {item['id']}: {pattern!r} ({exc})") from exc
    return queries


def anchor_hits(text: str, anchors: list[dict]) -> list[bool]:
    return [
        any(re.search(pattern, text, re.IGNORECASE) for pattern in anchor["any"])
        for anchor in anchors
    ]


def gate_metrics(answers: list[str]) -> dict:
    """How many research_mark answers carry the for/against tally the skill asks for."""
    tallied = against_url = against_zero = 0
    for answer in answers:
        match = _TALLY.search(answer or "")
        if not match:
            continue
        tallied += 1
        if int(match.group(2)) == 0:
            against_zero += 1
        if _AGAINST_URL.search(answer):
            against_url += 1
    return {
        "subq": len(answers),
        "tallied": tallied,
        "against_url": against_url,
        "against_zero": against_zero,
    }


def gap_count(text: str) -> int:
    sections = list(_SECTION.finditer(text))
    for i, section in enumerate(sections):
        if section.group(1).casefold() not in ("gaps", "пробелы"):
            continue
        end = sections[i + 1].start() if i + 1 < len(sections) else len(text)
        body = text[section.end() : end]
        return sum(1 for line in body.splitlines() if line.startswith("- "))
    return 0


def _norm_topic(topic: str) -> str:
    return _WS.sub(" ", topic).strip().rstrip("?.!").casefold()


def same_topic(a: str, b: str) -> bool:
    return _norm_topic(a) == _norm_topic(b)


def _domain(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host.removeprefix("www.")


def latest_run(conn: sqlite3.Connection, query: str, since: int) -> dict | None:
    """The newest brief on this exact topic created at or after `since` (epoch)."""
    rows = conn.execute(
        "SELECT brief_id, job_id, topic, path, created FROM briefs "
        "WHERE created >= ? ORDER BY created DESC",
        (since,),
    ).fetchall()
    for brief_id, job_id, topic, path, created in rows:
        if same_topic(topic, query):
            return {"brief_id": brief_id, "job_id": job_id, "path": path, "created": created}
    return None


def score_run(conn: sqlite3.Connection, run: dict, anchors: list[dict]) -> dict:
    claims = conn.execute(
        "SELECT c.kind, c.source_class, f.url FROM brief_claims c "
        "LEFT JOIN issued_fragments f ON f.fragment_id = c.fragment_id "
        "WHERE c.brief_id = ?",
        (run["brief_id"],),
    ).fetchall()
    facts = [c for c in claims if c[0] == "fact"]
    domains = Counter(_domain(url) for _, _, url in facts if url)
    answers = [
        row[0] or ""
        for row in conn.execute(
            "SELECT answer FROM subquestions WHERE job_id = ? ORDER BY subq_id",
            (run["job_id"],),
        )
    ]
    text = Path(run["path"]).read_text(encoding="utf-8")
    hits = anchor_hits(text, anchors)
    with_url = sum(domains.values())
    return {
        "facts": len(facts),
        "assumptions": sum(1 for c in claims if c[0] == "assumption"),
        "vendor": sum(1 for c in facts if c[1] == "vendor"),
        "secondary": sum(1 for c in facts if c[1] == "secondary"),
        "domains": len(domains),
        "top_domain_share": (domains.most_common(1)[0][1] / with_url) if with_url else 0.0,
        "gate": gate_metrics(answers),
        "gaps": gap_count(text),
        "anchors_hit": sum(hits),
        "anchors_total": len(hits),
        "missed": [a["desc"] for a, hit in zip(anchors, hits, strict=True) if not hit],
    }


def _print_runs(rows: list[tuple[dict, dict | None]]) -> None:
    print(
        "| id | mode | facts | vendor | retell | domains | top% | gate tallied/subq "
        "| against url | gaps | anchors |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for query, score in rows:
        if score is None:
            print(f"| {query['id']} | {query['mode']} | not run |  |  |  |  |  |  |  |  |")
            continue
        gate = score["gate"]
        print(
            f"| {query['id']} | {query['mode']} | {score['facts']} | {score['vendor']} "
            f"| {score['secondary']} | {score['domains']} | {score['top_domain_share']:.0%} "
            f"| {gate['tallied']}/{gate['subq']} | {gate['against_url']} | {score['gaps']} "
            f"| {score['anchors_hit']}/{score['anchors_total']} |"
        )
    for query, score in rows:
        if score and score["missed"]:
            print(f"- {query['id']}: missed — " + "; ".join(score["missed"]))


def check_baselines(queries: list[dict], baseline_dir: Path) -> int:
    """Anchors must hit their own baseline brief, or they measure nothing."""
    bad = 0
    for query in queries:
        text = (baseline_dir / query["baseline"]).read_text(encoding="utf-8")
        hits = anchor_hits(text, query["anchors"])
        missed = [a["desc"] for a, hit in zip(query["anchors"], hits, strict=True) if not hit]
        bad += bool(missed)
        print(f"{query['id']}: {sum(hits)}/{len(hits)}" + (f" missed {missed}" if missed else ""))
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--db", type=Path, help="research-state sqlite db")
    parser.add_argument("--since", help="only runs on or after this date, YYYY-MM-DD")
    parser.add_argument(
        "--baseline-dir",
        type=Path,
        help="check anchors against each query's baseline brief instead of scoring runs",
    )
    parser.add_argument("--json", action="store_true", help="print raw scores as JSON")
    args = parser.parse_args(argv)

    queries = load_queries(args.queries)
    if args.baseline_dir:
        return check_baselines(queries, args.baseline_dir)
    if not args.db:
        parser.error("--db is required unless --baseline-dir is given")

    since = int(datetime.fromisoformat(args.since).timestamp()) if args.since else 0
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows = []
    for query in queries:
        run = latest_run(conn, query["query"], since)
        rows.append((query, score_run(conn, run, query["anchors"]) if run else None))
    if args.json:
        print(json.dumps({q["id"]: s for q, s in rows}, ensure_ascii=False, indent=2))
        return 0
    _print_runs(rows)
    log.info("regression.scored", runs=sum(1 for _, s in rows if s), queries=len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
