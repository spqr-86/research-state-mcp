from pathlib import Path

from research_state import config


def test_defaults_live_under_home():
    assert (
        config.state_db_path()
        == Path.home() / ".local/share/research-state-mcp/state.sqlite"
    )
    assert config.search_cache_path() == Path.home() / ".cache/search-mcp/cache.sqlite"
    assert config.brief_dir() == Path.home() / "knowledge/research"


def test_every_path_is_env_overridable(monkeypatch, tmp_path):
    monkeypatch.setenv("RESEARCH_STATE_DB", str(tmp_path / "s.sqlite"))
    monkeypatch.setenv("SEARCH_MCP_CACHE", str(tmp_path / "c.sqlite"))
    monkeypatch.setenv("RESEARCH_BRIEF_DIR", str(tmp_path / "briefs"))
    assert config.state_db_path() == tmp_path / "s.sqlite"
    assert config.search_cache_path() == tmp_path / "c.sqlite"
    assert config.brief_dir() == tmp_path / "briefs"
