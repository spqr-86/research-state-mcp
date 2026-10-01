"""Tests for the regression scorer — an in-memory state db, no network."""

from __future__ import annotations

import json
import sqlite3

import pytest
import regression as rg

# --- anchors ------------------------------------------------------------------


def test_anchor_hits_when_any_alternative_matches_ignoring_case():
    anchors = [{"desc": "a", "any": ["warner music", "nothing"]}]
    assert rg.anchor_hits("Parent: Warner Music Group", anchors) == [True]


def test_anchor_misses_when_no_alternative_matches():
    anchors = [{"desc": "a", "any": ["Hastings"]}, {"desc": "b", "any": ["France"]}]
    assert rg.anchor_hits("France won in 2018", anchors) == [False, True]


# --- gate ---------------------------------------------------------------------


def test_gate_counts_answers_with_for_against_tally_and_against_url():
    answers = [
        "Да. за 3 / против 1, против: https://example.org/critique",
        "Да. За 2 / против 0 — искал «X limitations», ничего",
        "Просто ответ без счёта",
    ]
    got = rg.gate_metrics(answers)
    assert got == {"subq": 3, "tallied": 2, "against_url": 1, "against_zero": 1}


def test_gate_accepts_sources_listed_between_the_for_count_and_the_slash():
    answers = ["Итог. За 3 (a.ru, b.ru, vk) / против 2 — https://c.ru/x, https://d.ru"]
    assert rg.gate_metrics(answers) == {
        "subq": 1,
        "tallied": 1,
        "against_url": 1,
        "against_zero": 0,
    }


@pytest.mark.parametrize(
    "answer",
    [
        "За (позитив) 3 / против 3 — https://o.ru/r",
        "За «цена выше рынка» 3 (Яндекс, ЦИАН) / против 2 — https://c.ru",
        "Контраргументы есть. За риски 4 / против 2 — https://r.ru",
    ],
)
def test_gate_accepts_a_label_between_for_and_its_count(answer):
    assert rg.gate_metrics([answer])["tallied"] == 1


def test_gate_on_no_subquestions_is_all_zero():
    assert rg.gate_metrics([]) == {"subq": 0, "tallied": 0, "against_url": 0, "against_zero": 0}


# --- gaps ---------------------------------------------------------------------


def test_gap_count_reads_bullets_of_the_gaps_section_only():
    text = "## Claims\n\n- c1\n\n## Gaps\n\n- g1\n- g2\n\n## Sources\n\n- <https://a.org>\n"
    assert rg.gap_count(text) == 2


def test_gap_count_is_zero_without_a_gaps_section():
    assert rg.gap_count("# Title\n\n- just a bullet\n") == 0


# --- topic matching -----------------------------------------------------------


def test_same_topic_ignores_case_whitespace_and_trailing_punctuation():
    assert rg.same_topic("Как  устроены агенты?", "как устроены агенты")


def test_same_topic_rejects_a_different_question():
    assert not rg.same_topic("RAG vs agentic search", "Google Agentic RAG")


# --- db scoring ---------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE jobs (job_id TEXT PRIMARY KEY, topic TEXT, status TEXT, created INTEGER);
        CREATE TABLE subquestions (job_id TEXT, subq_id INTEGER, text TEXT, norm TEXT,
            status TEXT, answer TEXT, closed_at INTEGER);
        CREATE TABLE issued_fragments (fragment_id TEXT PRIMARY KEY, url TEXT);
        CREATE TABLE briefs (brief_id TEXT PRIMARY KEY, job_id TEXT, topic TEXT,
            summary TEXT, path TEXT, created INTEGER);
        CREATE TABLE brief_claims (brief_id TEXT, idx INTEGER, text TEXT, kind TEXT,
            fragment_id TEXT, quote TEXT, source_class TEXT);
        """
    )
    brief = tmp_path / "b.md"
    brief.write_text("# T\n\nWarner Music\n\n## Gaps\n\n- g1\n", encoding="utf-8")
    conn.execute("INSERT INTO jobs VALUES ('j1', 'Topic one', 'done', 100)")
    conn.executemany(
        "INSERT INTO subquestions VALUES ('j1', ?, 'q', 'q', 'done', ?, 1)",
        [(1, "за 2 / против 1 https://x.org/c"), (2, "без счёта")],
    )
    conn.executemany(
        "INSERT INTO issued_fragments VALUES (?, ?)",
        [("f1", "https://a.org/p"), ("f2", "https://www.a.org/q"), ("f3", "https://b.org/r")],
    )
    conn.execute("INSERT INTO briefs VALUES ('b1', 'j1', 'Topic one', 's', ?, 200)", (str(brief),))
    conn.executemany(
        "INSERT INTO brief_claims VALUES ('b1', ?, 't', ?, ?, 'q', ?)",
        [
            (0, "fact", "f1", "primary"),
            (1, "fact", "f2", "vendor"),
            (2, "fact", "f3", "secondary"),
            (3, "assumption", None, None),
        ],
    )
    return conn


def test_latest_run_finds_the_brief_by_topic_and_since(db):
    assert rg.latest_run(db, "topic one", since=0)["brief_id"] == "b1"
    assert rg.latest_run(db, "topic one", since=300) is None


def test_score_run_combines_claims_domains_gate_gaps_and_anchors(db):
    run = rg.latest_run(db, "Topic one", since=0)
    got = rg.score_run(db, run, [{"desc": "a", "any": ["warner"]}, {"desc": "b", "any": ["zzz"]}])
    assert got["facts"] == 3
    assert got["assumptions"] == 1
    assert got["vendor"] == 1
    assert got["secondary"] == 1
    assert got["domains"] == 2  # www. is folded into the bare domain
    assert got["top_domain_share"] == pytest.approx(2 / 3)
    assert got["gate"] == {"subq": 2, "tallied": 1, "against_url": 1, "against_zero": 0}
    assert got["gaps"] == 1
    assert got["anchors_hit"] == 1
    assert got["anchors_total"] == 2


# --- queries file -------------------------------------------------------------


def test_load_queries_rejects_duplicate_ids(tmp_path):
    path = tmp_path / "q.json"
    item = {"id": "x", "query": "q", "mode": "normal", "anchors": []}
    path.write_text(json.dumps({"queries": [item, item]}), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        rg.load_queries(path)


def test_load_queries_rejects_an_invalid_regex(tmp_path):
    path = tmp_path / "q.json"
    item = {"id": "x", "query": "q", "mode": "normal", "anchors": [{"desc": "d", "any": ["("]}]}
    path.write_text(json.dumps({"queries": [item]}), encoding="utf-8")
    with pytest.raises(ValueError, match="regex"):
        rg.load_queries(path)
