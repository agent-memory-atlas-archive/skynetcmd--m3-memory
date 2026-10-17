"""What m3's MCP tools tell an agent about themselves (bin/tool_quality_check.py).

Clients saw 0% of the catalog's parameter descriptions and no annotations until
2026-10-17: FastMCP rebuilt each advertised schema from a generated signature.
Glama's tool-quality score is what surfaced it. These keep the catalog complete
and keep the server sending it.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

import tool_quality_check  # noqa: E402


def test_every_catalog_tool_documents_itself():
    findings = tool_quality_check.catalog_findings()
    assert not findings, "\n".join(f"{f['tool']}: {f['check']}: {f['detail']}" for f in findings)


def test_the_server_sends_the_catalog_schema_and_annotations():
    findings = tool_quality_check.wire_findings()
    assert not findings, "\n".join(f"{f['tool']}: {f['detail']}" for f in findings)


def test_annotations_follow_the_catalog():
    import mcp_tool_catalog
    from catalog.annotations import tool_annotations

    get, delete, write = (mcp_tool_catalog.get_tool(n) for n in ("memory_get", "memory_delete", "memory_write"))
    assert tool_annotations(get) == {"readOnlyHint": True, "destructiveHint": False}
    assert tool_annotations(delete) == {"readOnlyHint": False, "destructiveHint": True}
    assert tool_annotations(write) == {"readOnlyHint": False, "destructiveHint": False}
