"""How much fabrication the citation check actually stops.

# ANCHOR: eval/citations
# Role: measure the invariant this server exists for — a factual claim without a
# verbatim quote from an issued fragment cannot enter a brief.
# In: real page text (the FRAMES page cache written by frames_recall.py).
# Out: catch rate per fabrication type, false-reject rate per typography type.
# Why not "how many made-up citations does it catch": the check is exact
# containment after whitespace/case normalisation, so in the limit the answer is
# 100% by construction. The two numbers that are not settled by construction:
#   * fabrication that survives anyway — a quote that is verbatim yet supports
#     nothing (`trivial`), or one stitched out of real wording;
#   * honest quotes rejected on typography — a model retyping “ as ", NBSP as a
#     space, en dash as hyphen, or eliding the middle with an ellipsis. That is the friction
#     price of the invariant, and it was never measured.
# Not part of the package: imports research_state as a library, run by hand.

Every check goes through `briefs.validate_claims` against a real database with
really issued fragments. Nothing here reimplements the rule being measured.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import structlog

from research_state import briefs, db, issued

log = structlog.get_logger(__name__)

DEFAULT_CACHE_DIR = Path(__file__).resolve().parent / ".cache" / "paragraphs"
DEFAULT_LIMIT = 100
MIN_FRAGMENT_CHARS = 120
TRIVIAL_WORDS = 4
DROP_MIN_WORDS = 4
STITCH_MIN_WORDS = 4
ELLIPSIS_MIN_WORDS = 6
EVAL_URL = "https://eval.invalid/page"
EVAL_FETCHED_AT = 1_700_000_000

_SENTENCE_END = re.compile(r"(?<=[.!?])\s")
_DIGITS = re.compile(r"\d+")
_WORDISH = re.compile(r"\w")
_MID_CAPITAL = re.compile(r"(?<=\w\s)\b[A-Z][a-z]{2,}\b")
_CURLY = {"“": '"', "”": '"', "‘": "'", "’": "'"}  # noqa: RUF001
_EXOTIC_SPACE = re.compile("[    ]")  # noqa: RUF001
_DASH = re.compile("[–—]")  # noqa: RUF001

INVENTED = (
    "The programme was cancelled after a single unaired pilot season, "
    "according to the network's internal review."
)


# --- picking a quote out of a fragment --------------------------------------


def pick_quote(text: str) -> str | None:
    """The first sentence — what a model actually cites, and verbatim by design.

    Cached pages start mid-quotation often enough ("... two gentlemen come in")
    that a naive sentence split hands back a bare "..." — not a quote, and it
    poisons the control row. A candidate with no word characters is skipped.
    """
    for candidate in _SENTENCE_END.split((text or "").strip()):
        if _WORDISH.search(candidate):
            return candidate.strip()
    return None


# --- fabricated: the quote no longer matches the source ---------------------


def invent(text: str) -> str | None:
    """A fluent sentence that was never on the page."""
    return INVENTED


def swap_number(text: str) -> str | None:
    """Same sentence, one number tampered with — the classic silent falsehood."""
    match = _DIGITS.search(text)
    if match is None:
        return None
    original = match.group()
    replacement = str(int(original) + 1).zfill(len(original))
    return text[: match.start()] + replacement + text[match.end() :]


def swap_entity(text: str) -> str | None:
    """Same sentence, a different name in it."""
    match = _MID_CAPITAL.search(text)
    if match is None:
        return None
    return text[: match.start()] + "Thompson" + text[match.end() :]


def drop_word(text: str) -> str | None:
    """A near-verbatim quote with one word lost — sloppy copying, not invention."""
    words = text.split()
    if len(words) < DROP_MIN_WORDS:
        return None
    middle = len(words) // 2
    return " ".join(words[:middle] + words[middle + 1 :])


def stitch(text: str, other: str) -> str | None:
    """Head of one real quote welded to the tail of another. Every word is real."""
    left, right = text.split(), other.split()
    if len(left) < STITCH_MIN_WORDS or len(right) < STITCH_MIN_WORDS:
        return None
    return " ".join(left[: len(left) // 2] + right[len(right) // 2 :])


# --- faithful: the model meant the real sentence and retyped it -------------


def straighten_quotes(text: str) -> str | None:
    """Typographic quotation marks typed back as ASCII."""
    if not any(char in text for char in _CURLY):
        return None
    for curly, plain in _CURLY.items():
        text = text.replace(curly, plain)
    return text


def plain_space(text: str) -> str | None:
    """A non-breaking or thin space typed back as an ordinary one.

    Note: whitespace normalisation already collapses these, so this one is
    expected to pass — it is here to prove that, not to fail.
    """
    if not _EXOTIC_SPACE.search(text):
        return None
    return _EXOTIC_SPACE.sub(" ", text)


def plain_dash(text: str) -> str | None:
    """An en/em dash typed back as a hyphen."""
    if not _DASH.search(text):
        return None
    return _DASH.sub("-", text)


def ellipsis_gap(text: str) -> str | None:
    """The middle elided — a citation convention, not a fabrication."""
    words = text.split()
    if len(words) < ELLIPSIS_MIN_WORDS:
        return None
    return " ".join(words[:2]) + " … " + " ".join(words[-2:])


# --- genuine but worthless ---------------------------------------------------


def trivial(text: str) -> str | None:
    """The shortest verbatim quote a model can get away with."""
    quote = pick_quote(text)
    if quote is None:
        return None
    words = quote.split()[:TRIVIAL_WORDS]
    return " ".join(words) or None


def verbatim(text: str) -> str | None:
    """The control. Rejecting this would mean the check is broken."""
    return pick_quote(text)


# --- the mutation table ------------------------------------------------------


class Family(StrEnum):
    """What a rejection means for this mutation."""

    FABRICATED = "fabricated"  # rejection is the server working
    FAITHFUL = "faithful"  # rejection is friction the user pays
    TRIVIAL = "trivial"  # verbatim by construction — passing is the finding
    CONTROL = "control"  # rejection would be a bug


@dataclass(frozen=True)
class Mutation:
    """One way a quote can differ from the fragment it claims to come from."""

    name: str
    family: Family
    apply: Callable[[str], str | None]
    needs_pair: bool = False


def _on_quote(fn: Callable[[str], str | None]) -> Callable[[str], str | None]:
    """Run a mutation on the picked quote rather than the whole fragment."""

    def wrapped(text: str) -> str | None:
        quote = pick_quote(text)
        return None if quote is None else fn(quote)

    return wrapped


MUTATIONS: tuple[Mutation, ...] = (
    Mutation("verbatim", Family.CONTROL, verbatim),
    Mutation("invent", Family.FABRICATED, invent),
    Mutation("swap_number", Family.FABRICATED, _on_quote(swap_number)),
    Mutation("swap_entity", Family.FABRICATED, _on_quote(swap_entity)),
    Mutation("drop_word", Family.FABRICATED, _on_quote(drop_word)),
    Mutation("stitch", Family.FABRICATED, verbatim, needs_pair=True),
    Mutation("trivial", Family.TRIVIAL, trivial),
    Mutation("straighten_quotes", Family.FAITHFUL, _on_quote(straighten_quotes)),
    Mutation("plain_space", Family.FAITHFUL, _on_quote(plain_space)),
    Mutation("plain_dash", Family.FAITHFUL, _on_quote(plain_dash)),
    Mutation("ellipsis_gap", Family.FAITHFUL, _on_quote(ellipsis_gap)),
)


# --- the check being measured ------------------------------------------------


def rejected(conn: sqlite3.Connection, fragment_id: str, quote: str | None) -> bool:
    """True when the real validator refuses this claim."""
    claim = {
        "text": "a factual claim about the page",
        "kind": "fact",
        "fragment_id": fragment_id,
        "quote": quote,
    }
    return bool(briefs.validate_claims(conn, [claim]))


# --- report ------------------------------------------------------------------


@dataclass
class Row:
    """One mutation's tally."""

    name: str
    family: Family
    applicable: int = 0
    rejected: int = 0
    not_applicable: int = 0

    @property
    def rate(self) -> float:
        return self.rejected / self.applicable if self.applicable else 0.0


@dataclass
class Report:
    """Counts per mutation plus the two headline rates."""

    _rows: dict[str, Row] = field(default_factory=dict)
    fragments: int = 0

    def add(self, name: str, family: Family, *, rejected: bool) -> None:
        row = self._rows.setdefault(name, Row(name, family))
        row.applicable += 1
        row.rejected += int(rejected)

    def skip(self, name: str, family: Family = Family.FABRICATED) -> None:
        """The mutation had nothing to work with on this fragment."""
        self._rows.setdefault(name, Row(name, family)).not_applicable += 1

    def rows(self) -> list[Row]:
        return list(self._rows.values())

    def _family_rate(self, family: Family) -> float:
        rows = [row for row in self._rows.values() if row.family is family]
        applicable = sum(row.applicable for row in rows)
        if not applicable:
            return 0.0
        return sum(row.rejected for row in rows) / applicable

    def catch_rate(self) -> float:
        """Share of fabricated quotes the check refuses. Higher is better."""
        return self._family_rate(Family.FABRICATED)

    def false_reject_rate(self) -> float:
        """Share of faithful quotes the check refuses. Lower is better."""
        return self._family_rate(Family.FAITHFUL)

    def as_dict(self) -> dict:
        return {
            "fragments": self.fragments,
            "catch_rate": round(self.catch_rate(), 4),
            "false_reject_rate": round(self.false_reject_rate(), 4),
            "mutations": [
                {
                    "name": row.name,
                    "family": str(row.family),
                    "applicable": row.applicable,
                    "rejected": row.rejected,
                    "not_applicable": row.not_applicable,
                    "rate": round(row.rate, 4),
                }
                for row in self.rows()
            ],
        }


# --- runner ------------------------------------------------------------------


def evaluate(conn: sqlite3.Connection, texts: Sequence[str]) -> Report:
    """Issue every fragment for real, then try every mutation against it."""
    report = Report(fragments=len(texts))
    recorded = [
        issued.record(
            conn,
            f"{EVAL_URL}/{index}",
            EVAL_FETCHED_AT,
            {"text": text, "char_start": 0, "char_end": len(text)},
        )["fragment_id"]
        for index, text in enumerate(texts)
    ]

    for index, (text, fragment_id) in enumerate(zip(texts, recorded, strict=True)):
        for mutation in MUTATIONS:
            quote = mutation.apply(text)
            if quote is not None and mutation.needs_pair:
                quote = _stitch_with_neighbour(texts, index, quote)
            if quote is None:
                report.skip(mutation.name, mutation.family)
                continue
            report.add(
                mutation.name,
                mutation.family,
                rejected=rejected(conn, fragment_id, quote),
            )
    return report


def _stitch_with_neighbour(texts: Sequence[str], index: int, quote: str) -> str | None:
    """Weld this fragment's quote to the next fragment's — all wording is real."""
    if len(texts) < 2:
        return None
    other = pick_quote(texts[(index + 1) % len(texts)])
    return None if other is None else stitch(quote, other)


# --- corpus ------------------------------------------------------------------


def load_fragments(cache_dir: Path, limit: int, min_chars: int = MIN_FRAGMENT_CHARS) -> list[str]:
    """Real paragraphs from the FRAMES page cache, longest-lived first."""
    files = sorted(Path(cache_dir).glob("*.txt"))
    if not files:
        raise SystemExit(f"no cached pages in {cache_dir} — run eval/frames_recall.py first")
    return list(_paragraphs(files, limit, min_chars))


def _paragraphs(files: Iterable[Path], limit: int, min_chars: int) -> Iterable[str]:
    from research_state import fragments as fragments_module

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
    conn = db.connect(Path(args.cache_dir).parent / "citation_eval.sqlite")
    issued.init_schema(conn)
    briefs.init_schema(conn)
    report = evaluate(conn, texts)
    _print_summary(report)
    if args.json:
        args.json.write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
    return 0


def _print_summary(report: Report) -> None:
    print(f"fragments: {report.fragments}")
    print(f"{'mutation':<20}{'family':<12}{'n':>5}{'rejected':>10}{'rate':>8}{'n/a':>6}")
    for row in report.rows():
        print(
            f"{row.name:<20}{row.family!s:<12}{row.applicable:>5}"
            f"{row.rejected:>10}{row.rate:>8.0%}{row.not_applicable:>6}"
        )
    print(f"\ncatch rate (fabricated):    {report.catch_rate():.1%}")
    print(f"false-reject rate (faithful): {report.false_reject_rate():.1%}")


if __name__ == "__main__":
    sys.exit(main())
