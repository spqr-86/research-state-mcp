"""Tests for the FRAMES eval harness — no network is touched here."""

from __future__ import annotations

import hashlib

import frames_recall as fr
import pytest

# --- answer_present ---------------------------------------------------------


def test_answer_found_verbatim():
    assert fr.answer_present("Napoleon", "born to Napoleon in 1802")


def test_answer_match_ignores_case():
    assert fr.answer_present("NAPOLEON bonaparte", "...napoleon Bonaparte...")


def test_answer_match_collapses_whitespace():
    assert fr.answer_present("Napoleon  Bonaparte", "Napoleon\n  Bonaparte was")


def test_answer_match_ignores_trailing_period():
    assert fr.answer_present("42 years.", "he was 42 years old")


def test_answer_absent():
    assert not fr.answer_present("Napoleon", "Caesar crossed the Rubicon")


def test_empty_answer_is_never_present():
    assert not fr.answer_present("", "any text at all")
    assert not fr.answer_present("   ", "any text at all")


def test_answer_absent_from_empty_text():
    assert not fr.answer_present("Napoleon", "")


# --- wikipedia links --------------------------------------------------------


def test_links_are_collected_in_numeric_order():
    row = {
        "Prompt": "q",
        "wikipedia_link_1": "https://en.wikipedia.org/wiki/A",
        "wikipedia_link_2": "https://en.wikipedia.org/wiki/B",
        "wikipedia_link_10": "https://en.wikipedia.org/wiki/J",
        "wikipedia_link_11+": "https://en.wikipedia.org/wiki/K",
    }
    assert fr.wikipedia_links(row) == [
        "https://en.wikipedia.org/wiki/A",
        "https://en.wikipedia.org/wiki/B",
        "https://en.wikipedia.org/wiki/J",
        "https://en.wikipedia.org/wiki/K",
    ]


def test_blank_and_missing_links_are_dropped():
    row = {
        "wikipedia_link_1": "https://en.wikipedia.org/wiki/A",
        "wikipedia_link_2": "",
        "wikipedia_link_3": None,
        "wikipedia_link_4": "   ",
    }
    assert fr.wikipedia_links(row) == ["https://en.wikipedia.org/wiki/A"]


def test_link_field_may_hold_several_urls():
    row = {
        "wikipedia_link_11+": ("https://en.wikipedia.org/wiki/A\nhttps://en.wikipedia.org/wiki/B")
    }
    assert fr.wikipedia_links(row) == [
        "https://en.wikipedia.org/wiki/A",
        "https://en.wikipedia.org/wiki/B",
    ]


def test_duplicate_links_appear_once():
    row = {
        "wikipedia_link_1": "https://en.wikipedia.org/wiki/A",
        "wikipedia_link_2": "https://en.wikipedia.org/wiki/A",
    }
    assert fr.wikipedia_links(row) == ["https://en.wikipedia.org/wiki/A"]


def test_title_from_url_is_unquoted():
    url = "https://en.wikipedia.org/wiki/Napoleon_%C3%89mile"
    assert fr.title_from_url(url) == "Napoleon Émile"


def test_title_from_url_drops_fragment_and_query():
    url = "https://en.wikipedia.org/wiki/Paris?action=raw#History"
    assert fr.title_from_url(url) == "Paris"


def test_title_from_a_non_article_url_is_none():
    assert fr.title_from_url("https://example.com/Napoleon") is None


# --- extract text to paragraphs --------------------------------------------


def test_extract_lines_become_blank_line_separated_paragraphs():
    raw = "Intro line.\nSecond paragraph.\n\n\n== History ==\nThird paragraph."
    assert fr.paragraphs_from_extract(raw) == (
        "Intro line.\n\nSecond paragraph.\n\n== History ==\n\nThird paragraph."
    )


def test_extract_text_is_stripped_per_line():
    assert fr.paragraphs_from_extract("  a  \n\t b \n") == "a\n\nb"


def test_empty_extract_gives_empty_text():
    assert fr.paragraphs_from_extract("") == ""
    assert fr.paragraphs_from_extract(None) == ""


# --- metrics ----------------------------------------------------------------


def _page(answer: str, filler: str = "filler") -> str:
    blocks = [f"{filler} paragraph number {i}." for i in range(20)]
    blocks[10] = f"The answer is {answer} and nothing else."
    return "\n\n".join(blocks)


def _ok(text: str):
    """A fetcher that always serves the same page."""
    return lambda url: fr.PageResult.ok(text)


def _pages(mapping: dict):
    def fetch(url: str):
        text = mapping.get(url)
        return fr.PageResult.ok(text) if text else fr.PageResult.missing()

    return fetch


def test_metrics_on_a_hit_and_a_miss():
    rows = [
        {
            "Prompt": "answer number seventeen",
            "Answer": "seventeen",
            "wikipedia_link_1": "https://en.wikipedia.org/wiki/Hit",
        },
        {
            "Prompt": "totally unrelated wording",
            "Answer": "not on the page",
            "wikipedia_link_1": "https://en.wikipedia.org/wiki/Miss",
        },
    ]
    fetch = _pages(
        {
            "https://en.wikipedia.org/wiki/Hit": _page("seventeen"),
            "https://en.wikipedia.org/wiki/Miss": _page("nineteen"),
        }
    )
    report = fr.evaluate(rows, fetch, k=5, neighbours=1)

    assert report.total == 2
    assert report.skipped == 0
    assert report.answer_on_page == 1
    assert report.hits_per_example == 1
    assert report.recall_at_k_per_example == pytest.approx(0.5)
    assert report.recall_at_k_per_example_when_present == pytest.approx(1.0)
    assert report.avg_pages == pytest.approx(1.0)
    assert 0.0 < report.avg_returned_ratio < 1.0
    assert report.avg_fragments > 0


def test_per_example_pool_is_capped_at_k_across_all_pages():
    rows = [
        {
            "Prompt": "filler paragraph number",
            "Answer": "seventeen",
            "wikipedia_link_1": "https://en.wikipedia.org/wiki/A",
            "wikipedia_link_2": "https://en.wikipedia.org/wiki/B",
            "wikipedia_link_3": "https://en.wikipedia.org/wiki/C",
        }
    ]
    report = fr.evaluate(rows, _ok(_page("seventeen")), k=3, neighbours=0)
    assert report.avg_pages == pytest.approx(3.0)
    assert report.avg_fragments <= 3


def test_the_per_page_variant_really_beats_the_per_example_one():
    """The two metrics must actually diverge, or naming them apart means nothing.

    A decoy page repeats the query words far more often than the page that
    carries the answer, so with k=1 the pooled top-1 comes from the decoy and
    the answer is dropped — while the per-page variant still extracts it,
    because that page gets its own budget of one fragment.
    """
    decoy = "\n\n".join(
        ["rivers and bridges of no interest here"] * 10
        + ["lisbon capital treaty, all three words at once"]
        + ["rivers and bridges of no interest here"] * 9
    )
    answer_page = "\n\n".join(
        [
            f"lisbon treaty capital background note {i} with a good deal of padding text"
            for i in range(10)
        ]
        + ["lisbon treaty capital 2007"]
        + [
            f"lisbon treaty capital background note {i} with a good deal of padding text"
            for i in range(11, 20)
        ]
    )
    rows = [
        {
            "Prompt": "lisbon capital treaty",
            "Answer": "2007",
            "wikipedia_link_1": "https://en.wikipedia.org/wiki/Decoy",
            "wikipedia_link_2": "https://en.wikipedia.org/wiki/Answer",
        }
    ]
    pages = {
        "https://en.wikipedia.org/wiki/Decoy": decoy,
        "https://en.wikipedia.org/wiki/Answer": answer_page,
    }
    report = fr.evaluate(rows, _pages(pages), k=1, neighbours=0)
    assert report.answer_on_page == 1
    assert report.hits_per_page == 1
    assert report.hits_per_example == 0


def test_a_row_without_links_is_skipped_with_a_reason():
    rows = [{"Prompt": "q", "Answer": "a"}]
    report = fr.evaluate(rows, _ok(""), k=5, neighbours=1)
    assert report.skipped == report.skipped_no_links == 1
    assert report.skipped_network == report.skipped_no_page == 0
    assert report.total == 1
    assert report.answer_on_page == 0


def test_a_row_whose_pages_are_all_missing_is_skipped_as_no_page():
    rows = [{"Prompt": "q", "Answer": "a", "wikipedia_link_1": "https://x/wiki/A"}]
    report = fr.evaluate(rows, lambda url: fr.PageResult.missing(), k=5, neighbours=1)
    assert report.skipped_no_page == 1
    assert report.skipped_network == 0
    assert report.hits_per_example == 0


def test_a_row_whose_page_request_failed_is_skipped_as_network():
    rows = [{"Prompt": "q", "Answer": "a", "wikipedia_link_1": "https://x/wiki/A"}]
    report = fr.evaluate(rows, lambda url: fr.PageResult.failed(), k=5, neighbours=1)
    assert report.skipped_network == 1
    assert report.skipped_no_page == 0


def test_one_network_failure_among_missing_pages_wins_the_reason():
    rows = [
        {
            "Prompt": "q",
            "Answer": "a",
            "wikipedia_link_1": "https://en.wikipedia.org/wiki/Gone",
            "wikipedia_link_2": "https://en.wikipedia.org/wiki/Flaky",
        }
    ]
    outcomes = {
        "https://en.wikipedia.org/wiki/Gone": fr.PageResult.missing(),
        "https://en.wikipedia.org/wiki/Flaky": fr.PageResult.failed(),
    }
    report = fr.evaluate(rows, outcomes.__getitem__, k=5, neighbours=1)
    assert report.skipped_network == 1


def test_short_answers_get_their_own_slice():
    rows = [
        {
            "Prompt": "answer number 42",
            "Answer": "42",
            "wikipedia_link_1": "https://en.wikipedia.org/wiki/A",
        },
        {
            "Prompt": "answer number seventeen",
            "Answer": "seventeen",
            "wikipedia_link_1": "https://en.wikipedia.org/wiki/B",
        },
    ]
    fetch = _pages(
        {
            "https://en.wikipedia.org/wiki/A": _page("42"),
            "https://en.wikipedia.org/wiki/B": _page("seventeen"),
        }
    )
    report = fr.evaluate(rows, fetch, k=5, neighbours=1)
    assert report.short_answers == 1
    assert report.short_answers_on_page == 1
    assert report.short_answer_hits == 1
    assert report.recall_short_answers == pytest.approx(1.0)


def test_an_answer_only_spelled_out_across_the_join_is_not_a_hit():
    """Two fragments whose junction reads as the answer must not score."""
    page = "\n\n".join(
        [
            "alpha query ends with red",
            "unrelated middle block",
            "herring is a query too",
        ]
    )
    rows = [
        {
            "Prompt": "query",
            "Answer": "red herring",
            "wikipedia_link_1": "https://en.wikipedia.org/wiki/A",
        }
    ]
    report = fr.evaluate(rows, _ok(page), k=2, neighbours=0)
    assert report.answer_on_page == 0
    assert report.hits_per_example == 0
    assert report.join_artefacts == 1


def test_report_dict_is_json_shaped_and_hides_private_fields():
    rows = [
        {
            "Prompt": "seventeen",
            "Answer": "seventeen",
            "wikipedia_link_1": "https://en.wikipedia.org/wiki/A",
        }
    ]
    data = fr.evaluate(rows, _ok(_page("seventeen")), k=2, neighbours=0).as_dict()
    assert data["k"] == 2
    assert data["neighbours"] == 0
    assert data["hits_per_example"] == 1
    assert not [key for key in data if key.startswith("_")]
    assert set(data) >= {
        "total",
        "skipped",
        "skipped_no_links",
        "skipped_network",
        "skipped_no_page",
        "avg_pages",
        "answer_on_page",
        "recall_at_k_per_example",
        "recall_at_k_per_page",
        "recall_at_k_per_example_when_present",
        "recall_at_k_per_page_when_present",
        "avg_returned_ratio",
        "avg_fragments",
        "short_answers",
        "recall_short_answers",
    }


# --- row paging -------------------------------------------------------------


def test_rows_are_paged_until_the_limit_is_reached():
    calls: list[tuple[int, int]] = []

    def fake_fetch(offset: int, length: int) -> list[dict]:
        calls.append((offset, length))
        return [{"Prompt": f"q{offset + i}"} for i in range(length)]

    rows = fr.load_rows(250, fake_fetch)
    assert len(rows) == 250
    assert calls == [(0, 100), (100, 100), (200, 50)]


def test_paging_stops_when_the_dataset_runs_out():
    def fake_fetch(offset: int, length: int) -> list[dict]:
        return [{"Prompt": "q"}] * 10 if offset == 0 else []

    assert len(fr.load_rows(200, fake_fetch)) == 10


# --- page cache -------------------------------------------------------------


def test_cached_page_is_not_downloaded_twice(tmp_path):
    downloads: list[str] = []

    def download(title: str):
        downloads.append(title)
        return fr.PageResult.ok("body text")

    url = "https://en.wikipedia.org/wiki/A"
    first = fr.page_text(url, tmp_path, download)
    second = fr.page_text(url, tmp_path, download)
    assert first.text == second.text == "body text"
    assert first.status is fr.PageStatus.OK
    assert downloads == ["A"]


def test_a_missing_article_is_cached_as_empty(tmp_path):
    calls: list[str] = []

    def download(title: str):
        calls.append(title)
        return fr.PageResult.missing()

    url = "https://en.wikipedia.org/wiki/A"
    assert fr.page_text(url, tmp_path, download).status is fr.PageStatus.MISSING
    assert fr.page_text(url, tmp_path, download).status is fr.PageStatus.MISSING
    assert calls == ["A"]


def test_a_failed_request_is_never_cached(tmp_path):
    calls: list[str] = []

    def download(title: str):
        calls.append(title)
        return fr.PageResult.failed()

    url = "https://en.wikipedia.org/wiki/A"
    assert fr.page_text(url, tmp_path, download).status is fr.PageStatus.FAILED
    assert fr.page_text(url, tmp_path, download).status is fr.PageStatus.FAILED
    assert calls == ["A", "A"]
    assert list(tmp_path.iterdir()) == []


def test_a_failure_then_a_success_serves_the_page(tmp_path):
    outcomes = [fr.PageResult.failed(), fr.PageResult.ok("body")]
    url = "https://en.wikipedia.org/wiki/A"

    def download(title: str):
        return outcomes.pop(0)

    assert fr.page_text(url, tmp_path, download).status is fr.PageStatus.FAILED
    assert fr.page_text(url, tmp_path, download).text == "body"


def test_a_non_wikipedia_url_is_never_downloaded(tmp_path):
    def download(title: str):
        raise AssertionError("must not be called")

    result = fr.page_text("https://example.com/x", tmp_path, download)
    assert result.status is fr.PageStatus.MISSING


# --- download_page ----------------------------------------------------------


def _payload(*pages: dict) -> dict:
    return {"query": {"pages": list(pages)}}


def test_download_page_returns_the_first_non_empty_page():
    payload = _payload(
        {"missing": True},
        {"extract": ""},
        {"extract": "line one\nline two"},
    )
    result = fr.download_page("A", get_json=lambda url: payload)
    assert result.status is fr.PageStatus.OK
    assert result.text == "line one\n\nline two"


def test_download_page_reports_a_failed_request_as_failed():
    result = fr.download_page("A", get_json=lambda url: None)
    assert result.status is fr.PageStatus.FAILED
    assert result.text is None


def test_download_page_reports_a_missing_article_as_missing():
    result = fr.download_page("A", get_json=lambda url: _payload({"missing": True}))
    assert result.status is fr.PageStatus.MISSING


def test_download_page_with_no_pages_at_all_is_missing():
    assert fr.download_page("A", get_json=lambda url: {}).status is fr.PageStatus.MISSING


def test_download_page_asks_for_the_requested_title():
    seen: list[str] = []

    def get_json(url: str):
        seen.append(url)
        return _payload({"extract": "body"})

    fr.download_page("New York", get_json=get_json)
    assert "titles=New+York" in seen[0]


def test_download_page_raw_text_keeps_the_body_as_is():
    payload = _payload({"extract": "  line one\nline two  "})
    result = fr.download_page("A", restructure=False, get_json=lambda url: payload)
    assert result.text == "line one\nline two"


# --- edges: grader ----------------------------------------------------------


def test_answer_present_tolerates_none_answer():
    assert not fr.answer_present(None, "Napoleon was here")


def test_answer_present_tolerates_none_text():
    assert not fr.answer_present("Napoleon", None)


def test_answer_that_is_only_a_period_is_never_present():
    assert not fr.answer_present(".", "a sentence.")


def test_answer_trailing_periods_all_stripped():
    assert fr.answer_present("42...", "he was 42 years old")


def test_answer_inside_a_word_is_not_present():
    assert not fr.answer_present("cat", "concatenation")


def test_a_short_number_does_not_match_inside_a_longer_one():
    assert not fr.answer_present("4", "There were 14 ships")
    assert not fr.answer_present("2 years", "the 12 years war")
    assert fr.answer_present("4", "There were 4 ships")


def test_an_answer_with_punctuation_edges_still_matches():
    assert fr.answer_present("$5", "it cost $5 in total")
    assert fr.answer_present("(1912)", "born (1912) in Kiev")


def test_regex_metacharacters_in_the_answer_are_literal():
    assert fr.answer_present("c++", "written in c++ back then")
    assert not fr.answer_present("a.c", "abc")


def test_short_answers_are_flagged():
    assert fr.is_short_answer("42")
    assert fr.is_short_answer("two")
    assert not fr.is_short_answer("1912")
    assert not fr.is_short_answer("")


# --- edges: links -----------------------------------------------------------


def test_a_row_with_no_link_fields_at_all_yields_no_links():
    assert fr.wikipedia_links({"Prompt": "q", "Answer": "a"}) == []


def test_an_empty_row_yields_no_links():
    assert fr.wikipedia_links({}) == []


def test_non_string_link_values_are_ignored():
    row = {
        "wikipedia_link_1": 42,
        "wikipedia_link_2": ["https://en.wikipedia.org/wiki/A"],
    }
    assert fr.wikipedia_links(row) == []


def test_fields_that_only_look_like_link_fields_are_ignored():
    row = {"wikipedia_link": "https://en.wikipedia.org/wiki/A", "wiki_link_1": "x"}
    assert fr.wikipedia_links(row) == []


def test_trailing_punctuation_is_stripped_from_a_url():
    row = {"wikipedia_link_1": "https://en.wikipedia.org/wiki/A, https://en.wikipedia.org/wiki/B;"}
    assert fr.wikipedia_links(row) == [
        "https://en.wikipedia.org/wiki/A",
        "https://en.wikipedia.org/wiki/B",
    ]


def test_a_link_field_without_a_url_contributes_nothing():
    assert fr.wikipedia_links({"wikipedia_link_1": "see the article"}) == []


def test_title_from_a_bare_wiki_root_url_is_none():
    assert fr.title_from_url("https://en.wikipedia.org/wiki/") is None


def test_title_from_a_non_english_wikipedia_is_accepted():
    assert fr.title_from_url("https://fr.wikipedia.org/wiki/Paris") == "Paris"


def test_title_from_url_ignores_surrounding_whitespace():
    assert fr.title_from_url("  https://en.wikipedia.org/wiki/Paris  ") == "Paris"


# --- edges: extract ---------------------------------------------------------


def test_extract_of_only_whitespace_is_empty():
    assert fr.paragraphs_from_extract("   \n\t\n  ") == ""


def test_extract_of_a_single_line_is_unchanged():
    assert fr.paragraphs_from_extract("just one line") == "just one line"


# --- edges: page cache ------------------------------------------------------


def test_a_pre_existing_zero_size_cache_file_means_missing(tmp_path):
    title = "A"
    path = tmp_path / f"{hashlib.sha256(title.encode()).hexdigest()}.txt"
    path.write_text("", encoding="utf-8")

    def download(_: str):
        raise AssertionError("must not be called")

    result = fr.page_text("https://en.wikipedia.org/wiki/A", tmp_path, download)
    assert result.status is fr.PageStatus.MISSING


def test_the_cache_directory_is_created_on_demand(tmp_path):
    cache = tmp_path / "nested" / "cache"
    result = fr.page_text(
        "https://en.wikipedia.org/wiki/A", cache, lambda t: fr.PageResult.ok("body")
    )
    assert result.text == "body"
    assert cache.is_dir()
    assert len(list(cache.iterdir())) == 1


def test_urls_that_name_the_same_article_share_one_cache_entry(tmp_path):
    calls: list[str] = []

    def download(title: str):
        calls.append(title)
        return fr.PageResult.ok("body")

    assert fr.page_text("https://en.wikipedia.org/wiki/New_York", tmp_path, download).text == "body"
    assert (
        fr.page_text("https://en.wikipedia.org/wiki/New%20York", tmp_path, download).text == "body"
    )
    assert calls == ["New York"]


def test_a_non_article_url_does_not_create_a_cache_directory(tmp_path):
    cache = tmp_path / "cache"
    result = fr.page_text("https://example.com/x", cache, lambda t: fr.PageResult.ok("body"))
    assert result.status is fr.PageStatus.MISSING
    assert not cache.exists()


# --- edges: paging ----------------------------------------------------------


def test_a_limit_that_is_an_exact_multiple_of_the_page_size_stops_early():
    calls: list[tuple[int, int]] = []

    def fake_fetch(offset: int, length: int) -> list[dict]:
        calls.append((offset, length))
        return [{"Prompt": "q"}] * length

    assert len(fr.load_rows(200, fake_fetch)) == 200
    assert calls == [(0, 100), (100, 100)]


def test_a_limit_below_the_page_size_asks_for_exactly_that_many():
    calls: list[tuple[int, int]] = []

    def fake_fetch(offset: int, length: int) -> list[dict]:
        calls.append((offset, length))
        return [{"Prompt": "q"}] * length

    assert len(fr.load_rows(7, fake_fetch)) == 7
    assert calls == [(0, 7)]


def test_a_zero_limit_never_calls_the_fetcher():
    def fake_fetch(offset: int, length: int) -> list[dict]:
        raise AssertionError("must not be called")

    assert fr.load_rows(0, fake_fetch) == []


def test_an_overlong_page_is_truncated_to_the_limit():
    def fake_fetch(offset: int, length: int) -> list[dict]:
        return [{"Prompt": f"q{i}"} for i in range(length + 5)]

    rows = fr.load_rows(10, fake_fetch)
    assert len(rows) == 10


def test_a_short_page_is_topped_up_by_the_next_call():
    calls: list[tuple[int, int]] = []

    def fake_fetch(offset: int, length: int) -> list[dict]:
        calls.append((offset, length))
        return [{"Prompt": "q"}] * 3

    assert len(fr.load_rows(10, fake_fetch)) == 10
    assert calls == [(0, 10), (3, 7), (6, 4), (9, 1)]


def test_an_immediately_empty_split_yields_no_rows():
    assert fr.load_rows(50, lambda offset, length: []) == []


# --- edges: metrics ---------------------------------------------------------


def test_no_rows_at_all_gives_a_zeroed_report_and_no_division_by_zero():
    report = fr.evaluate([], lambda url: fr.PageResult.missing(), k=5, neighbours=1)
    assert report.total == 0
    assert report.skipped == 0
    assert report.recall_at_k_per_example == 0.0
    assert report.recall_at_k_per_page == 0.0
    assert report.recall_at_k_per_example_when_present == 0.0
    assert report.recall_short_answers == 0.0
    assert report.avg_returned_ratio == 0.0
    assert report.avg_fragments == 0.0
    assert report.avg_pages == 0.0


def test_every_row_skipped_leaves_the_ratios_at_zero():
    rows = [{"Prompt": "q", "Answer": "a"}, {"Prompt": "q2", "Answer": "b"}]
    report = fr.evaluate(rows, lambda url: fr.PageResult.missing(), k=5, neighbours=1)
    assert report.total == report.skipped == report.skipped_no_links == 2
    assert report.recall_at_k_per_example == 0.0
    assert report.avg_returned_ratio == 0.0
    assert report.avg_fragments == 0.0


def test_one_dead_page_among_several_does_not_skip_the_row():
    rows = [
        {
            "Prompt": "answer number seventeen",
            "Answer": "seventeen",
            "wikipedia_link_1": "https://en.wikipedia.org/wiki/Dead",
            "wikipedia_link_2": "https://en.wikipedia.org/wiki/Live",
        }
    ]
    fetch = _pages({"https://en.wikipedia.org/wiki/Live": _page("seventeen")})
    report = fr.evaluate(rows, fetch, k=5, neighbours=1)
    assert report.skipped == 0
    assert report.answer_on_page == 1
    assert report.hits_per_example == 1
    assert report.avg_pages == pytest.approx(1.0)


def test_a_row_without_a_prompt_or_answer_is_still_scored():
    rows = [{"wikipedia_link_1": "https://en.wikipedia.org/wiki/A"}]
    report = fr.evaluate(rows, _ok(_page("seventeen")), k=5, neighbours=1)
    assert report.total == 1
    assert report.skipped == 0
    assert report.answer_on_page == 0
    assert report.hits_per_example == 0


def test_a_none_answer_counts_as_absent_rather_than_crashing():
    rows = [
        {
            "Prompt": "seventeen",
            "Answer": None,
            "wikipedia_link_1": "https://en.wikipedia.org/wiki/A",
        }
    ]
    report = fr.evaluate(rows, _ok(_page("seventeen")), k=5, neighbours=1)
    assert report.answer_on_page == 0
    assert report.hits_per_example == 0
    assert report.recall_at_k_per_example == 0.0


def test_rows_are_consumed_from_a_generator_once():
    rows = (
        {
            "Prompt": "seventeen",
            "Answer": "seventeen",
            "wikipedia_link_1": "https://en.wikipedia.org/wiki/A",
        }
        for _ in range(3)
    )
    report = fr.evaluate(rows, _ok(_page("seventeen")), k=5, neighbours=1)
    assert report.total == 3
    assert report.hits_per_example == 3
    assert report.recall_at_k_per_example == pytest.approx(1.0)


def test_the_returned_ratio_is_one_when_everything_comes_back():
    rows = [
        {
            "Prompt": "seventeen",
            "Answer": "seventeen",
            "wikipedia_link_1": "https://en.wikipedia.org/wiki/A",
        }
    ]
    report = fr.evaluate(rows, _ok("The answer is seventeen."), k=5, neighbours=1)
    assert report.avg_returned_ratio == pytest.approx(1.0)
    assert report.avg_fragments == 1


def test_the_raw_text_flag_is_recorded_in_the_report():
    report = fr.evaluate([], lambda url: fr.PageResult.missing(), 5, 1, raw_text=True)
    assert report.as_dict()["raw_text"] is True


# --- CLI arguments ----------------------------------------------------------


@pytest.mark.parametrize("argv", [["--limit", "0"], ["--limit", "-3"], ["--k", "0"]])
def test_non_positive_limit_or_k_is_rejected(argv):
    with pytest.raises(SystemExit):
        fr._parse_args(argv)


def test_a_negative_neighbour_count_is_rejected():
    with pytest.raises(SystemExit):
        fr._parse_args(["--neighbours", "-1"])


def test_defaults_are_sane():
    args = fr._parse_args([])
    assert args.limit == fr.DEFAULT_LIMIT
    assert args.k >= 1
    assert args.neighbours >= 0
    assert args.raw_text is False


def test_the_raw_text_flag_is_parsed():
    assert fr._parse_args(["--raw-text"]).raw_text is True


# --- main() wiring ----------------------------------------------------------


def _capture_main(monkeypatch, argv, tmp_path):
    """Run main() with the network stubbed, returning what it wired up."""
    seen: dict = {}
    monkeypatch.setattr(fr, "load_rows", lambda limit, fetch: [{"Prompt": "q"}])
    monkeypatch.setattr(fr, "_print_summary", lambda report: None)

    def fake_evaluate(rows, fetch_page, k, neighbours, raw_text=False):
        seen["fetch_page"] = fetch_page
        seen["k"] = k
        seen["raw_text"] = raw_text
        return fr.Report(k=k, neighbours=neighbours, raw_text=raw_text)

    monkeypatch.setattr(fr, "evaluate", fake_evaluate)

    def fake_download(title, restructure=True):
        seen["restructure"] = restructure
        return fr.PageResult.ok("body")

    monkeypatch.setattr(fr, "download_page", fake_download)
    code = fr.main([*argv, "--cache-dir", str(tmp_path)])
    seen["code"] = code
    return seen


def test_main_uses_the_paragraph_cache_and_restructures_by_default(monkeypatch, tmp_path):
    seen = _capture_main(monkeypatch, [], tmp_path)
    seen["fetch_page"]("https://en.wikipedia.org/wiki/A")
    assert seen["code"] == 0
    assert seen["raw_text"] is False
    assert seen["restructure"] is True
    assert (tmp_path / "paragraphs").is_dir()
    assert not (tmp_path / "raw").exists()


def test_main_keeps_the_raw_corpus_in_a_separate_cache(monkeypatch, tmp_path):
    """The two text modes must never share a cache entry — that would mix corpora."""
    seen = _capture_main(monkeypatch, ["--raw-text"], tmp_path)
    seen["fetch_page"]("https://en.wikipedia.org/wiki/A")
    assert seen["raw_text"] is True
    assert seen["restructure"] is False
    assert (tmp_path / "raw").is_dir()
    assert not (tmp_path / "paragraphs").exists()


def test_main_reports_failure_when_no_rows_load(monkeypatch, tmp_path):
    monkeypatch.setattr(fr, "load_rows", lambda limit, fetch: [])
    assert fr.main(["--cache-dir", str(tmp_path)]) == 1
