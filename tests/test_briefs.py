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
                "quote": "The constant k defaults to 60",
                "binding": "timeless",
                "source_class": "primary",
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
                "quote": "The constant k defaults to 42",
                "binding": "timeless",
                "source_class": "primary",
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
                "quote": "The constant k defaults to 6O",
                "binding": "timeless",
                "source_class": "primary",
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
                "quote": "The  constant K  defaults\nto 60",
                "binding": "timeless",
                "source_class": "primary",
            }
        ],
    )
    assert problems == []


def test_unknown_fragment_is_rejected(conn):
    problems = briefs.validate_claims(
        conn,
        [
            {
                "text": "x",
                "kind": "fact",
                "fragment_id": "0" * 16,
                "quote": "five whole words right here",
                "binding": "timeless",
                "source_class": "primary",
            }
        ],
    )
    assert problems[0]["reason"] == "unknown_fragment"


def test_a_fact_without_a_citation_is_rejected(conn):
    problems = briefs.validate_claims(conn, [{"text": "x", "kind": "fact"}])
    assert problems[0]["reason"] == "missing_citation"


def test_an_assumption_needs_no_citation(conn):
    assert briefs.validate_claims(conn, [{"text": "probably x", "kind": "assumption"}]) == []


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
            {
                "text": "k is 60",
                "kind": "fact",
                "fragment_id": fid,
                "quote": "The constant k defaults to 60",
                "binding": "timeless",
                "source_class": "primary",
            }
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


# --- elided quotes ----------------------------------------------------------
#
# Skipping the middle of a long quote is normal citation practice, and the eval
# (eval/RESULTS.md) showed it was refused in 479 of 500 cases. Every piece must
# still be verbatim, and the pieces must appear in the fragment in the order
# they are written — that is what keeps foreign wording out.


def test_a_quote_with_an_elided_middle_passes(conn, fid):
    problems = briefs.validate_claims(
        conn,
        [
            {
                "text": "k is 60",
                "kind": "fact",
                "fragment_id": fid,
                "quote": "The constant k … in Elasticsearch.",
                "binding": "timeless",
                "source_class": "primary",
            }
        ],
    )
    assert problems == []


def test_three_dots_work_the_same_as_an_ellipsis(conn, fid):
    problems = briefs.validate_claims(
        conn,
        [
            {
                "text": "k is 60",
                "kind": "fact",
                "fragment_id": fid,
                "quote": "The constant k ... in Elasticsearch.",
                "binding": "timeless",
                "source_class": "primary",
            }
        ],
    )
    assert problems == []


def test_elided_pieces_out_of_order_are_rejected(conn, fid):
    problems = briefs.validate_claims(
        conn,
        [
            {
                "text": "k is 60",
                "kind": "fact",
                "fragment_id": fid,
                "quote": "in Elasticsearch … The constant k",
                "binding": "timeless",
                "source_class": "primary",
            }
        ],
    )
    assert problems[0]["reason"] == "quote_not_found"


def test_an_elided_quote_with_one_invented_piece_is_rejected(conn, fid):
    problems = briefs.validate_claims(
        conn,
        [
            {
                "text": "k is 42",
                "kind": "fact",
                "fragment_id": fid,
                "quote": "The constant k … in Solr.",
                "binding": "timeless",
                "source_class": "primary",
            }
        ],
    )
    assert problems[0]["reason"] == "quote_not_found"


def test_pieces_may_not_overlap_the_same_words_twice(conn, fid):
    problems = briefs.validate_claims(
        conn,
        [
            {
                "text": "k is 60",
                "kind": "fact",
                "fragment_id": fid,
                "quote": "defaults to 60 … defaults to 60",
                "binding": "timeless",
                "source_class": "primary",
            }
        ],
    )
    assert problems[0]["reason"] == "quote_not_found"


def test_a_bare_ellipsis_is_not_a_quote(conn, fid):
    """Nothing between the dots is nothing to check — refused as too short."""
    problems = briefs.validate_claims(
        conn, [{"text": "k is 60", "kind": "fact", "fragment_id": fid, "quote": " … "}]
    )
    assert problems[0]["reason"] == "quote_too_short"
    assert problems[0]["words"] == 0


def test_a_quote_that_is_only_leading_ellipsis_still_has_to_match(conn, fid):
    problems = briefs.validate_claims(
        conn,
        [
            {
                "text": "k is 60",
                "kind": "fact",
                "fragment_id": fid,
                "quote": "… constant k defaults to 60 …",
                "binding": "timeless",
                "source_class": "primary",
            }
        ],
    )
    assert problems == []


# --- the length floor -------------------------------------------------------
#
# A four-word verbatim quote passed 100% of the time and supported nothing
# (eval/RESULTS.md). Five words is where ambiguity and honest cost cross on the
# measured corpus: 4.2% of openings still occur in another fragment, 4.2% of
# real sentences are shorter than the floor.


def test_a_quote_shorter_than_the_floor_is_rejected(conn, fid):
    problems = briefs.validate_claims(
        conn,
        [
            {
                "text": "k is 60",
                "kind": "fact",
                "fragment_id": fid,
                "quote": "The constant k defaults",
                "binding": "timeless",
                "source_class": "primary",
            }
        ],
    )
    assert problems[0]["reason"] == "quote_too_short"
    assert problems[0]["words"] == 4


def test_a_quote_exactly_at_the_floor_passes(conn, fid):
    problems = briefs.validate_claims(
        conn,
        [
            {
                "text": "k is 60",
                "kind": "fact",
                "fragment_id": fid,
                "quote": "The constant k defaults to",
                "binding": "timeless",
                "source_class": "primary",
            }
        ],
    )
    assert problems == []


def test_the_floor_counts_words_across_an_ellipsis(conn, fid):
    problems = briefs.validate_claims(
        conn,
        [
            {
                "text": "k is 60",
                "kind": "fact",
                "fragment_id": fid,
                "quote": "The constant k … to 60",
                "binding": "timeless",
                "source_class": "primary",
            }
        ],
    )
    assert problems == []


def test_a_too_short_quote_is_reported_before_it_is_matched(conn, fid):
    """Length is cheap to check and the clearer complaint — it wins."""
    problems = briefs.validate_claims(
        conn,
        [
            {
                "text": "k is 42",
                "kind": "fact",
                "fragment_id": fid,
                "quote": "never on the page",
                "binding": "timeless",
                "source_class": "primary",
            }
        ],
    )
    assert problems[0]["reason"] == "quote_too_short"


# --- binding: an expiry date on the fact, not on the brief -------------------
#
# A six-week-old brief can already lie: "the reigning world champion is
# Argentina" died on 19.07.2026 while every quote in it stayed verbatim
# (CLAUDE.md backlog, 27.07). So a fact carries what it is bound to, and the
# horizon of that binding decides when it has to be rechecked.


def _fact(fid, **extra):
    return {
        "text": "k is 60",
        "kind": "fact",
        "fragment_id": fid,
        "quote": "The constant k defaults to 60",
        "binding": "timeless",
        "source_class": "primary",
        **extra,
    }


def test_a_fact_carrying_a_binding_and_a_source_class_passes(conn, fid):
    assert briefs.validate_claims(conn, [_fact(fid)]) == []


def test_a_fact_without_a_binding_is_a_problem(conn, fid):
    claim = _fact(fid)
    del claim["binding"]
    problems = briefs.validate_claims(conn, [claim])
    assert problems[0]["reason"] == "missing_binding"


def test_a_binding_outside_the_enum_is_a_problem(conn, fid):
    problems = briefs.validate_claims(conn, [_fact(fid, binding="eternal")])
    assert problems[0]["reason"] == "unknown_binding"


def test_a_dated_binding_needs_to_say_what_it_is_bound_to(conn, fid):
    problems = briefs.validate_claims(conn, [_fact(fid, binding="dataset")])
    assert problems[0]["reason"] == "missing_bound_to"


def test_a_dated_binding_with_bound_to_passes(conn, fid):
    claim = _fact(fid, binding="dataset", bound_to="jina-embeddings-v2-small on Quora")
    assert briefs.validate_claims(conn, [claim]) == []


def test_a_timeless_fact_bound_to_something_is_a_contradiction(conn, fid):
    problems = briefs.validate_claims(conn, [_fact(fid, bound_to="Anthropic, July 2026")])
    assert problems[0]["reason"] == "bound_to_on_timeless"


def test_an_unparsable_recheck_date_is_a_problem(conn, fid):
    claim = _fact(fid, binding="world", bound_to="the leaderboard", recheck_after="soon")
    problems = briefs.validate_claims(conn, [claim])
    assert problems[0]["reason"] == "bad_recheck_after"


def test_a_recheck_date_on_a_timeless_fact_is_a_problem(conn, fid):
    problems = briefs.validate_claims(conn, [_fact(fid, recheck_after="2027-01-01")])
    assert problems[0]["reason"] == "recheck_after_on_timeless"


def test_an_assumption_needs_no_binding_but_a_bad_one_still_fails(conn):
    assert briefs.validate_claims(conn, [{"text": "x", "kind": "assumption"}]) == []
    problems = briefs.validate_claims(
        conn, [{"text": "x", "kind": "assumption", "binding": "forever"}]
    )
    assert problems[0]["reason"] == "unknown_binding"


def test_default_recheck_dates_follow_the_binding_horizon():
    assert briefs.default_recheck_after("timeless", "2026-07-30") is None
    assert briefs.default_recheck_after("dataset", "2026-07-30") == "2027-07-30"
    assert briefs.default_recheck_after("vendor", "2026-07-30") == "2026-10-28"
    assert briefs.default_recheck_after("world", "2026-07-30") == "2026-08-29"


def test_save_fills_in_the_default_recheck_date(conn, fid, tmp_path):
    briefs.save(
        conn,
        job_id="j",
        topic="T",
        summary="s",
        claims=[_fact(fid, binding="world", bound_to="the leaderboard")],
        gaps=[],
        brief_dir=tmp_path,
        today="2026-07-30",
    )
    row = conn.execute("SELECT binding, bound_to, recheck_after FROM brief_claims").fetchone()
    assert row["binding"] == "world"
    assert row["bound_to"] == "the leaderboard"
    assert row["recheck_after"] == "2026-08-29"


def test_an_explicit_recheck_date_wins_over_the_default(conn, fid, tmp_path):
    briefs.save(
        conn,
        job_id="j",
        topic="T",
        summary="s",
        claims=[_fact(fid, binding="vendor", bound_to="Anthropic", recheck_after="2026-08-01")],
        gaps=[],
        brief_dir=tmp_path,
        today="2026-07-30",
    )
    row = conn.execute("SELECT recheck_after FROM brief_claims").fetchone()
    assert row["recheck_after"] == "2026-08-01"


def test_the_migration_is_idempotent_and_keeps_old_rows(conn, fid, tmp_path):
    """Petr's live DB already holds briefs — the columns are added, not recreated."""
    briefs.save(
        conn,
        job_id="j",
        topic="T",
        summary="s",
        claims=[_fact(fid)],
        gaps=[],
        brief_dir=tmp_path,
        today="2026-07-30",
    )
    briefs.init_schema(conn)
    briefs.init_schema(conn)
    assert conn.execute("SELECT count(*) AS n FROM brief_claims").fetchone()["n"] == 1


_OLD_CLAIMS_DDL = """
CREATE TABLE brief_claims (
    brief_id    TEXT NOT NULL,
    idx         INTEGER NOT NULL,
    text        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    fragment_id TEXT,
    quote       TEXT,
    PRIMARY KEY (brief_id, idx)
);
"""


def test_the_migration_adds_the_columns_to_a_pre_existing_table(tmp_path):
    """Petr's live DB has three briefs under the old DDL — the ALTER path is the real one."""
    c = db.connect(tmp_path / "old.sqlite")
    c.executescript(_OLD_CLAIMS_DDL)
    c.execute(
        "INSERT INTO brief_claims (brief_id, idx, text, kind, fragment_id, quote)"
        " VALUES ('b1', 0, 'k is 60', 'fact', 'abc', 'the constant k defaults to 60')"
    )
    c.commit()

    briefs.init_schema(c)

    columns = {row["name"] for row in c.execute("PRAGMA table_info(brief_claims)")}
    assert {"binding", "bound_to", "recheck_after", "source_class"} <= columns
    row = c.execute("SELECT text, binding FROM brief_claims").fetchone()
    assert row["text"] == "k is 60"
    assert row["binding"] is None
    assert briefs.freshness(c, "b1") == {
        "binding_mix": {},
        "unlabelled": 1,
        "recheck_total": 0,
        "recheck_due": 0,
    }


def test_render_shows_the_binding_and_the_recheck_date(conn, fid, tmp_path):
    briefs.save(
        conn,
        job_id="j",
        topic="T",
        summary="s",
        claims=[_fact(fid, binding="dataset", bound_to="jina-embeddings-v2-small on Quora")],
        gaps=[],
        brief_dir=tmp_path,
        today="2026-07-30",
    )
    body = (tmp_path / "2026-07-30-t.md").read_text()
    assert "jina-embeddings-v2-small on Quora" in body
    assert "2027-07-30" in body
    assert "## Freshness" in body


def test_the_freshness_section_is_sorted_by_date(conn, fid, tmp_path):
    briefs.save(
        conn,
        job_id="j",
        topic="T",
        summary="s",
        claims=[
            _fact(fid, text="late", binding="dataset", bound_to="a dataset"),
            _fact(fid, text="early", binding="world", bound_to="the leaderboard"),
        ],
        gaps=[],
        brief_dir=tmp_path,
        today="2026-07-30",
    )
    body = (tmp_path / "2026-07-30-t.md").read_text()
    freshness = body.split("## Freshness", 1)[1]
    assert freshness.index("2026-08-29") < freshness.index("2027-07-30")


# --- source quality: warn, never refuse -------------------------------------
#
# Origin is checkable, truth is not: a scholarly-sounding retelling quoted
# verbatim passes every check this server has (CLAUDE.md backlog, 27.07). What
# is mechanically countable is the class of the source and the number of
# distinct domains under a fact — so those are counted and reported, and the
# brief is still saved.


def test_a_fact_without_a_source_class_is_a_problem(conn, fid):
    claim = _fact(fid)
    del claim["source_class"]
    problems = briefs.validate_claims(conn, [claim])
    assert problems[0]["reason"] == "missing_source_class"


def test_a_source_class_outside_the_enum_is_a_problem(conn, fid):
    problems = briefs.validate_claims(conn, [_fact(fid, source_class="rumour")])
    assert problems[0]["reason"] == "unknown_source_class"


def test_a_retelling_warns_but_does_not_refuse(conn, fid, tmp_path):
    result = briefs.save(
        conn,
        job_id="j",
        topic="T",
        summary="s",
        claims=[_fact(fid, source_class="secondary")],
        gaps=[],
        brief_dir=tmp_path,
        today="2026-07-30",
    )
    reasons = [w["reason"] for w in result["warnings"]]
    assert "secondary_only" in reasons
    assert (tmp_path / "2026-07-30-t.md").exists()


def test_render_marks_a_retelling_in_the_file(conn, fid, tmp_path):
    briefs.save(
        conn,
        job_id="j",
        topic="T",
        summary="s",
        claims=[_fact(fid, source_class="secondary")],
        gaps=[],
        brief_dir=tmp_path,
        today="2026-07-30",
    )
    body = (tmp_path / "2026-07-30-t.md").read_text().lower()
    assert "retelling" in body
    assert "primary source" in body


def _second_fragment(conn, url, char_start=99):
    return issued.record(
        conn,
        url=url,
        fetched_at=1753500000,
        fragment={
            "text": "The constant k defaults to 60 elsewhere too.",
            "char_start": char_start,
            "char_end": char_start + 44,
            "prefix": "",
            "suffix": "",
        },
    )["fragment_id"]


def test_a_fact_no_other_fact_confirms_is_warned_about(conn, fid):
    warnings = briefs.source_warnings(conn, [_fact(fid)])
    assert [w["reason"] for w in warnings] == ["unique_domain"]
    assert warnings[0]["domain"] == "e.com"


def test_two_facts_from_the_same_domain_are_not_unique_domains(conn, fid):
    second = _second_fragment(conn, "https://www.e.com/b")
    reasons = [w["reason"] for w in briefs.source_warnings(conn, [_fact(fid), _fact(second)])]
    assert "unique_domain" not in reasons


def test_a_brief_built_entirely_off_one_site_is_warned_about(conn, fid):
    """The case the backlog was about: three facts, one source wearing three hats."""
    second = _second_fragment(conn, "https://www.e.com/b")
    third = _second_fragment(conn, "https://e.com/c", char_start=300)
    warnings = briefs.source_warnings(conn, [_fact(fid), _fact(second), _fact(third)])
    assert [w["reason"] for w in warnings] == ["single_domain_brief"]
    assert warnings[0]["index"] is None
    assert warnings[0]["domain"] == "e.com"
    assert warnings[0]["facts"] == 3


def test_two_facts_on_two_domains_are_each_unconfirmed_but_not_a_single_source(conn, fid):
    """Two facts, two sites: neither is cross-confirmed, but the brief is not one source."""
    second = _second_fragment(conn, "https://other.example/b")
    reasons = [w["reason"] for w in briefs.source_warnings(conn, [_fact(fid), _fact(second)])]
    assert reasons == ["unique_domain", "unique_domain"]


def test_an_assumption_from_the_same_domain_does_not_mute_the_signal(conn, fid):
    """An assumption is not independent confirmation, so it must not count."""
    warnings = briefs.source_warnings(
        conn, [_fact(fid), {"text": "maybe", "kind": "assumption", "fragment_id": fid}]
    )
    assert [w["reason"] for w in warnings] == ["unique_domain"]


def test_a_url_without_a_scheme_still_counts_as_its_domain(conn):
    bare = _second_fragment(conn, "e.com/no-scheme")
    warnings = briefs.source_warnings(conn, [_fact(bare)])
    assert warnings[0]["domain"] == "e.com"


def test_a_schemeless_url_with_a_port_counts_as_its_host_not_its_port(conn):
    """`urlsplit` reads "e.com:8080/a" as scheme "e.com" — the port must not become
    the domain, or the same site on two ports looks like two independent sources."""
    ported = _second_fragment(conn, "e.com:8080/a")
    warnings = briefs.source_warnings(conn, [_fact(ported)])
    assert warnings[0]["domain"] == "e.com"


def test_source_warnings_ignore_assumptions(conn):
    assert briefs.source_warnings(conn, [{"text": "x", "kind": "assumption"}]) == []


# --- stored dates are normalised, because freshness compares strings ---------


def test_a_date_without_dashes_is_stored_normalised(conn, fid, tmp_path):
    """`"20260730" <= "2026-07-30"` is False — an unnormalised date never comes due."""
    briefs.save(
        conn,
        job_id="j",
        topic="T",
        summary="s",
        claims=[_fact(fid, binding="world", bound_to="x", recheck_after="20200101")],
        gaps=[],
        brief_dir=tmp_path,
        today="2026-07-30",
    )
    assert conn.execute("SELECT recheck_after FROM brief_claims").fetchone()[0] == "2020-01-01"


def test_a_padded_date_is_stored_stripped(conn, fid, tmp_path):
    briefs.save(
        conn,
        job_id="j",
        topic="T",
        summary="s",
        claims=[_fact(fid, binding="world", bound_to="x", recheck_after=" 2026-08-01 ")],
        gaps=[],
        brief_dir=tmp_path,
        today="2026-07-30",
    )
    assert conn.execute("SELECT recheck_after FROM brief_claims").fetchone()[0] == "2026-08-01"


def test_a_bad_date_on_an_unbound_assumption_is_still_refused(conn):
    problems = briefs.validate_claims(
        conn, [{"text": "x", "kind": "assumption", "recheck_after": "next week"}]
    )
    assert problems[0]["reason"] == "bad_recheck_after"


def test_a_labelled_assumption_is_visible_in_the_file(conn, tmp_path):
    briefs.save(
        conn,
        job_id="j",
        topic="T",
        summary="s",
        claims=[
            {
                "text": "prices will keep falling",
                "kind": "assumption",
                "binding": "vendor",
                "bound_to": "Anthropic, July 2026",
            }
        ],
        gaps=[],
        brief_dir=tmp_path,
        today="2026-07-30",
    )
    body = (tmp_path / "2026-07-30-t.md").read_text()
    assert "Anthropic, July 2026" in body


# --- freshness in search hits ----------------------------------------------


def test_search_reports_how_many_facts_are_already_due(conn, fid, tmp_path):
    briefs.save(
        conn,
        job_id="j",
        topic="Reciprocal rank fusion",
        summary="s",
        claims=[
            _fact(fid, binding="world", bound_to="the leaderboard", recheck_after="2020-01-01"),
            _fact(fid, text="timeless one"),
        ],
        gaps=[],
        brief_dir=tmp_path,
        today="2026-07-30",
    )
    hit = briefs.search(conn, "rank fusion")[0]
    assert hit["recheck_total"] == 1
    assert hit["recheck_due"] == 1
    assert hit["binding_mix"] == {"world": 1, "timeless": 1}
    assert hit["unlabelled"] == 0


def test_search_treats_pre_migration_briefs_as_unlabelled(conn, fid, tmp_path):
    briefs.save(
        conn,
        job_id="j",
        topic="Reciprocal rank fusion",
        summary="s",
        claims=[_fact(fid)],
        gaps=[],
        brief_dir=tmp_path,
        today="2026-07-30",
    )
    conn.execute("UPDATE brief_claims SET binding = NULL, recheck_after = NULL")
    conn.commit()
    hit = briefs.search(conn, "rank fusion")[0]
    assert hit["unlabelled"] == 1
    assert hit["binding_mix"] == {}
    assert hit["recheck_total"] == 0
    assert hit["recheck_due"] == 0
