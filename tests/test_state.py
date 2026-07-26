import pytest

from research_state import db, state


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "state.sqlite")
    state.init_schema(c)
    return c


def test_start_job_returns_id_and_stores_topic(conn):
    job = state.start_job(conn, "reciprocal rank fusion")
    assert job["job_id"]
    stored = state.get_job(conn, job["job_id"])
    assert stored["topic"] == "reciprocal rank fusion"
    assert stored["status"] == "open"
    assert stored["subquestions"] == []


def test_start_job_rejects_empty_topic(conn):
    with pytest.raises(ValueError):
        state.start_job(conn, "   ")


def test_set_plan_stores_subquestions_in_order(conn):
    job_id = state.start_job(conn, "topic")["job_id"]
    state.set_plan(conn, job_id, ["what is it", "who uses it", "what breaks"])
    subqs = state.get_job(conn, job_id)["subquestions"]
    assert [s["text"] for s in subqs] == ["what is it", "who uses it", "what breaks"]
    assert {s["status"] for s in subqs} == {"open"}
    assert [s["subq_id"] for s in subqs] == [1, 2, 3]


def test_set_plan_appends_without_losing_progress(conn):
    """Raising the depth level mid-run must not restart the job."""
    job_id = state.start_job(conn, "topic")["job_id"]
    state.set_plan(conn, job_id, ["a", "b"])
    state.mark(conn, job_id, 1, "answer to a")
    state.set_plan(conn, job_id, ["c"])

    subqs = state.get_job(conn, job_id)["subquestions"]
    assert [s["text"] for s in subqs] == ["a", "b", "c"]
    assert subqs[0]["status"] == "closed"
    assert subqs[0]["answer"] == "answer to a"
    assert [s["subq_id"] for s in subqs] == [1, 2, 3]


def test_set_plan_skips_duplicate_subquestions(conn):
    job_id = state.start_job(conn, "topic")["job_id"]
    state.set_plan(conn, job_id, ["a", "b"])
    result = state.set_plan(conn, job_id, ["b", "c"])
    assert [s["text"] for s in state.get_job(conn, job_id)["subquestions"]] == [
        "a",
        "b",
        "c",
    ]
    assert result["added"] == 1
    assert result["skipped"] == 1


def test_set_plan_rejects_unknown_job(conn):
    with pytest.raises(state.UnknownJob):
        state.set_plan(conn, "nope", ["a"])


def test_mark_closes_subquestion_and_reports_progress(conn):
    job_id = state.start_job(conn, "topic")["job_id"]
    state.set_plan(conn, job_id, ["a", "b"])
    result = state.mark(conn, job_id, 1, "answered")
    assert result["closed"] == 1
    assert result["total"] == 2
    assert result["open_subquestions"] == [{"subq_id": 2, "text": "b"}]


def test_mark_rejects_unknown_subquestion(conn):
    job_id = state.start_job(conn, "topic")["job_id"]
    state.set_plan(conn, job_id, ["a"])
    with pytest.raises(state.UnknownSubquestion):
        state.mark(conn, job_id, 99, "answered")


def test_mark_rejects_empty_answer(conn):
    job_id = state.start_job(conn, "topic")["job_id"]
    state.set_plan(conn, job_id, ["a"])
    with pytest.raises(ValueError):
        state.mark(conn, job_id, 1, "")


def test_plan_survives_reconnect(tmp_path):
    path = tmp_path / "state.sqlite"
    first = db.connect(path)
    state.init_schema(first)
    job_id = state.start_job(first, "topic")["job_id"]
    state.set_plan(first, job_id, ["a", "b"])
    state.mark(first, job_id, 1, "answered")
    first.close()

    second = db.connect(path)
    state.init_schema(second)
    job = state.get_job(second, job_id)
    assert job["subquestions"][0]["status"] == "closed"
    assert job["subquestions"][1]["status"] == "open"


def test_init_schema_is_idempotent(conn):
    state.init_schema(conn)
    state.init_schema(conn)
    job_id = state.start_job(conn, "topic")["job_id"]
    assert state.get_job(conn, job_id)["topic"] == "topic"


def test_get_job_rejects_unknown_job(conn):
    with pytest.raises(state.UnknownJob):
        state.get_job(conn, "nope")


def test_open_subquestions_are_reported(conn):
    job_id = state.start_job(conn, "topic")["job_id"]
    state.set_plan(conn, job_id, ["a", "b"])
    state.mark(conn, job_id, 1, "answered")
    gaps = state.gaps(conn, job_id)
    assert gaps["open"] == [{"subq_id": 2, "text": "b"}]
    assert gaps["closed"] == 1
    assert gaps["total"] == 2


def test_gaps_on_a_fully_closed_job(conn):
    job_id = state.start_job(conn, "topic")["job_id"]
    state.set_plan(conn, job_id, ["a"])
    state.mark(conn, job_id, 1, "answered")
    assert state.gaps(conn, job_id)["open"] == []
