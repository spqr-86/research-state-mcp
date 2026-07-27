"""Tests for the citation-rejection eval harness — no network is touched here."""

from __future__ import annotations

import citation_rejection as cr
import pytest

from research_state import briefs, db, issued

PARA = (
    "The Office is the title of several mockumentary sitcoms based on a British "
    "series created by Stephen Merchant in 2001. The original series starred "
    "Ricky Gervais as manager David Brent."
)


# --- picking the quote ------------------------------------------------------


def test_quote_is_the_first_sentence_of_the_fragment():
    assert cr.pick_quote(PARA).startswith("The Office is the title")
    assert cr.pick_quote(PARA).endswith("in 2001.")


def test_quote_is_verbatim_inside_the_fragment():
    assert cr.pick_quote(PARA) in PARA


def test_no_quote_from_an_empty_fragment():
    assert cr.pick_quote("   ") is None


def test_quote_from_a_fragment_without_a_full_stop_is_the_whole_text():
    assert cr.pick_quote("no full stop here") == "no full stop here"


# --- fabricated mutations ---------------------------------------------------


def test_invented_quote_shares_no_wording_with_the_source():
    invented = cr.invent(PARA)
    assert invented is not None
    assert invented not in PARA


def test_swapped_number_changes_the_digits():
    mutated = cr.swap_number("created by Stephen Merchant in 2001.")
    assert mutated is not None
    assert "2001" not in mutated
    assert "created by Stephen Merchant in" in mutated


def test_swap_number_gives_up_when_there_is_no_number():
    assert cr.swap_number("a sentence without any digits at all.") is None


def test_swapped_entity_changes_a_capitalised_name():
    mutated = cr.swap_entity("The original series starred Ricky Gervais as manager.")
    assert mutated is not None
    assert "Ricky" not in mutated
    assert "Thompson" in mutated


def test_swap_entity_gives_up_without_a_mid_sentence_capital():
    assert cr.swap_entity("a lowercase sentence with no names in it.") is None


def test_dropped_word_removes_one_word_and_keeps_the_rest():
    mutated = cr.drop_word("the quick brown fox jumps over")
    assert mutated is not None
    assert len(mutated.split()) == 5
    assert mutated != "the quick brown fox jumps over"


def test_drop_word_gives_up_on_a_too_short_quote():
    assert cr.drop_word("two words") is None


def test_stitch_joins_the_head_of_one_quote_to_the_tail_of_another():
    mutated = cr.stitch("alpha beta gamma delta", "one two three four")
    assert mutated == "alpha beta three four"


def test_stitch_gives_up_when_either_side_is_too_short():
    assert cr.stitch("alpha", "one two three four") is None


# --- faithful-but-retyped mutations -----------------------------------------


def test_curly_quotes_are_straightened():
    mutated = cr.straighten_quotes("he called it “the office”, later")
    assert mutated == 'he called it "the office", later'


def test_straighten_quotes_gives_up_without_curly_marks():
    assert cr.straighten_quotes('plain "ascii" only') is None


def test_non_breaking_space_becomes_a_plain_space():
    mutated = cr.plain_space("aired in 2001 on BBC Two")  # noqa: RUF001
    assert mutated == "aired in 2001 on BBC Two"


def test_plain_space_gives_up_without_an_exotic_space():
    assert cr.plain_space("ordinary spaces only") is None


def test_en_dash_becomes_a_hyphen():
    assert cr.plain_dash("ran 2001–2003 on BBC") == "ran 2001-2003 on BBC"  # noqa: RUF001


def test_plain_dash_gives_up_without_a_dash():
    assert cr.plain_dash("no dashes here") is None


def test_ellipsis_gap_elides_the_middle_of_the_quote():
    mutated = cr.ellipsis_gap("alpha beta gamma delta epsilon zeta eta theta")
    assert mutated is not None
    assert mutated.startswith("alpha beta gamma")
    assert mutated.endswith("zeta eta theta")
    assert "…" in mutated


def test_an_elided_quote_keeps_enough_words_to_clear_the_length_floor():
    """Otherwise this row measures the floor, not the ellipsis rule."""
    mutated = cr.ellipsis_gap("alpha beta gamma delta epsilon zeta eta theta")
    assert briefs.quote_words(mutated) >= briefs.MIN_QUOTE_WORDS


def test_ellipsis_gap_gives_up_on_a_short_quote():
    assert cr.ellipsis_gap("alpha beta gamma delta epsilon") is None


# --- trivial but genuine ----------------------------------------------------


def test_trivial_quote_is_short_and_still_verbatim():
    trivial = cr.trivial(PARA)
    assert trivial is not None
    assert trivial in PARA
    assert len(trivial.split()) <= cr.TRIVIAL_WORDS


# --- the real check ---------------------------------------------------------


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "eval.sqlite")
    issued.init_schema(connection)
    briefs.init_schema(connection)
    return connection


def _record(connection, text: str) -> str:
    fragment = issued.record(
        connection,
        "https://example.org/page",
        1_700_000_000,
        {"text": text, "char_start": 0, "char_end": len(text)},
    )
    return fragment["fragment_id"]


def test_verbatim_quote_is_accepted(conn):
    fragment_id = _record(conn, PARA)
    assert not cr.rejected(conn, fragment_id, cr.pick_quote(PARA))


def test_invented_quote_is_rejected(conn):
    fragment_id = _record(conn, PARA)
    assert cr.rejected(conn, fragment_id, cr.invent(PARA))


def test_quote_against_an_unknown_fragment_is_rejected(conn):
    assert cr.rejected(conn, "deadbeefdeadbeef", "anything")


# --- report -----------------------------------------------------------------


def test_report_counts_applicable_and_rejected_per_mutation():
    report = cr.Report()
    report.add("swap_number", cr.Family.FABRICATED, rejected=True)
    report.add("swap_number", cr.Family.FABRICATED, rejected=False)
    report.add("swap_number", cr.Family.FABRICATED, rejected=True)
    row = report.rows()[0]
    assert (row.applicable, row.rejected) == (3, 2)
    assert row.rate == pytest.approx(2 / 3)


def test_report_counts_a_mutation_that_did_not_apply():
    report = cr.Report()
    report.skip("swap_number")
    report.add("swap_number", cr.Family.FABRICATED, rejected=True)
    row = report.rows()[0]
    assert (row.applicable, row.not_applicable) == (1, 1)


def test_catch_rate_covers_the_fabricated_family_only():
    report = cr.Report()
    report.add("invent", cr.Family.FABRICATED, rejected=True)
    report.add("invent", cr.Family.FABRICATED, rejected=False)
    report.add("plain_dash", cr.Family.FAITHFUL, rejected=True)
    assert report.catch_rate() == pytest.approx(0.5)


def test_false_reject_rate_covers_the_faithful_family_only():
    report = cr.Report()
    report.add("plain_dash", cr.Family.FAITHFUL, rejected=True)
    report.add("plain_dash", cr.Family.FAITHFUL, rejected=False)
    report.add("invent", cr.Family.FABRICATED, rejected=True)
    assert report.false_reject_rate() == pytest.approx(0.5)


def test_rates_are_zero_when_nothing_was_measured():
    report = cr.Report()
    assert report.catch_rate() == 0.0
    assert report.false_reject_rate() == 0.0


# --- end to end over fragments ----------------------------------------------


OTHER = (
    "Radio Times reported that the sixth series was commissioned in 2009 after a "
    "long delay. Critics praised the ensemble cast throughout its run."
)


def test_evaluate_runs_every_mutation_over_the_given_fragments(conn):
    report = cr.evaluate(conn, [PARA, OTHER])
    names = {row.name for row in report.rows()}
    assert "invent" in names and "trivial" in names
    assert report.catch_rate() == 1.0


def test_a_verbatim_but_worthless_quote_is_now_stopped_by_the_length_floor(conn):
    """Before the floor this row passed 100% of the time — that was the hole."""
    report = cr.evaluate(conn, [PARA, OTHER])
    row = next(row for row in report.rows() if row.name == "trivial")
    assert row.family is cr.Family.TRIVIAL
    assert row.rejected == row.applicable
    assert cr.TRIVIAL_WORDS < briefs.MIN_QUOTE_WORDS


def test_evaluate_never_rejects_the_verbatim_control(conn):
    report = cr.evaluate(conn, [PARA])
    control = next(row for row in report.rows() if row.name == "verbatim")
    assert control.rejected == 0
    assert control.applicable == 1


def test_a_leading_ellipsis_is_not_taken_for_a_sentence():
    """Real cached pages start mid-quotation: "... two gentlemen come in, ..."."""
    quote = cr.pick_quote("... two gentlemen come in, leading a tiny lady. She may be.")
    assert quote == "two gentlemen come in, leading a tiny lady."


def test_a_fragment_with_no_words_yields_no_quote():
    assert cr.pick_quote("... . ...") is None
