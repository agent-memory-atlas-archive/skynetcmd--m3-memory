#!/usr/bin/env python3
"""`m3 config show`: every setting, its value and where it came from (read-only).

    m3 config show [--origin] [--json] [--config FILE]

A repair command: it always starts. An invalid m3_config.toml or environment
value is printed in full (every problem, its line and the fix) with exit 2,
never a traceback (CONFIG_DESIGN §3). Secrets are listed by name and source
only; a value is never printed (§10).

Exit codes: 0 = shown; 2 = the configuration is invalid (problems printed).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

_BIN = os.path.dirname(os.path.abspath(__file__))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from m3_core.config import REGISTRY, KeyClass, format_config_error, locate  # noqa: E402
from m3_core.config.loader import ConfigFileError  # noqa: E402
from m3_core.config.resolve import ConfigEnvErrors  # noqa: E402
from m3_core.config.settings import Settings, build  # noqa: E402


def _value_text(value: object) -> str:
    return json.dumps(value if not hasattr(value, "__fspath__") else str(value))


def _secrets() -> list[dict]:
    from m3_core.secret_store import explain_named_secret

    out = []
    for key in sorted((k for k in REGISTRY if k.klass is KeyClass.SECRET), key=lambda k: k.name):
        r = explain_named_secret(key.name)
        out.append(
            {
                "name": key.name,
                "set": bool(r.value),
                "source": r.source,
                "bootstrap": key.bootstrap,
                "errors": list(r.errors),
            }
        )
    return out


def show(settings: Settings, *, origin: bool, as_json: bool) -> str:
    rows: list[dict[str, Any]] = [
        {
            "key": name,
            "value": r.value if not hasattr(r.value, "__fspath__") else str(r.value),
            "origin": r.origin,
            "shadowed": list(r.shadowed),
        }
        for name, r in sorted(settings.resolved.items())
    ]
    secrets = _secrets()
    if as_json:
        return json.dumps(
            {
                "file": str(settings.location.file),
                "locator": settings.location.tier,
                "file_exists": settings.identity is not None,
                "settings": rows,
                "secrets": secrets,
                "legacy_env_in_use": settings.legacy_env_in_use(),
                "ignored_env": settings.shadowed_env(),
                "retired_env_set": list(settings.retired_env),
                "warnings": list(settings.warnings),
            },
            indent=2,
        )
    exists = "" if settings.identity is not None else "  (absent: every key takes its default)"
    lines = [f"# {settings.location.file}{exists}", f"# located by {settings.location.tier}", ""]
    width = max((len(r["key"]) for r in rows), default=0)
    for r in rows:
        line = f"{r['key']:<{width}} = {_value_text(r['value'])}"
        if origin:
            line += f"    # {r['origin']}"
            if r["shadowed"]:
                line += f"; ignored env: {', '.join(r['shadowed'])}"
        lines.append(line)
    from m3_core.secret_store import SecretsFileError, secrets_file_path

    try:
        where = f"secrets file: {secrets_file_path()}"
    except SecretsFileError as e:
        where = f"secrets file: cannot be located ({e})"
    lines += ["", f"# secrets (values are never shown; {where})"]
    swidth = max((len(s["name"]) for s in secrets), default=0)
    for s in secrets:
        state = f"set, from {s['source']}" if s["set"] else "not set"
        if not s["set"] and s["errors"]:
            state += f" ({'; '.join(s['errors'])})"
        lines.append(f"{s['name']:<{swidth}}  {state}" + ("    # bootstrap" if s["bootstrap"] and origin else ""))
    if settings.legacy_env_in_use():
        lines += [
            "",
            "# honoured from legacy environment names until N+3; move them into the file:",
            *(f"#   {n}" for n in settings.legacy_env_in_use()),
        ]
    if settings.shadowed_env():
        lines += [
            "",
            "# ignored environment names (the file sets these keys):",
            *(f"#   {n}" for n in settings.shadowed_env()),
        ]
    if settings.retired_env:
        from m3_core.config.keys import RETIRED

        lines += [
            "",
            "# set, but have NO effect (retired settings; unset them):",
            *(f"#   {n}: {RETIRED[n]}" for n in settings.retired_env),
        ]
    lines += [f"# warning: {w}" for w in settings.warnings]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="m3 config", description="Show m3's configuration (read-only).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("show", help="every setting with its value (and, with --origin, its source)")
    p.add_argument("--origin", action="store_true", help="say where each value came from")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--config", metavar="FILE", help="read this file instead of the located one")
    args = ap.parse_args(argv)

    try:
        settings = build(locate(args.config))
    except (ConfigFileError, ConfigEnvErrors) as e:
        print(format_config_error(e), file=sys.stderr)
        return 2
    print(show(settings, origin=args.origin, as_json=args.json))
    return 0


if __name__ == "__main__":
    sys.exit(main())
