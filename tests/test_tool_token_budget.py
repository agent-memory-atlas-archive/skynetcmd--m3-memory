"""The published tool-schema token figures match the measurement.

README.md's gating table and memory_bridge's docstring quote the context cost of
the tool schemas. They drifted: the universal `store` argument added +6,962
eager tokens (+21%) unmeasured, and the README figures were stale even before
that. bin/measure_tool_tokens.measure() is the one owner of the numbers; this
compares every quoted figure to it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from conftest import skip_reason

_ROOT = Path(__file__).resolve().parent.parent
_TOLERANCE = 0.02  # 2%: room for a description edit, not for a regression


@pytest.fixture(scope="module")
def measured():
    sys.path.insert(0, str(_ROOT / "bin"))
    import measure_tool_tokens as mt

    if not mt.METHOD.startswith("tiktoken"):
        pytest.skip(
            skip_reason(
                "dep",
                "tiktoken is not installed: the published figures are cl100k_base "
                "tokens and the 4-chars approximation would compare different units",
            )
        )
    return mt.measure()


def _num(text: str) -> int:
    return int(text.replace(",", ""))


def _readme_rows() -> dict[str, tuple[int, int]]:
    text = (_ROOT / "README.md").read_text(encoding="utf-8")
    rows = {}
    for label, key in (("Lazy (Default)", "lazy"), ("Typical Active Session", "typical"), ("Eager Mode", "eager")):
        m = re.search(rf"\|[^|\n]*{re.escape(label)}[^|\n]*\|\s*\**(\d+)\**\s*\|\s*\**~?([\d,]+)\**", text)
        assert m, f"README gating table has no {label!r} row"
        rows[key] = (int(m.group(1)), _num(m.group(2)))
    return rows


@pytest.mark.parametrize("row", ["lazy", "typical", "eager"])
def test_readme_gating_table_matches_the_measurement(measured, row):
    tools, tokens = _readme_rows()[row]
    assert tools == measured[f"{row}_tools"], (
        f"README {row} row says {tools} tools; measured {measured[f'{row}_tools']}"
    )
    actual = measured[f"{row}_tokens"]
    assert abs(tokens - actual) <= actual * _TOLERANCE, (
        f"README {row} row says ~{tokens:,} tokens; measured {actual:,}. "
        "Re-run `python bin/measure_tool_tokens.py` and update README.md"
    )


def test_memory_bridge_docstring_matches_the_measurement(measured):
    text = (_ROOT / "bin" / "memory_bridge.py").read_text(encoding="utf-8")
    lazy = re.search(r"([\d,]+) tokens on the MCP wire", text)
    eager = re.search(r"(\d+) tools, ([\d,]+) tokens", text)
    assert lazy and eager, "memory_bridge docstring no longer quotes the figures"
    assert abs(_num(lazy.group(1)) - measured["lazy_tokens"]) <= measured["lazy_tokens"] * _TOLERANCE
    assert int(eager.group(1)) == measured["eager_tools"]
    assert abs(_num(eager.group(2)) - measured["eager_tokens"]) <= measured["eager_tokens"] * _TOLERANCE
