"""CrewAI's metadata_filter is applied, never accepted and ignored.

An ignored filter widened search to every record and made a filter-only delete
remove nothing without saying so. Hermetic: neither module imports crewai.
"""

from __future__ import annotations

import json

from m3_memory.integrations.crewai import mapping
from m3_memory.integrations.crewai.backend import _match_for_delete


def test_filter_is_key_equality():
    md = {"team": "research", "n": 1}
    assert mapping.metadata_matches(md, None)
    assert mapping.metadata_matches(md, {})
    assert mapping.metadata_matches(md, {"team": "research"})
    assert not mapping.metadata_matches(md, {"team": "ops"})
    assert not mapping.metadata_matches(md, {"absent": None})


def test_delete_matcher_applies_the_filter():
    item = {"metadata_json": json.dumps({"team": "research"}), "created_at": "2026-01-01T00:00:00+00:00"}
    assert _match_for_delete(item, None, None, None, {"team": "research"})
    assert not _match_for_delete(item, None, None, None, {"team": "ops"})
