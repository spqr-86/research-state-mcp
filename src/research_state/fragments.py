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
ANCHOR_CONTEXT = 32

# A page whose author never pressed Enter twice — a dataset viewer, a changelog,
# a wiki table — is one blank-line-delimited "paragraph" the size of the page.
# Returning it would hand back everything this project exists to withhold, so
# such a block is cut further: first on line breaks, then, failing that, on
# characters.
MAX_PARAGRAPH_CHARS = 2000


def split_paragraphs(text: str) -> list[str]:
    """Split on blank lines, then cut any block too large to be one fragment."""
    return [span[0] for span in split_paragraphs_with_spans(text)]


def split_paragraphs_with_spans(text: str) -> list[tuple[str, int, int]]:
    """Paragraphs plus their exact character span in the original text.

    Spans are what makes a fragment re-anchorable later, so `extract` uses this
    and slices the page verbatim instead of rejoining stripped paragraphs.
    """
    spans: list[tuple[str, int, int]] = []
    for match in re.finditer(r"[^\n](?:.*?)(?=\n\s*\n|\Z)", text or "", re.DOTALL):
        chunk = match.group()
        stripped = chunk.strip()
        if not stripped:
            continue
        start = match.start() + chunk.index(stripped)
        spans.extend(_cut_to_size(stripped, start))
    return spans


def _cut_to_size(block: str, offset: int) -> list[tuple[str, int, int]]:
    """Break an oversized block into span-accurate pieces, on lines if possible."""
    if len(block) <= MAX_PARAGRAPH_CHARS:
        return [(block, offset, offset + len(block))]

    pieces: list[tuple[str, int, int]] = []
    cursor = 0
    for line in block.splitlines(keepends=True):
        start = cursor
        cursor += len(line)
        stripped = line.strip()
        if not stripped:
            continue
        line_start = start + line.index(stripped)
        pieces.extend(_cut_by_chars(stripped, offset + line_start))
    return pieces


def _cut_by_chars(piece: str, offset: int) -> list[tuple[str, int, int]]:
    """Last resort for a single line longer than the cap: fixed-width slices."""
    if len(piece) <= MAX_PARAGRAPH_CHARS:
        return [(piece, offset, offset + len(piece))]
    return [
        (
            piece[i : i + MAX_PARAGRAPH_CHARS],
            offset + i,
            offset + i + len(piece[i : i + MAX_PARAGRAPH_CHARS]),
        )
        for i in range(0, len(piece), MAX_PARAGRAPH_CHARS)
    ]


def looks_unstructured(text: str) -> bool:
    """True when the page had no usable paragraph breaks and had to be cut."""
    blocks = [p.strip() for p in _PARAGRAPH_SPLIT.split(text or "") if p.strip()]
    return any(len(b) > MAX_PARAGRAPH_CHARS for b in blocks)


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
    spans = split_paragraphs_with_spans(text)
    paragraphs = [span[0] for span in spans]
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
    return _windows(spans, text, hits, neighbours)


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
            windows.append(
                {"start": start, "end": end, "paragraph_index": index, "score": score}
            )

    for w in windows:
        char_start = spans[w["start"]][1]
        char_end = spans[w["end"]][2]
        w["text"] = text[char_start:char_end]
        w["char_start"] = char_start
        w["char_end"] = char_end
        w["prefix"] = text[max(0, char_start - ANCHOR_CONTEXT) : char_start]
        w["suffix"] = text[char_end : char_end + ANCHOR_CONTEXT]
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
