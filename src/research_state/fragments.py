"""Paragraph-level retrieval — the reason this project exists.

# ANCHOR: fragments
# Role: turn one page of text + a query into 2-5 relevant paragraphs.
# In: page text (or a URL present in the free-search-mcp cache) and a query.
# Out: [{text, paragraph_index, score, start, end}] — never the whole page.
# How: a throwaway in-memory FTS5 table over the page's paragraphs, ranked by
# bm25. unicode61 handles Russian without any preprocessing, which rank-bm25
# does not. Hits are widened by `neighbours` paragraphs on each side and
# overlapping windows are merged, so a match never arrives without context.
# Note: the foreign cache stores title as "\x01META\x01{json}" — stripped here.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")
_WORD = re.compile(r"\w+", re.UNICODE)
_META_PREFIX = "\x01META\x01"

DEFAULT_K = 5
DEFAULT_NEIGHBOURS = 1


def split_paragraphs(text: str) -> list[str]:
    """Split on blank lines, dropping empties and trailing whitespace."""
    return [p.strip() for p in _PARAGRAPH_SPLIT.split(text or "") if p.strip()]


def _fts_query(query: str) -> str:
    """Turn a raw human query into a safe FTS5 MATCH expression.

    Every token is quoted, so operators the user happened to type (NEAR, AND,
    quotes, parentheses, `*`, `-`) are matched as words instead of blowing up
    the parser.
    """
    tokens = _WORD.findall(query or "")
    return " OR ".join(f'"{t}"' for t in tokens)


def extract(
    text: str,
    query: str,
    k: int = DEFAULT_K,
    neighbours: int = DEFAULT_NEIGHBOURS,
) -> list[dict]:
    """Return at most `k` fragments of `text` most relevant to `query`."""
    paragraphs = split_paragraphs(text)
    match = _fts_query(query)
    if not paragraphs or not match:
        return []

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE p USING fts5(body, tokenize='unicode61')")
        conn.executemany(
            "INSERT INTO p (rowid, body) VALUES (?, ?)", enumerate(paragraphs)
        )
        rows = conn.execute(
            "SELECT rowid, bm25(p) AS rank FROM p WHERE p MATCH ? ORDER BY rank LIMIT ?",
            (match, k),
        ).fetchall()
    except sqlite3.OperationalError as exc:  # malformed MATCH should never reach here
        log.warning("fragments.match_failed", error=str(exc))
        return []
    finally:
        conn.close()

    hits = [(int(rowid), -float(rank)) for rowid, rank in rows]
    return _windows(paragraphs, hits, neighbours)


def _windows(
    paragraphs: list[str], hits: list[tuple[int, float]], neighbours: int
) -> list[dict]:
    """Widen each hit by `neighbours` paragraphs and merge overlapping windows."""
    windows: list[dict] = []
    for index, score in hits:  # hits arrive best-first
        start = max(0, index - neighbours)
        end = min(len(paragraphs) - 1, index + neighbours)
        for w in windows:
            if start <= w["end"] + 1 and end >= w["start"] - 1:
                w["start"] = min(w["start"], start)
                w["end"] = max(w["end"], end)
                break
        else:
            windows.append(
                {"start": start, "end": end, "paragraph_index": index, "score": score}
            )

    for w in windows:
        w["text"] = "\n\n".join(paragraphs[w["start"] : w["end"] + 1])
    return windows


def cached_page(cache_path: str | Path, url: str) -> dict | None:
    """Read one page from the free-search-mcp cache. Read-only, always."""
    cache_path = Path(cache_path)
    if not cache_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{cache_path}?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:
        log.warning("fragments.cache_unavailable", path=str(cache_path), error=str(exc))
        return None
    try:
        row = conn.execute(
            "SELECT title, content, fetched FROM pages WHERE url = ?", (url,)
        ).fetchone()
    except sqlite3.OperationalError as exc:
        log.warning("fragments.cache_unreadable", error=str(exc))
        return None
    finally:
        conn.close()

    if row is None:
        return None
    return {
        "url": url,
        "title": _clean_title(row[0]),
        "content": row[1],
        "fetched_at": row[2],
        "cache_path": str(cache_path),
    }


def _clean_title(raw: str | None) -> str | None:
    """free-search-mcp wraps titles as "\x01META\x01{json}\x01" — note the terminator."""
    if not raw or not raw.startswith(_META_PREFIX):
        return raw
    payload = raw[len(_META_PREFIX) :].rstrip("\x01")
    try:
        return json.loads(payload).get("title")
    except (json.JSONDecodeError, AttributeError):
        return None
