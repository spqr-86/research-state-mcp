"""recall@k of the fragment layer on the FRAMES benchmark.

# ANCHOR: eval/frames
# Role: measure the only thing this server actually does — pull the paragraphs
# that carry the answer out of a page, and drop the rest.
# In: google/frames-benchmark (test split) over the HuggingFace datasets-server
# REST API, plus the Wikipedia pages each example cites.
# Out: two recall figures, the baseline (is the answer on the page at all), how
# much text was withheld, how many fragments came back.
# Why recall_when_present is the headline: if the gold answer never appears in
# the cited pages, a miss says nothing about fragmentation — it is a property of
# the dataset. Raw recall without that denominator is unreadable.
# Not part of the package: this imports research_state as a library and is run
# by hand, never by the server.

Two recall variants, because a FRAMES example cites 2-8 pages:

* `recall_at_k_per_example` — **the headline**. Fragments from every cited page
  are pooled, ranked by score and cut to `k`, which is what a client actually
  receives for one question.
* `recall_at_k_per_page` — `k` fragments per page, so up to `k * avg_pages`
  fragments per example. Useful as an upper bound, not as a claim about the
  server. Caveat: bm25 scores come from a per-page index, so pooling them ranks
  across incomparable scales — the per-example figure is conservative for that
  reason, never optimistic.

Known limitation — page structure. By default the `extracts` body is re-split so
that every source line becomes its own paragraph. That is the *friendly* case:
`fragments` then never has to cut an oversized block, so `_cut_to_size` /
`looks_unstructured` (the code path that handles real, badly formatted pages) is
never exercised and the numbers are an upper bound. `--raw-text` feeds the body
through untouched and measures the unfriendly case instead. Report both.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

import structlog

from research_state import fragments

log = structlog.get_logger(__name__)

ROWS_URL = "https://datasets-server.huggingface.co/rows"
DATASET = "google/frames-benchmark"
WIKI_API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = (
    "research-state-mcp-eval/0.1 (https://github.com/spqr86/research-state-mcp)"
)

PAGE_SIZE = 100  # datasets-server hard limit
DEFAULT_LIMIT = 100
DEFAULT_CACHE_DIR = Path(__file__).resolve().parent / ".cache"
TIMEOUT = 30
SHORT_ANSWER_CHARS = 4  # "42", "1912", "two" — a match on these is barely evidence

_LINK_FIELD = re.compile(r"^wikipedia_link_(\d+)")
_URL = re.compile(r"https?://\S+")
_WIKI_ARTICLE = re.compile(
    r"^https?://[a-z0-9-]+\.wikipedia\.org/wiki/(?P<title>[^?#]+)"
)
_SPACE = re.compile(r"\s+")
_WORDISH = re.compile(r"\w")

JsonFetcher = Callable[[str], dict | None]


# --- page outcomes ----------------------------------------------------------


class PageStatus(StrEnum):
    """Why a page did or did not yield text — a timeout is not a missing page."""

    OK = "ok"
    MISSING = "missing"  # the API answered and there is no such article
    FAILED = "failed"  # the request itself did not succeed


@dataclass(frozen=True)
class PageResult:
    """Outcome of one page lookup."""

    status: PageStatus
    text: str | None = None

    @classmethod
    def ok(cls, text: str) -> PageResult:
        return cls(PageStatus.OK, text)

    @classmethod
    def missing(cls) -> PageResult:
        return cls(PageStatus.MISSING)

    @classmethod
    def failed(cls) -> PageResult:
        return cls(PageStatus.FAILED)


RowFetcher = Callable[[int, int], list[dict]]
PageFetcher = Callable[[str], PageResult]
Downloader = Callable[[str], PageResult]


# --- grading ----------------------------------------------------------------


def _normalise(text: str | None) -> str:
    """Casefold, collapse whitespace, drop a trailing period."""
    return _SPACE.sub(" ", (text or "").strip()).casefold().rstrip(".").strip()


def _needle_pattern(needle: str) -> re.Pattern[str]:
    """`needle` as a literal, fenced by word boundaries where they make sense.

    Plain substring matching turns "4" into a hit inside "14 ships" and "2
    years" into a hit inside "the 12 years war"; FRAMES answers are very often a
    bare number or year, so that inflates both the baseline and the headline.
    The boundary is only asserted on an end that is itself word-ish, so answers
    like "$5" or "(1912)" still match.
    """
    left = r"(?<!\w)" if _WORDISH.match(needle[0]) else ""
    right = r"(?!\w)" if _WORDISH.match(needle[-1]) else ""
    return re.compile(left + re.escape(needle) + right)


def answer_present(answer: str | None, text: str | None) -> bool:
    """True when the gold answer occurs in `text` as a whole word or phrase."""
    needle = _normalise(answer)
    if not needle:
        return False
    return _needle_pattern(needle).search(_normalise(text)) is not None


def is_short_answer(answer: str | None) -> bool:
    """True for answers too short for a match to mean much on its own."""
    needle = _normalise(answer)
    return bool(needle) and len(needle) < SHORT_ANSWER_CHARS


# --- dataset rows -----------------------------------------------------------


def wikipedia_links(row: dict) -> list[str]:
    """Every URL in the row's wikipedia_link_* fields, in field order, deduped."""
    fields: list[tuple[int, str]] = []
    for key, value in row.items():
        match = _LINK_FIELD.match(key)
        if match and isinstance(value, str):
            fields.append((int(match.group(1)), value))

    seen: set[str] = set()
    links: list[str] = []
    for _, value in sorted(fields, key=lambda item: item[0]):
        for url in _URL.findall(value):
            url = url.strip().rstrip(",;")
            if url and url not in seen:
                seen.add(url)
                links.append(url)
    return links


def title_from_url(url: str) -> str | None:
    """Article title of a Wikipedia URL, percent-decoded. None if not an article."""
    match = _WIKI_ARTICLE.match(url.strip())
    if not match:
        return None
    title = urllib.parse.unquote(match.group("title")).replace("_", " ").strip()
    return title or None


def load_rows(limit: int, fetch: RowFetcher) -> list[dict]:
    """Page through `fetch` until `limit` rows are in hand or the split ends."""
    rows: list[dict] = []
    while len(rows) < limit:
        batch = fetch(len(rows), min(PAGE_SIZE, limit - len(rows)))
        if not batch:
            break
        rows.extend(batch)
    return rows[:limit]


def fetch_rows(offset: int, length: int) -> list[dict]:
    """One page of the FRAMES test split from the HuggingFace datasets-server."""
    query = urllib.parse.urlencode(
        {
            "dataset": DATASET,
            "config": "default",
            "split": "test",
            "offset": offset,
            "length": length,
        }
    )
    payload = _get_json(f"{ROWS_URL}?{query}")
    if payload is None:
        return []
    return [item.get("row", {}) for item in payload.get("rows", [])]


# --- pages ------------------------------------------------------------------


def paragraphs_from_extract(raw: str | None) -> str:
    """Turn an `extracts` body into blank-line separated paragraphs.

    The action API separates paragraphs with a single newline; `fragments`
    splits on blank lines, so without this the whole article is one block. This
    is the friendly case — see the module docstring and `--raw-text`.
    """
    lines = [line.strip() for line in (raw or "").splitlines()]
    return "\n\n".join(line for line in lines if line)


def as_table_line(raw: str | None) -> str:
    """Reshape an article into the pathological page: one line of table cells.

    The real case this imitates is a HuggingFace dataset viewer page — a whole
    markdown table on a single line, no blank lines and no sentence-per-line
    structure for `fragments` to hold on to. Every word of the page survives;
    only its shape changes, so recall stays comparable to the other modes.
    """
    cells = [line.strip() for line in (raw or "").splitlines() if line.strip()]
    return "| " + " | ".join(cells) + " |" if cells else ""


def download_page(
    title: str,
    restructure: bool = True,
    get_json: JsonFetcher | None = None,
) -> PageResult:
    """Plain text of one article via the action API, with the failure reason kept."""
    query = urllib.parse.urlencode(
        {
            "action": "query",
            "prop": "extracts",
            "explaintext": "1",
            "redirects": "1",
            "format": "json",
            "formatversion": "2",
            "titles": title,
        }
    )
    payload = (get_json or _get_json)(f"{WIKI_API}?{query}")
    if payload is None:
        log.warning("frames.page_request_failed", title=title)
        return PageResult.failed()

    pages = payload.get("query", {}).get("pages", [])
    for page in pages:
        if page.get("missing"):
            continue
        raw = page.get("extract")
        text = (
            (raw or "").strip()
            if restructure is False
            else paragraphs_from_extract(raw)
        )
        if text:
            return PageResult.ok(text)

    log.warning("frames.page_missing", title=title)
    return PageResult.missing()


def page_text(url: str, cache_dir: Path, download: Downloader) -> PageResult:
    """Cached article text for `url`.

    An empty cache file means "the API said there is no such article". A failed
    request is never cached: a timeout or a 429 must not become a permanent
    verdict about the page.
    """
    title = title_from_url(url)
    if title is None:
        log.warning("frames.not_an_article", url=url)
        return PageResult.missing()

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{hashlib.sha256(title.encode()).hexdigest()}.txt"
    if path.exists():
        cached = path.read_text(encoding="utf-8")
        return PageResult.ok(cached) if cached else PageResult.missing()

    result = download(title)
    if result.status is PageStatus.FAILED:
        return result
    path.write_text(result.text or "", encoding="utf-8")
    return result if result.text else PageResult.missing()


def _get_json(url: str) -> dict | None:
    """GET JSON, turning every network or decoding failure into None + a log."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.warning("frames.request_failed", url=url, error=str(exc))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        log.warning("frames.bad_payload", url=url, error=str(exc))
    return None


# --- measurement ------------------------------------------------------------


@dataclass
class Report:
    """Everything the run produced.

    `recall_at_k_per_example_when_present` is the headline: `k` fragments for the
    whole question, counted only where the answer is on a cited page.
    """

    k: int
    neighbours: int
    raw_text: bool = False
    text_shape: str = "paragraphs"
    total: int = 0
    skipped: int = 0
    skipped_no_links: int = 0
    skipped_network: int = 0
    skipped_no_page: int = 0
    avg_pages: float = 0.0
    answer_on_page: int = 0
    hits_per_example: int = 0
    hits_per_page: int = 0
    recall_at_k_per_example: float = 0.0
    recall_at_k_per_page: float = 0.0
    recall_at_k_per_example_when_present: float = 0.0
    recall_at_k_per_page_when_present: float = 0.0
    avg_returned_ratio: float = 0.0
    avg_fragments: float = 0.0
    short_answers: int = 0
    short_answers_on_page: int = 0
    short_answer_hits: int = 0
    recall_short_answers: float = 0.0
    join_artefacts: int = 0
    _ratios: list[float] = field(default_factory=list, repr=False)
    _fragment_counts: list[int] = field(default_factory=list, repr=False)
    _page_counts: list[int] = field(default_factory=list, repr=False)

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if not k.startswith("_")}


def _gather_pages(row: dict, fetch_page: PageFetcher) -> tuple[list[str], PageStatus]:
    """Text of every cited page, plus the worst outcome seen while collecting."""
    pages: list[str] = []
    worst = PageStatus.MISSING
    for url in wikipedia_links(row):
        result = fetch_page(url)
        if result.status is PageStatus.OK and result.text:
            pages.append(result.text)
        elif result.status is PageStatus.FAILED:
            worst = PageStatus.FAILED
    return pages, worst


def _hit(
    answer: str,
    found: Sequence[dict],
    on_page: bool,
    report: Report,
    prompt: str,
    record: bool = True,
) -> bool:
    """A hit is the answer inside the returned fragments *and* on the page.

    Fragments are joined with "\\n\\n" and normalisation collapses whitespace, so
    two non-adjacent fragments can spell out an answer that is nowhere on the
    page. That is an artefact of the grader, not a retrieval success.
    """
    returned = "\n\n".join(f["text"] for f in found)
    if not answer_present(answer, returned):
        return False
    if not on_page:
        if record:
            report.join_artefacts += 1
            log.warning("frames.join_artefact", prompt=prompt[:80], answer=answer[:80])
        return False
    return True


def evaluate(
    rows: Iterable[dict],
    fetch_page: PageFetcher,
    k: int,
    neighbours: int,
    raw_text: bool = False,
    text_shape: str = "paragraphs",
) -> Report:
    """Run the fragment layer over every example and score it against the gold."""
    report = Report(k=k, neighbours=neighbours, raw_text=raw_text, text_shape=text_shape)

    for row in rows:
        report.total += 1
        prompt = row.get("Prompt") or ""
        answer = row.get("Answer") or ""

        if not wikipedia_links(row):
            _skip(report, "no_links", prompt)
            continue

        pages, worst = _gather_pages(row, fetch_page)
        if not pages:
            _skip(
                report,
                "network" if worst is PageStatus.FAILED else "no_page",
                prompt,
            )
            continue

        report._page_counts.append(len(pages))
        full = "\n\n".join(pages)
        on_page = answer_present(answer, full)
        short = is_short_answer(answer)
        if on_page:
            report.answer_on_page += 1
        if short:
            report.short_answers += 1
            if on_page:
                report.short_answers_on_page += 1

        per_page = [fragments.extract(page, prompt, k, neighbours) for page in pages]
        all_fragments = [f for page in per_page for f in page]
        pooled = sorted(all_fragments, key=lambda f: f["score"], reverse=True)[:k]

        report._fragment_counts.append(len(pooled))
        if full:
            returned = "\n\n".join(f["text"] for f in pooled)
            report._ratios.append(len(returned) / len(full))

        if _hit(answer, pooled, on_page, report, prompt):
            report.hits_per_example += 1
            if short:
                report.short_answer_hits += 1
        elif on_page:
            log.info("frames.miss", prompt=prompt[:80], answer=answer[:80])

        if _hit(answer, all_fragments, on_page, report, prompt, record=False):
            report.hits_per_page += 1

    return _finalise(report)


def _skip(report: Report, reason: str, prompt: str) -> None:
    report.skipped += 1
    setattr(report, f"skipped_{reason}", getattr(report, f"skipped_{reason}") + 1)
    log.info("frames.skipped", reason=reason, prompt=prompt[:80])


def _finalise(report: Report) -> Report:
    scored = report.total - report.skipped
    present = report.answer_on_page
    report.recall_at_k_per_example = report.hits_per_example / scored if scored else 0.0
    report.recall_at_k_per_page = report.hits_per_page / scored if scored else 0.0
    report.recall_at_k_per_example_when_present = (
        report.hits_per_example / present if present else 0.0
    )
    report.recall_at_k_per_page_when_present = (
        report.hits_per_page / present if present else 0.0
    )
    report.recall_short_answers = (
        report.short_answer_hits / report.short_answers_on_page
        if report.short_answers_on_page
        else 0.0
    )
    report.avg_returned_ratio = _mean(report._ratios)
    report.avg_fragments = _mean(report._fragment_counts)
    report.avg_pages = _mean(report._page_counts)
    return report


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


# --- CLI --------------------------------------------------------------------


def _positive(name: str) -> Callable[[str], int]:
    def parse(value: str) -> int:
        number = int(value)
        if number < 1:
            raise argparse.ArgumentTypeError(f"--{name} must be >= 1, got {number}")
        return number

    return parse


def _non_negative(name: str) -> Callable[[str], int]:
    def parse(value: str) -> int:
        number = int(value)
        if number < 0:
            raise argparse.ArgumentTypeError(f"--{name} must be >= 0, got {number}")
        return number

    return parse


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=_positive("limit"), default=DEFAULT_LIMIT)
    parser.add_argument("--k", type=_positive("k"), default=fragments.DEFAULT_K)
    parser.add_argument(
        "--neighbours",
        type=_non_negative("neighbours"),
        default=fragments.DEFAULT_NEIGHBOURS,
    )
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument(
        "--raw-text",
        action="store_true",
        help="feed the article body in as it comes, without re-splitting it into "
        "paragraphs — measures the badly structured page instead of the tidy one",
    )
    parser.add_argument(
        "--table-text",
        action="store_true",
        help="reshape each article into one line of pipe-separated cells — the "
        "table page that has no structure at all to split on",
    )
    parser.add_argument("--json", type=Path, default=None, help="write the report here")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    rows = load_rows(args.limit, fetch_rows)
    if not rows:
        log.error("frames.no_rows")
        return 1

    plain = args.raw_text or args.table_text
    cache_dir = args.cache_dir / ("raw" if plain else "paragraphs")

    def fetch(url: str) -> PageResult:
        result = page_text(
            url,
            cache_dir,
            lambda title: download_page(title, restructure=not plain),
        )
        if args.table_text and result.status is PageStatus.OK:
            return PageResult.ok(as_table_line(result.text))
        return result

    report = evaluate(
        rows,
        fetch,
        k=args.k,
        neighbours=args.neighbours,
        raw_text=args.raw_text,
        text_shape="table" if args.table_text else ("raw" if args.raw_text else "paragraphs"),
    )
    _print_summary(report)
    if args.json:
        args.json.write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
    return 0


def _print_summary(report: Report) -> None:
    lines = [
        f"examples            {report.total}",
        f"text mode           {report.text_shape}",
        f"skipped             {report.skipped}"
        f" (no links {report.skipped_no_links},"
        f" network {report.skipped_network},"
        f" no page {report.skipped_no_page})",
        f"pages per example   {report.avg_pages:.2f}",
        f"answer on page      {report.answer_on_page} (baseline)",
        f"recall@{report.k} example  {report.recall_at_k_per_example:.1%} of scored",
        f"  | present         {report.recall_at_k_per_example_when_present:.1%}"
        f"  <- headline",
        f"recall@{report.k} per page {report.recall_at_k_per_page:.1%} of scored"
        f" (up to k x {report.avg_pages:.1f} fragments)",
        f"  | present         {report.recall_at_k_per_page_when_present:.1%}",
        f"short answers       {report.short_answers}"
        f" ({report.short_answers_on_page} on page,"
        f" recall {report.recall_short_answers:.1%})",
        f"join artefacts      {report.join_artefacts}",
        f"text returned       {report.avg_returned_ratio:.1%} of page on average",
        f"fragments           {report.avg_fragments:.2f} on average",
    ]
    sys.stdout.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
