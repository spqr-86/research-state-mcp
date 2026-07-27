import sqlite3

import pytest

from research_state import fragments

PAGE = """# Reciprocal Rank Fusion

RRF combines several ranked lists into one. Each document gets a score that is the
sum of one over k plus its rank in every list where it appears.

The constant k is usually set to 60. Elasticsearch and Azure AI Search both ship
this default and it rarely needs tuning.

Cats are unrelated to search ranking and this paragraph exists only as noise about
kittens and their napping habits.

Because the score depends on rank and not on the raw relevance score, RRF needs no
score normalisation between the lists it fuses.
"""

RUSSIAN_PAGE = """Взаимное ранговое слияние

RRF объединяет несколько ранжированных списков в один общий список результатов.

Константа k обычно равна шестидесяти, это значение по умолчанию в Elasticsearch.

Котики к ранжированию отношения не имеют, этот абзац здесь только как шум.
"""


def test_split_paragraphs_drops_blanks_and_keeps_order():
    paras = fragments.split_paragraphs(PAGE)
    assert len(paras) == 5
    assert paras[0] == "# Reciprocal Rank Fusion"
    assert all(p.strip() for p in paras)


def test_split_paragraphs_on_empty_text():
    assert fragments.split_paragraphs("   \n\n  ") == []


def test_rank_finds_the_relevant_paragraph():
    result = fragments.extract(
        PAGE, "what is the default value of k", k=1, neighbours=0
    )
    assert len(result) == 1
    assert "60" in result[0]["text"]


def test_extract_returns_neighbour_context():
    result = fragments.extract(PAGE, "default value of k", k=1, neighbours=1)
    assert len(result) == 1
    # the hit plus one paragraph on each side, joined in document order
    assert "60" in result[0]["text"]
    assert "sum of one over k" in result[0]["text"]
    assert result[0]["paragraph_index"] == 2


def test_extract_never_returns_the_whole_page():
    result = fragments.extract(
        PAGE, "rank fusion score normalisation", k=2, neighbours=0
    )
    joined = "\n".join(f["text"] for f in result)
    assert "kittens" not in joined
    assert len(joined) < len(PAGE)


def test_extract_respects_k():
    """k is a cap on returned fragments — merging neighbours may yield fewer, never more."""
    for k in (1, 2, 3):
        assert (
            1 <= len(fragments.extract(PAGE, "rank list score", k=k, neighbours=0)) <= k
        )


def test_extract_works_on_russian():
    result = fragments.extract(
        RUSSIAN_PAGE, "чему равна константа k", k=1, neighbours=0
    )
    assert "шестидесяти" in result[0]["text"]


def test_extract_survives_fts_operator_characters_in_query():
    """A raw user query may contain quotes, NEAR, AND, hyphens — none may crash FTS5."""
    result = fragments.extract(
        PAGE, 'k "AND" NEAR OR (rank) - default*', k=1, neighbours=0
    )
    assert result


def test_extract_on_query_with_no_match_returns_nothing():
    assert fragments.extract(PAGE, "квантовая хромодинамика кварков", k=3) == []


def test_extract_on_empty_text_returns_nothing():
    assert fragments.extract("", "anything", k=3) == []


def test_extract_merges_adjacent_hits_into_one_fragment():
    text = "alpha beta\n\ngamma beta\n\nfar away noise\n\nmore noise here"
    result = fragments.extract(text, "beta", k=2, neighbours=1)
    assert len(result) == 1
    assert "alpha beta" in result[0]["text"] and "gamma beta" in result[0]["text"]


def test_scores_are_descending():
    result = fragments.extract(
        PAGE, "rank fusion normalisation score", k=3, neighbours=0
    )
    scores = [f["score"] for f in result]
    assert scores == sorted(scores, reverse=True)


# --- pages with no paragraph structure ---------------------------------------

# A dataset viewer, a changelog, a wiki table: one "paragraph" of many lines and
# no blank line anywhere. Splitting on blank lines alone hands back the page.
TABLE_PAGE = "| id | topic | text |\n" + "\n".join(
    f"| {i} | {'needle' if i == 40 else 'filler'} | " + ("lorem ipsum " * 20) + "|"
    for i in range(120)
)


def test_a_line_only_page_is_split_into_many_paragraphs():
    paras = fragments.split_paragraphs(TABLE_PAGE)
    assert len(paras) > 10
    assert all(len(p) <= fragments.MAX_PARAGRAPH_CHARS for p in paras)


def test_extract_on_a_line_only_page_returns_fragments_not_the_page():
    result = fragments.extract(TABLE_PAGE, "needle", k=3, neighbours=1)
    joined = "".join(f["text"] for f in result)
    assert "needle" in joined
    assert len(joined) < len(TABLE_PAGE) / 5


def test_no_single_fragment_exceeds_the_paragraph_cap_times_the_window():
    result = fragments.extract(TABLE_PAGE, "needle", k=5, neighbours=1)
    window = fragments.MAX_PARAGRAPH_CHARS * 3
    assert all(len(f["text"]) <= window for f in result)


def test_a_single_unbroken_line_is_still_capped():
    """No newlines at all — the last resort is a hard character cut."""
    text = ("filler " * 500) + "needle " + ("filler " * 500)
    paras = fragments.split_paragraphs(text)
    assert len(paras) > 1
    assert all(len(p) <= fragments.MAX_PARAGRAPH_CHARS for p in paras)


def test_splitting_long_paragraphs_keeps_spans_exact():
    """Fragments stay re-anchorable: every span must slice the original verbatim."""
    for text in (TABLE_PAGE, PAGE, ("filler " * 500) + "needle"):
        for para, start, end in fragments.split_paragraphs_with_spans(text):
            assert text[start:end] == para


def test_normal_pages_are_not_re_split():
    """The cap must not change behaviour on ordinary prose."""
    assert fragments.split_paragraphs(PAGE) == [
        p.strip() for p in PAGE.split("\n\n") if p.strip()
    ]


def test_unstructured_pages_are_flagged():
    assert fragments.looks_unstructured(TABLE_PAGE) is True
    assert fragments.looks_unstructured(PAGE) is False


# --- reading the foreign free-search-mcp cache (read-only) -------------------


@pytest.fixture
def foreign_cache(tmp_path):
    path = tmp_path / "cache.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE pages (url TEXT PRIMARY KEY, title TEXT, content TEXT NOT NULL,"
        " fetched INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO pages VALUES (?,?,?,?)",
        (
            "https://example.com/rrf",
            '\x01META\x01{"title": "RRF explained", "author": "X"}\x01',
            PAGE,
            1753500000,
        ),
    )
    conn.execute(
        "INSERT INTO pages VALUES (?,?,?,?)",
        ("https://example.com/plain", "Plain title", "body", 1753500000),
    )
    conn.commit()
    conn.close()
    return path


def test_cached_page_strips_the_meta_prefix_from_title(foreign_cache):
    page = fragments.cached_page(foreign_cache, "https://example.com/rrf")
    assert page["title"] == "RRF explained"
    assert page["content"] == PAGE
    assert page["fetched_at"] == 1753500000


def test_cached_page_keeps_a_plain_title(foreign_cache):
    assert (
        fragments.cached_page(foreign_cache, "https://example.com/plain")["title"]
        == "Plain title"
    )


def test_cached_page_returns_none_for_a_miss(foreign_cache):
    assert fragments.cached_page(foreign_cache, "https://example.com/nope") is None


def test_cached_page_returns_none_when_cache_file_is_absent(tmp_path):
    assert (
        fragments.cached_page(tmp_path / "no-such.sqlite", "https://example.com")
        is None
    )


def test_cached_page_reads_a_write_protected_cache(foreign_cache):
    """The cache belongs to free-search-mcp; we must never need write access to it."""
    foreign_cache.chmod(0o444)
    try:
        assert (
            fragments.cached_page(foreign_cache, "https://example.com/rrf") is not None
        )
    finally:
        foreign_cache.chmod(0o644)


def test_fragment_carries_offsets_and_context():
    text = "alpha one\n\nbeta two\n\ngamma three"
    result = fragments.extract(text, "beta", k=1, neighbours=0)
    frag = result[0]
    assert text[frag["char_start"] : frag["char_end"]] == frag["text"]
    assert frag["prefix"].endswith("alpha one\n\n")
    assert frag["suffix"].startswith("\n\ngamma three")


def test_context_is_capped():
    text = ("x" * 200) + "\n\nneedle here\n\n" + ("y" * 200)
    frag = fragments.extract(text, "needle", k=1, neighbours=0)[0]
    assert len(frag["prefix"]) == fragments.ANCHOR_CONTEXT
    assert len(frag["suffix"]) == fragments.ANCHOR_CONTEXT


# --- cutting a single oversized line ----------------------------------------
#
# A table page arrives as one enormous line of pipe-separated cells. Slicing it
# every MAX_PARAGRAPH_CHARS lands mid-cell and mid-word: measured on the FRAMES
# corpus reshaped into that form, the server handed back 56% of the page.


def test_a_long_line_of_cells_is_cut_on_cell_boundaries():
    row = "| " + " | ".join(f"cell number {i} with some filler text" for i in range(200)) + " |"
    pieces = [piece for piece, _, _ in fragments.split_paragraphs_with_spans(row)]
    assert len(pieces) > 1
    assert all("|" not in piece.strip("| ") for piece in pieces)


def test_cell_boundary_pieces_keep_exact_spans():
    row = "| " + " | ".join(f"cell number {i} with some filler text" for i in range(200)) + " |"
    for piece, start, end in fragments.split_paragraphs_with_spans(row):
        assert row[start:end] == piece


def test_a_long_line_without_cells_is_cut_on_whitespace_not_mid_word():
    line = " ".join(f"word{i}" for i in range(2000))
    pieces = [piece for piece, _, _ in fragments.split_paragraphs_with_spans(line)]
    assert len(pieces) > 1
    assert all(piece.split()[-1].startswith("word") for piece in pieces)
    assert all(piece == piece.strip() for piece in pieces)


def test_a_long_line_with_no_separator_at_all_is_still_cut_to_size():
    line = "x" * (fragments.MAX_PARAGRAPH_CHARS * 3)
    pieces = [piece for piece, _, _ in fragments.split_paragraphs_with_spans(line)]
    assert len(pieces) == 3
    assert all(len(piece) <= fragments.MAX_PARAGRAPH_CHARS for piece in pieces)


def test_every_piece_stays_within_the_cap():
    row = "| " + " | ".join("a very long cell " * 20 for _ in range(50)) + " |"
    for piece, _, _ in fragments.split_paragraphs_with_spans(row):
        assert len(piece) <= fragments.MAX_PARAGRAPH_CHARS
