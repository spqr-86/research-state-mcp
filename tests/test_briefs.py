import pytest

from research_state import briefs, db, issued


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "state.sqlite")
    issued.init_schema(c)
    briefs.init_schema(c)
    return c


@pytest.fixture
def fid(conn):
    return issued.record(
        conn,
        url="https://e.com/a",
        fetched_at=1753500000,
        fragment={
            "text": "The constant k defaults to 60 in Elasticsearch.",
            "char_start": 10,
            "char_end": 56,
            "prefix": "",
            "suffix": "",
        },
    )["fragment_id"]


def test_a_fact_with_a_verbatim_quote_passes(conn, fid):
    problems = briefs.validate_claims(
        conn,
        [
            {
                "text": "k is 60",
                "kind": "fact",
                "fragment_id": fid,
                "quote": "k defaults to 60",
            }
        ],
    )
    assert problems == []


def test_a_quote_absent_from_the_fragment_is_rejected(conn, fid):
    problems = briefs.validate_claims(
        conn,
        [
            {
                "text": "k is 42",
                "kind": "fact",
                "fragment_id": fid,
                "quote": "k defaults to 42",
            }
        ],
    )
    assert problems[0]["reason"] == "quote_not_found"


def test_one_character_off_is_still_rejected(conn, fid):
    problems = briefs.validate_claims(
        conn,
        [
            {
                "text": "k is 60",
                "kind": "fact",
                "fragment_id": fid,
                "quote": "k defaults to 6O",
            }
        ],
    )
    assert problems[0]["reason"] == "quote_not_found"


def test_whitespace_and_case_differences_are_tolerated(conn, fid):
    problems = briefs.validate_claims(
        conn,
        [
            {
                "text": "k is 60",
                "kind": "fact",
                "fragment_id": fid,
                "quote": "K  defaults\nto 60",
            }
        ],
    )
    assert problems == []


def test_unknown_fragment_is_rejected(conn):
    problems = briefs.validate_claims(
        conn, [{"text": "x", "kind": "fact", "fragment_id": "0" * 16, "quote": "x"}]
    )
    assert problems[0]["reason"] == "unknown_fragment"


def test_a_fact_without_a_citation_is_rejected(conn):
    problems = briefs.validate_claims(conn, [{"text": "x", "kind": "fact"}])
    assert problems[0]["reason"] == "missing_citation"


def test_an_assumption_needs_no_citation(conn):
    assert (
        briefs.validate_claims(conn, [{"text": "probably x", "kind": "assumption"}])
        == []
    )


def test_an_unknown_kind_is_rejected(conn):
    problems = briefs.validate_claims(conn, [{"text": "x", "kind": "guess"}])
    assert problems[0]["reason"] == "unknown_kind"


def test_every_bad_claim_is_reported_not_just_the_first(conn):
    problems = briefs.validate_claims(
        conn, [{"text": "a", "kind": "fact"}, {"text": "b", "kind": "fact"}]
    )
    assert len(problems) == 2
    assert [p["index"] for p in problems] == [0, 1]
