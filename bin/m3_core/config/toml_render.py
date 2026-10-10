"""Render a flat TOML table. THE owner for m3's generated TOML files (the
secrets file, m3-embed-server's [embed] file) until tomlkit (U11) edits the
user's own m3_config.toml.

JSON string escapes (``\\" \\\\ \\n \\uXXXX``) are all valid TOML basic-string
escapes, so ``json.dumps`` renders a correct TOML string for any value; ints,
floats and bools render as TOML literals. Nothing else is accepted: a value
that cannot be rendered exactly is refused, never stringified.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping


def toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{value!r} has no TOML form m3 writes")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    raise TypeError(f"cannot render {type(value).__name__} {value!r} as a TOML value")


def render_table(table: str, values: Mapping[str, object], *, header: str = "") -> str:
    """``[table]`` then ``key = value`` lines in sorted order; None values are omitted."""
    lines = [f"# {line}" for line in header.splitlines()] if header else []
    lines.append(f"[{table}]")
    lines += [f"{k} = {toml_value(v)}" for k, v in sorted(values.items()) if v is not None]
    return "\n".join(lines) + "\n"
