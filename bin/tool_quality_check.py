#!/usr/bin/env python3
"""tool_quality_check.py — check what m3's MCP tools tell an agent about themselves.

Usage:
    python bin/tool_quality_check.py          # report; exit 1 on any failure
    python bin/tool_quality_check.py --json

Registries grade MCP servers on their tool definitions (Glama's TDQS: purpose,
usage guidance, parameter documentation, annotations). Glama's grader is a
hosted model and is not published, so this checks the objective half locally,
on every catalog tool:

  * every parameter has a description;
  * every tool's annotations (readOnlyHint / destructiveHint) are consistent:
    a read-only tool is never destructive, every READ_ONLY_TOOLS name exists;
  * every tool description is at least MIN_DESCRIPTION characters;
  * what the server SENDS equals the catalog: until 2026-10-17 FastMCP rebuilt
    each schema from a generated signature and dropped every parameter
    description, so clients saw 0% while the catalog held ~90%.

tests/test_tool_quality.py runs it, so a regression fails the suite.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MIN_DESCRIPTION = 60


def catalog_findings() -> list[dict]:
    import mcp_tool_catalog
    from catalog.annotations import READ_ONLY_TOOLS, tool_annotations

    findings: list[dict] = []
    names = {t.name for t in mcp_tool_catalog.MCP_TOOLS}
    for name in sorted(READ_ONLY_TOOLS - names):
        findings.append(
            {"tool": name, "check": "annotations", "detail": "READ_ONLY_TOOLS names a tool the catalog lacks"}
        )
    for spec in mcp_tool_catalog.MCP_TOOLS:
        if len(spec.description or "") < MIN_DESCRIPTION:
            findings.append(
                {
                    "tool": spec.name,
                    "check": "description",
                    "detail": f"{len(spec.description or '')} chars (minimum {MIN_DESCRIPTION}): say what it returns and when to use it",
                }
            )
        for param, schema in (spec.parameters.get("properties") or {}).items():
            if not (schema or {}).get("description"):
                findings.append({"tool": spec.name, "check": "parameter", "detail": f"`{param}` has no description"})
        ann = tool_annotations(spec)
        if ann["readOnlyHint"] and ann["destructiveHint"]:
            findings.append({"tool": spec.name, "check": "annotations", "detail": "read-only AND destructive"})
        if spec.name in READ_ONLY_TOOLS and spec.is_destructive:
            findings.append(
                {"tool": spec.name, "check": "annotations", "detail": "listed read-only but gated as destructive"}
            )
    return findings


def wire_findings() -> list[dict]:
    """Register every tool on a scratch server through the bridge's own path and
    compare what it lists with the catalog."""
    import mcp_compat
    import mcp_tool_catalog
    from catalog.annotations import tool_annotations

    server = mcp_compat.FastMCP("tool-quality-check")
    for spec in mcp_tool_catalog.MCP_TOOLS:

        def _fn(**kwargs):  # the advertised schema is the subject, not the call
            return None

        mcp_compat.register_tool(
            server,
            _fn,
            name=spec.name,
            description=spec.description,
            input_schema=spec.parameters,
            annotations=tool_annotations(spec),
        )
    listed = {t.name: t for t in asyncio.run(server.list_tools())}
    findings: list[dict] = []
    for spec in mcp_tool_catalog.MCP_TOOLS:
        tool = listed.get(spec.name)
        if tool is None:
            findings.append({"tool": spec.name, "check": "wire", "detail": "not listed"})
            continue
        sent = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)
        if sent != spec.parameters:
            findings.append({"tool": spec.name, "check": "wire", "detail": "listed schema differs from the catalog"})
        if tool.annotations is None:
            findings.append({"tool": spec.name, "check": "wire", "detail": "listed without annotations"})
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--json", action="store_true", help="print findings as JSON")
    args = ap.parse_args(argv)
    findings = catalog_findings() + wire_findings()
    if args.json:
        print(json.dumps(findings, indent=2))
    elif findings:
        for f in findings:
            print(f"FAIL  {f['tool']:<34} {f['check']:<12} {f['detail']}")
        print(f"\n{len(findings)} finding(s)")
    else:
        print("tool definitions: OK (descriptions, parameters, annotations, wire schema)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
