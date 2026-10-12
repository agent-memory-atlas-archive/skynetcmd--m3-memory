#!/usr/bin/env python3
"""`m3 config`: show, read and change m3_config.toml.

    m3 config show [--origin] [--json] [--config FILE]
    m3 config get KEY [--origin] [--config FILE]
    m3 config set KEY VALUE [--yes] [--config FILE]
    m3 config unset KEY [--yes] [--config FILE]
    m3 config import-env [--dry-run] [--config FILE]

`set` takes a value as the environment would spell it (a list comma-separated)
and writes it in its TOML type under the file's lock (CONFIG_DESIGN §12). Secrets
are never written to this file. A key that moves the store or the roots asks a
person at the console, or needs --yes (plan "Config-set confirmation": defence in
depth, not a boundary; a shell can edit the file directly).

A repair command: it always starts. An invalid m3_config.toml or environment
value is printed in full (every problem, its line and the fix) with exit 2,
never a traceback (CONFIG_DESIGN §3). Secrets are listed by name and source
only; a value is never printed (§10).

Exit codes: 0 = done; 1 = declined at the prompt; 2 = refused or invalid
(problems printed).
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
from m3_core.config.keys import DB_BACKEND  # noqa: E402
from m3_core.config.loader import ConfigFileError  # noqa: E402
from m3_core.config.registry import ConfigValueError, Key, Scope  # noqa: E402
from m3_core.config.resolve import ConfigEnvErrors, env_outranks_file  # noqa: E402
from m3_core.config.settings import Settings, build, env_contradicting  # noqa: E402
from m3_core.config.writer import ConfigWriteError, set_values  # noqa: E402
from m3_core.seams import console_has_person  # noqa: E402


def needs_confirmation(key: Key) -> bool:
    """The backend selector, or a LOCATOR key (it decides which file is read: the
    roots, the secrets file). A mistaken or injected write to one moves where m3
    keeps its data, so `set`/`unset` confirm it. The registry's own facts, not a
    list kept here."""
    return key is DB_BACKEND or key.scope is Scope.LOCATOR


class _Refused(Exception):
    """A request that changes nothing; the message is printed and exit is 2."""


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
                "llm_file_in_use": settings.llm_file_in_use(),
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
    if settings.llm_file_in_use():
        lines += [
            "",
            "# honoured from the legacy .llm_config.json; move them into [llm]:",
            *(f"#   {n}" for n in settings.llm_file_in_use()),
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


def _config_key(name: str) -> Key:
    key = REGISTRY.get(name)
    if key is None:
        raise _Refused(f"observed: {name!r} is not a setting. inspect: `m3 config show` lists every key")
    if key.klass is KeyClass.SECRET:
        raise _Refused(
            f"observed: {name} is a secret; secrets are never written to m3_config.toml. "
            "inspect: store it in the keyring or the secrets file (`m3 config show` names each secret's source)"
        )
    if key.klass is not KeyClass.CONFIG:
        raise _Refused(f"observed: {name} is a {key.klass} key, not a setting")
    return key


def _confirmed(key: Key, change: str, file: object, yes: bool) -> bool:
    """True to go ahead. A key that needs confirmation needs --yes or a person who
    says yes; with no one at the console and no --yes it is refused (exit 2)."""
    name = key.name
    if yes or not needs_confirmation(key):
        return True
    if not console_has_person():
        raise _Refused(
            f"observed: {name} changes where m3 keeps its data; no one is at the console to confirm. "
            "inspect: rerun with --yes"
        )
    return input(f"{change} in {file}? [y/N] ").strip().lower() in ("y", "yes")


def _get(args: argparse.Namespace) -> int:
    key = _config_key(args.key)
    settings = build(locate(args.config))
    line = _value_text(settings[key])
    print(f"{line}    # {settings.origin(key)}" if args.origin else line)
    return 0


def _set(args: argparse.Namespace) -> int:
    key = _config_key(args.key)
    loc = locate(args.config)
    value = None
    if args.cmd == "set":
        try:
            value = key.coerce_env_value(args.value)
        except ConfigValueError as e:
            raise _Refused(f"observed: {args.key} = {args.value!r} is refused: {e}") from None
    change = f"Set {args.key} = {_value_text(value)}" if args.cmd == "set" else f"Remove {args.key}"
    if not _confirmed(key, change, loc.file, args.yes):
        print("not changed")
        return 1
    try:
        set_values(loc, {key: value})
    except ConfigWriteError as e:
        raise _Refused(f"observed: {e}") from None
    done = f"{args.key} = {_value_text(value)}" if value is not None else f"removed {args.key}"
    print(f"{done}  ({loc.file})")
    if value is not None:
        winner = "the environment" if env_outranks_file(key) else "the file"
        for name, seen in env_contradicting(key, value):
            used = seen if env_outranks_file(key) else value
            print(
                f"  [!] {name}={seen} is set in this process's environment and disagrees; m3 uses "
                f"{_value_text(used)} ({winner}). fix: remove {name} where it is set (`m3 doctor` "
                "names each place)."
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="m3 config", description="Show, read and change m3_config.toml.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("show", help="every setting with its value (and, with --origin, its source)")
    p.add_argument("--origin", action="store_true", help="say where each value came from")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--config", metavar="FILE", help="read this file instead of the located one")
    g = sub.add_parser("get", help="one setting's value")
    g.add_argument("key")
    g.add_argument("--origin", action="store_true", help="say where the value came from")
    s_ = sub.add_parser("set", help="write one setting to m3_config.toml")
    s_.add_argument("key")
    s_.add_argument("value", help="as the environment would spell it; a list comma-separated")
    u = sub.add_parser("unset", help="remove one setting from m3_config.toml (it falls back)")
    u.add_argument("key")
    for q in (s_, u):
        q.add_argument("--yes", "-y", action="store_true", help="skip the confirmation for a sensitive key")
    im = sub.add_parser("import-env", help="move settings from env, rc files, .env and MCP env blocks into the file")
    im.add_argument("--dry-run", action="store_true", help="say what would be imported; write nothing")
    for q in (g, s_, u, im):
        q.add_argument("--config", metavar="FILE", help="use this file instead of the located one")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "get":
            return _get(args)
        if args.cmd in ("set", "unset"):
            return _set(args)
        if args.cmd == "import-env":
            from m3_memory.install import import_env  # the sweep lives with the installer

            return import_env.run(args.config, dry_run=args.dry_run)
        settings = build(locate(args.config))
    except _Refused as e:
        print(e, file=sys.stderr)
        return 2
    except (ConfigFileError, ConfigEnvErrors) as e:
        print(format_config_error(e), file=sys.stderr)
        return 2
    print(show(settings, origin=args.origin, as_json=args.json))
    return 0


if __name__ == "__main__":
    sys.exit(main())
