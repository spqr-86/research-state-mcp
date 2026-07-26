"""Every filesystem path the server uses, in one place and env-overridable.

# ANCHOR: config
# Role: nothing else may hardcode a path. Tests override via env vars, and the
# public repo stays free of anyone's home directory.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_STATE_DB = (
    Path.home() / ".local" / "share" / "research-state-mcp" / "state.sqlite"
)
DEFAULT_SEARCH_CACHE = Path.home() / ".cache" / "search-mcp" / "cache.sqlite"
DEFAULT_BRIEF_DIR = Path.home() / "knowledge" / "research"


def state_db_path() -> Path:
    """Our own state: jobs, plans, issued fragments, briefs."""
    return Path(os.environ.get("RESEARCH_STATE_DB", DEFAULT_STATE_DB))


def search_cache_path() -> Path:
    """The free-search-mcp page cache. Read-only for us."""
    return Path(os.environ.get("SEARCH_MCP_CACHE", DEFAULT_SEARCH_CACHE))


def brief_dir() -> Path:
    """Where finished briefs are written as markdown."""
    return Path(os.environ.get("RESEARCH_BRIEF_DIR", DEFAULT_BRIEF_DIR))
