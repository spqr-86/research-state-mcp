"""Where a minimum-quote-length floor would have to sit, measured not guessed.

# ANCHOR: eval/quote-floor
# Role: price the only hole citation_rejection.py found — a quote that is
# verbatim yet carries nothing ("The Office is the"). A length floor is the
# obvious answer; this says what it would cost and what it would buy.
# In: the same FRAMES page cache as the other two harnesses.
# Out: per candidate floor, two rates that move in opposite directions.
#
# The two numbers, and why these two:
#   * `ambiguous_rate` — share of opening quotes of that length that also occur
#     verbatim in another fragment of the corpus. A quote that several unrelated
#     fragments contain cannot be evidence for a claim about any one of them.
#     This is the closest measurable stand-in for "supports nothing" that a
#     server without a model can have.
#   * `blocked_honest_rate` — share of real sentences on real pages shorter than
#     the floor. Those are honest citations the floor would forbid outright.
# There is no threshold that makes both zero. The floor is a choice about which
# error to prefer, and this puts numbers under the choice.
#
# Not part of the package: imports research_state as a library, run by hand.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from research_state import fragments as fragments_module

log = structlog.get_logger(__name__)

DEFAULT_CACHE_DIR = Path(__file__).resolve().parent / ".cache" / "paragraphs"
DEFAULT_LIMIT = 500
MIN_FRAGMENT_CHARS = 120
DEFAULT_THRESHOLDS = (2, 3, 4, 5, 6, 8, 10, 12, 15, 20)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_WS = re.compile(r"\s+")
_WORDISH = re.compile(r"\w")


def normalise(text: str) -> str:
    """The same normalisation the citation check uses: whitespace and case."""
    return _WS.sub(" ", text or "").strip().casefold()


def word_count(text: str) -> int:
    return len((text or "").split())


def first_words(text: str, n: int) -> str | None:
    """The opening `n` words, or None when the text does not have that many."""
    words = (text or "").split()
    if len(words) < n:
        return None
    return " ".join(words[:n])


def sentences(text: str) -> list[str]:
    """Real sentences of a page — the honest short citations a floor would hit."""
    return [
        piece.strip()
        for piece in _SENTENCE_SPLIT.split((text or "").strip())
        if _WORDISH.search(piece)
    ]


# --- ambiguity ---------------------------------------------------------------


def build_index(texts: Sequence[str]) -> list[str]:
    """The corpus, normalised once, so containment can be counted cheaply."""
    return [normalise(text) for text in texts]


def is_ambiguous(quote: str, index: Sequence[str]) -> bool:
    """True when this exact wording occurs in more than one fragment."""
    needle = normalise(quote)
    if not needle:
        return True
    hits = 0
    for haystack in index:
        if needle in haystack:
            hits += 1
            if hits > 1:
                return True
    return False


# --- report ------------------------------------------------------------------


@dataclass
class FloorRow:
    """What one candidate floor would cost and buy."""

    words: int
    ambiguous: int = 0
    measured: int = 0
    blocked: int = 0
    honest: int = 0

    @property
    def ambiguous_rate(self) -> float:
        return self.ambiguous / self.measured if self.measured else 0.0

    @property
    def blocked_honest_rate(self) -> float:
        return self.blocked / self.honest if self.honest else 0.0


@dataclass
class FloorReport:
    """One row per candidate floor, plus the corpus it was measured on."""

    rows: list[FloorRow] = field(default_factory=list)
    fragments: int = 0
    honest_sentences: int = 0

    def as_dict(self) -> dict:
        return {
            "fragments": self.fragments,
            "honest_sentences": self.honest_sentences,
            "floors": [
                {
                    "words": row.words,
                    "ambiguous_rate": round(row.ambiguous_rate, 4),
                    "blocked_honest_rate": round(row.blocked_honest_rate, 4),
                    "measured": row.measured,
                    "honest": row.honest,
                }
                for row in self.rows
            ],
        }


# --- the scan ----------------------------------------------------------------


def scan(texts: Sequence[str], thresholds: Sequence[int] = DEFAULT_THRESHOLDS) -> FloorReport:
    """Price every candidate floor against the same corpus."""
    if any(threshold < 1 for threshold in thresholds):
        raise ValueError("a floor of zero words is not a floor")

    index = build_index(texts)
    honest = [sentence for text in texts for sentence in sentences(text)]
    report = FloorReport(fragments=len(texts), honest_sentences=len(honest))

    for threshold in thresholds:
        row = FloorRow(words=threshold)
        for text in texts:
            quote = first_words(text, threshold)
            if quote is None:
                continue
            row.measured += 1
            row.ambiguous += int(is_ambiguous(quote, index))
        row.honest = len(honest)
        row.blocked = sum(1 for sentence in honest if word_count(sentence) < threshold)
        report.rows.append(row)
    return report


# --- corpus ------------------------------------------------------------------


def load_fragments(cache_dir: Path, limit: int, min_chars: int = MIN_FRAGMENT_CHARS) -> list[str]:
    """Real paragraphs from the FRAMES page cache."""
    files = sorted(Path(cache_dir).glob("*.txt"))
    if not files:
        raise SystemExit(f"no cached pages in {cache_dir} — run eval/frames_recall.py first")
    return list(_paragraphs(files, limit, min_chars))


def _paragraphs(files: Iterable[Path], limit: int, min_chars: int) -> Iterable[str]:
    seen = 0
    for path in files:
        for paragraph in fragments_module.split_paragraphs(path.read_text(encoding="utf-8")):
            if len(paragraph) < min_chars:
                continue
            yield paragraph
            seen += 1
            if seen >= limit:
                return


# --- CLI ---------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--min-chars", type=int, default=MIN_FRAGMENT_CHARS)
    parser.add_argument("--json", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    texts = load_fragments(args.cache_dir, args.limit, args.min_chars)
    report = scan(texts)
    _print_summary(report)
    if args.json:
        args.json.write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
    return 0


def _print_summary(report: FloorReport) -> None:
    print(f"fragments: {report.fragments}   honest sentences: {report.honest_sentences}")
    print(f"{'floor (words)':>14}{'ambiguous':>12}{'blocks honest':>16}")
    for row in report.rows:
        print(f"{row.words:>14}{row.ambiguous_rate:>11.1%}{row.blocked_honest_rate:>16.1%}")


if __name__ == "__main__":
    sys.exit(main())
