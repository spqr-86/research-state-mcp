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


def test_save_writes_a_markdown_file_and_returns_its_path(conn, fid, tmp_path):
    result = briefs.save(
        conn,
        job_id="job1",
        topic="How does RRF work",
        summary="RRF fuses ranked lists.",
        claims=[
            {"text": "k is 60", "kind": "fact", "fragment_id": fid, "quote": "k defaults to 60"}
        ],
        gaps=["nothing on Russian-language sources"],
        brief_dir=tmp_path,
        today="2026-07-26",
    )
    path = tmp_path / "2026-07-26-how-does-rrf-work.md"
    assert result["path"] == str(path)
    body = path.read_text()
    assert "RRF fuses ranked lists." in body
    assert "k is 60" in body
    assert "https://e.com/a" in body
    assert "nothing on Russian-language sources" in body


def test_assumptions_are_labelled_in_the_file(conn, tmp_path):
    briefs.save(
        conn,
        job_id="j",
        topic="T",
        summary="s",
        claims=[{"text": "likely true", "kind": "assumption"}],
        gaps=[],
        brief_dir=tmp_path,
        today="2026-07-26",
    )
    body = (tmp_path / "2026-07-26-t.md").read_text()
    assert "assumption" in body.lower()


def test_save_refuses_an_invalid_claim_and_writes_nothing(conn, tmp_path):
    with pytest.raises(briefs.InvalidBrief) as exc:
        briefs.save(
            conn,
            job_id="j",
            topic="T",
            summary="s",
            claims=[{"text": "x", "kind": "fact"}],
            gaps=[],
            brief_dir=tmp_path / "briefs",
            today="2026-07-26",
        )
    assert exc.value.problems[0]["reason"] == "missing_citation"
    assert not (tmp_path / "briefs").exists()


def test_saving_the_same_topic_twice_does_not_overwrite(conn, tmp_path):
    for _ in range(2):
        briefs.save(
            conn,
            job_id="j",
            topic="T",
            summary="s",
            claims=[],
            gaps=[],
            brief_dir=tmp_path / "briefs",
            today="2026-07-26",
        )
    assert len(list((tmp_path / "briefs").iterdir())) == 2


def test_a_russian_topic_still_produces_a_usable_filename(conn, tmp_path):
    result = briefs.save(
        conn,
        job_id="j",
        topic="Трудовой кодекс: сроки",
        summary="s",
        claims=[],
        gaps=[],
        brief_dir=tmp_path,
        today="2026-07-26",
    )
    assert result["path"].endswith(".md")
    assert "трудовой-кодекс-сроки" in result["path"]


def test_search_finds_a_brief_by_topic(conn, tmp_path):
    briefs.save(
        conn,
        job_id="j",
        topic="Reciprocal rank fusion",
        summary="RRF fuses lists.",
        claims=[],
        gaps=[],
        brief_dir=tmp_path,
        today="2026-07-26",
    )
    hits = briefs.search(conn, "rank fusion")
    assert len(hits) == 1
    assert hits[0]["topic"] == "Reciprocal rank fusion"
    assert hits[0]["path"].endswith(".md")
    assert hits[0]["age_days"] == 0


def test_search_returns_nothing_for_an_unrelated_query(conn, tmp_path):
    briefs.save(
        conn,
        job_id="j",
        topic="Reciprocal rank fusion",
        summary="s",
        claims=[],
        gaps=[],
        brief_dir=tmp_path,
        today="2026-07-26",
    )
    assert briefs.search(conn, "квантовая хромодинамика") == []


def test_search_survives_operator_characters(conn, tmp_path):
    briefs.save(
        conn,
        job_id="j",
        topic="RRF",
        summary="s",
        claims=[],
        gaps=[],
        brief_dir=tmp_path,
        today="2026-07-26",
    )
    assert briefs.search(conn, 'NEAR "AND" (rrf) *') is not None


def test_search_never_returns_the_brief_body(conn, tmp_path):
    briefs.save(
        conn,
        job_id="j",
        topic="RRF",
        summary="s" * 5000,
        claims=[],
        gaps=[],
        brief_dir=tmp_path,
        today="2026-07-26",
    )
    hit = briefs.search(conn, "rrf")[0]
    assert len(hit["snippet"]) < 500
    assert "body" not in hit
