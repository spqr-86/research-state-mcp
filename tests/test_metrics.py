import pytest

from research_state import db, metrics


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "state.sqlite")
    metrics.init_schema(c)
    return c


def test_stats_on_an_empty_db(conn):
    assert metrics.stats(conn) == {
        "fetches": 0,
        "chars_total": 0,
        "chars_returned": 0,
        "chars_saved": 0,
        "saved_ratio": 0.0,
    }


def test_stats_sum_across_fetches(conn):
    metrics.record_fetch(
        conn,
        url="https://e.com/a",
        paragraphs_total=60,
        paragraphs_returned=2,
        chars_total=1000,
        chars_returned=100,
    )
    metrics.record_fetch(
        conn,
        url="https://e.com/b",
        paragraphs_total=20,
        paragraphs_returned=3,
        chars_total=1000,
        chars_returned=300,
    )
    s = metrics.stats(conn)
    assert s["fetches"] == 2
    assert s["chars_total"] == 2000
    assert s["chars_returned"] == 400
    assert s["chars_saved"] == 1600
    assert s["saved_ratio"] == 0.8
