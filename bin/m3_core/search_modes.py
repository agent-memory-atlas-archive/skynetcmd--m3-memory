"""Memory search modes: the public names and what each selects. The one owner.

The MCP schemas offered ``keyword`` while the search understood only ``fts5``
(its internal name for lexical-only search; on PostgreSQL it selects the same
query compilation for tsvector). A client choosing keyword search, exactly as
the schema offered, got an empty result and no error, and an unknown mode was
equally silent. Every mode now passes through ``normalize_search_mode``: a
public or internal name maps to the mode the search implements, anything else
raises with the valid names.
"""
from __future__ import annotations

# public name -> the mode memory_search_scored_impl implements
SEARCH_MODES: dict[str, str] = {
    "hybrid": "hybrid",      # lexical + vector
    "semantic": "semantic",  # vector only
    "keyword": "fts5",       # lexical only
}
# Accepted from internal callers as well; never advertised in a schema.
_INTERNAL = {"fts5": "fts5"}

#: What a tool schema offers (its "enum").
PUBLIC_SEARCH_MODES: tuple[str, ...] = tuple(SEARCH_MODES)


def normalize_search_mode(mode: str) -> str:
    """The implemented mode for ``mode``; raises ValueError for anything else."""
    key = (mode or "").strip().lower()
    found = SEARCH_MODES.get(key) or _INTERNAL.get(key)
    if found is None:
        raise ValueError(f"observed: search_mode={mode!r}. inspect: use one of "
                         f"{', '.join(PUBLIC_SEARCH_MODES)}")
    return found
