"""Tests for the quote-length floor scan — no network is touched here."""

from __future__ import annotations

import pytest
import quote_floor as qf

PAGE = (
    "The constant k defaults to 60 in Elasticsearch. It can be tuned.\n\n"
    "The constant k defaults to 60 in Solr as well. Reciprocal rank fusion "
    "combines two ranked lists into one."
)


# --- pieces -----------------------------------------------------------------


def test_first_words_takes_the_opening_words_verbatim():
    assert qf.first_words("The constant k defaults to 60", 3) == "The constant k"


def test_first_words_returns_none_when_the_text_is_shorter_than_asked():
    assert qf.first_words("two words", 5) is None


def test_sentences_splits_a_paragraph_on_terminal_punctuation():
    found = qf.sentences("One thing happened. Then another! And a third?")
    assert found == ["One thing happened.", "Then another!", "And a third?"]


def test_sentences_ignores_fragments_without_words():
    assert qf.sentences("... . One real sentence.") == ["One real sentence."]


def test_word_count_counts_words_not_characters():
    assert qf.word_count("The constant k defaults to 60") == 6


# --- ambiguity --------------------------------------------------------------


def test_a_quote_shared_by_two_fragments_is_ambiguous():
    index = qf.build_index(["alpha beta gamma delta", "alpha beta gamma epsilon"])
    assert qf.is_ambiguous("alpha beta", index)


def test_a_quote_unique_to_one_fragment_is_not_ambiguous():
    index = qf.build_index(["alpha beta gamma delta", "one two three four"])
    assert not qf.is_ambiguous("gamma delta", index)


def test_ambiguity_ignores_case_and_whitespace():
    index = qf.build_index(["Alpha  Beta gamma", "alpha beta delta"])
    assert qf.is_ambiguous("ALPHA   beta", index)


# --- the scan ---------------------------------------------------------------


def test_scan_reports_one_row_per_threshold():
    report = qf.scan([PAGE], thresholds=(2, 4, 6))
    assert [row.words for row in report.rows] == [2, 4, 6]


def test_ambiguity_falls_as_the_floor_rises():
    # Both fragments open with the same seven words and then diverge.
    report = qf.scan(PAGE.split("\n\n"), thresholds=(2, 10))
    short, long_ = report.rows
    assert short.ambiguous_rate > long_.ambiguous_rate


def test_a_higher_floor_blocks_more_honest_short_sentences():
    report = qf.scan([PAGE], thresholds=(2, 10))
    short, long_ = report.rows
    assert long_.blocked_honest_rate > short.blocked_honest_rate


def test_rates_are_zero_on_an_empty_corpus():
    report = qf.scan([], thresholds=(4,))
    row = report.rows[0]
    assert (row.ambiguous_rate, row.blocked_honest_rate) == (0.0, 0.0)


def test_scan_counts_the_corpus_it_measured():
    report = qf.scan([PAGE], thresholds=(4,))
    assert report.fragments == 1
    assert report.honest_sentences == 4


def test_scan_rejects_a_non_positive_threshold():
    with pytest.raises(ValueError):
        qf.scan([PAGE], thresholds=(0,))
