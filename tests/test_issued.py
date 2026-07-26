import pytest

from research_state import db, issued


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "state.sqlite")
    issued.init_schema(c)
    return c


FRAG = {
    "text": "The constant k defaults to 60.",
    "char_start": 100,
    "char_end": 130,
    "prefix": "before ",
    "suffix": " after",
    "paragraph_index": 3,
    "score": 4.2,
}


def test_record_returns_a_stable_id(conn):
    first = issued.record(
        conn, url="https://e.com/a", fetched_at=1753500000, fragment=FRAG
    )
    second = issued.record(
        conn, url="https://e.com/a", fetched_at=1753500000, fragment=FRAG
    )
    assert first["fragment_id"] == second["fragment_id"]
    assert len(first["fragment_id"]) == 16


def test_id_changes_with_url_offset_or_fetch_time(conn):
    base = issued.record(
        conn, url="https://e.com/a", fetched_at=1753500000, fragment=FRAG
    )
    other_url = issued.record(
        conn, url="https://e.com/b", fetched_at=1753500000, fragment=FRAG
    )
    later = issued.record(
        conn, url="https://e.com/a", fetched_at=1753600000, fragment=FRAG
    )
    moved = issued.record(
        conn,
        url="https://e.com/a",
        fetched_at=1753500000,
        fragment={**FRAG, "char_start": 200},
    )
    ids = {
        base["fragment_id"],
        other_url["fragment_id"],
        later["fragment_id"],
        moved["fragment_id"],
    }
    assert len(ids) == 4


def test_get_returns_the_stored_snapshot(conn):
    fid = issued.record(
        conn, url="https://e.com/a", fetched_at=1753500000, fragment=FRAG
    )["fragment_id"]
    stored = issued.get(conn, fid)
    assert stored["exact"] == FRAG["text"]
    assert stored["url"] == "https://e.com/a"
    assert stored["prefix"] == "before "
    assert stored["char_start"] == 100


def test_get_returns_none_for_unknown_id(conn):
    assert issued.get(conn, "deadbeefdeadbeef") is None


def test_record_is_idempotent(conn):
    issued.record(conn, url="https://e.com/a", fetched_at=1753500000, fragment=FRAG)
    issued.record(conn, url="https://e.com/a", fetched_at=1753500000, fragment=FRAG)
    assert conn.execute("SELECT COUNT(*) FROM issued_fragments").fetchone()[0] == 1


def test_record_keeps_the_original_fragment_fields(conn):
    result = issued.record(
        conn, url="https://e.com/a", fetched_at=1753500000, fragment=FRAG
    )
    assert result["text"] == FRAG["text"]
    assert result["score"] == FRAG["score"]
