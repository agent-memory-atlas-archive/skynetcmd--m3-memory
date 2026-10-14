"""`m3 config import-env`: move settings from the legacy places into m3_config.toml.

Sources are the one sweep (``env_sources.scan``): this process's environment,
MCP client env blocks (``~/.claude.json`` among them), the ``.env`` files, home rc
files, the Windows persistent environment and ``.llm_config.json``.

The conflict rule: a key is imported only
when every source that sets it agrees. On a disagreement a person at the console
chooses; with no one there (``m3 upgrade``) nothing is written for that key, and
every source and value is listed with the ``m3 config set`` that settles it.

A key already in the file is left alone (the file outranks every legacy source).
Secrets go to the secrets file, never m3_config.toml (slice 3, ``import_secrets``:
persistent places only; a keyring-held secret becomes a pointer; values are never
shown, logged or backed up). m3_config.toml is backed up first (``GuardedRewrite``: ``m3_config.toml.bak`` and a
``.backup_history`` section), and every imported key must then resolve from the
file to the chosen value; if one does not, the backup is restored. The legacy
places are never edited; ``.llm_config.json`` is renamed ``.imported`` once all
of its values are in the file.

Exit codes: 0 = done (or nothing to do); 2 = refused or rolled back.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from m3_memory import _bin
from m3_memory._platform import console_has_person

_bin.ensure_on_path()

from m3_core.config import REGISTRY, KeyClass, locate  # noqa: E402
from m3_core.config.loader import ConfigFileError, Location, format_config_error  # noqa: E402
from m3_core.config.registry import ConfigValue, ConfigValueError, Key  # noqa: E402
from m3_core.config.resolve import ConfigEnvErrors  # noqa: E402
from m3_core.config.settings import Settings, build  # noqa: E402

from m3_memory.install.env_sources import EnvFound, scan  # noqa: E402

BY = "m3 config import-env"


@dataclass(slots=True)
class _Candidate:
    key: Key
    values: dict[str, list[EnvFound]] = field(default_factory=dict)  # shown value -> its sources
    parsed: dict[str, ConfigValue] = field(default_factory=dict)  # shown value -> parsed
    bad: list[tuple[EnvFound, str]] = field(default_factory=list)  # unparseable, with the reason
    computed: list[EnvFound] = field(default_factory=list)  # worked out by the shell: not a value


def _shown(value: object) -> str:
    return json.dumps(str(value) if isinstance(value, Path) else value)


def _where(sources: Iterable[EnvFound]) -> str:
    return ", ".join(sorted({f"{f.where} ({f.name})" for f in sources}))


def _candidates(found: list[EnvFound]) -> tuple[dict[str, _Candidate], list[EnvFound]]:
    """Group what the sweep found by registry key; secrets are returned apart."""
    out: dict[str, _Candidate] = {}
    secrets: list[EnvFound] = []
    for f in found:
        key = REGISTRY.by_env(f.name)
        if key is None:
            continue
        if key.klass is KeyClass.SECRET:
            secrets.append(f)
            continue
        if key.klass is not KeyClass.CONFIG:
            continue
        c = out.setdefault(key.name, _Candidate(key))
        if f.computed:
            c.computed.append(f)
            continue
        try:
            value = key.coerce_env_value(f.value)
        except ConfigValueError as e:
            c.bad.append((f, str(e)))
            continue
        shown = _shown(value)
        c.values.setdefault(shown, []).append(f)
        c.parsed[shown] = value
    return out, secrets


def _choose(c: _Candidate) -> str | None:
    """The value a person picks for a disagreement, or None (skip)."""
    options = sorted(c.values)
    print(f"\n{c.key.name}: the places that set it disagree:")
    for i, shown in enumerate(options, 1):
        print(f"  {i}. {shown}  from {_where(c.values[shown])}")
    answer = input("  import which (number or value; Enter to skip)? ").strip()
    if not answer:
        return None
    if answer.isdigit() and 1 <= int(answer) <= len(options):
        return options[int(answer) - 1]
    for shown in options:
        if answer in (shown, shown.strip('"')):
            return shown
    print("  not one of those; skipped")
    return None


def _resolves_from_file(settings: Settings, key: Key, value: ConfigValue) -> bool:
    r = settings.resolved[key.name]
    return r.origin.startswith("file:") and bool(r.value == value)


def _plan(
    loc: Location, current: Settings, *, interactive: bool, verb: str
) -> tuple[dict[Key, ConfigValue], list[str], list[EnvFound]]:
    names = sorted(n for k in REGISTRY if k.klass in (KeyClass.CONFIG, KeyClass.SECRET) for n in k.env_names)
    found = scan(names)
    candidates, secrets = _candidates(found)
    chosen: dict[Key, ConfigValue] = {}
    lines: list[str] = []
    for name in sorted(candidates):
        c = candidates[name]
        for f, why in c.bad:
            lines.append(f"  [!] {name}: {f.name}={f.value!r} in {f.where} does not read ({why}); not imported")
        for f in c.computed:
            lines.append(
                f"  [i] {name}: {f.name} in {f.where} is worked out by the shell when it starts "
                f"({f.value}); not imported. If m3 needs it: `m3 config set {name} VALUE`"
            )
        if not c.values:
            continue
        if current.resolved[name].origin.startswith("file:"):
            lines.append(f"  [=] {name}: already set in {loc.file}; the legacy places are ignored")
            continue
        if len(c.values) == 1:
            (shown,) = c.values
        elif interactive:
            picked = _choose(c)
            if picked is None:
                lines.append(f"  [-] {name}: skipped; settle it with `m3 config set {name} VALUE`")
                continue
            shown = picked
        else:
            lines.append(f"  [!] {name}: the places that set it disagree; nothing imported:")
            lines += [f"        {s}  from {_where(c.values[s])}" for s in sorted(c.values)]
            lines.append(f"      settle it with `m3 config set {name} VALUE`")
            continue
        chosen[c.key] = c.parsed[shown]
        lines.append(f"  [+] {verb} {name} = {shown}  (from {_where(c.values[shown])})")
        if all(f.kind == "rc" for f in c.values[shown]):
            # A value only shell startup files set reached terminals, never the
            # services (launchd/systemd do not read them); from the file, it does.
            lines.append(
                "      only shell startup files set it, which m3's background services do not read; they use it from now on"
            )
    return chosen, lines, secrets


def _rename_llm_file(loc: Location, after: Settings) -> str | None:
    """Rename .llm_config.json once every value in it resolves from the file."""
    from m3_core.config.env import llm_file

    path, values = llm_file()
    if path is None:
        return None
    for name in values:
        key = REGISTRY.by_env(name)
        if key is not None and not after.resolved[key.name].origin.startswith("file:"):
            return f"  [i] kept {path}: {name} is not in {loc.file} yet"
    done = Path(path).with_name(Path(path).name + ".imported")
    os.replace(path, done)
    return f"  [+] renamed {path} to {done.name}; its values are in {loc.file}"


def run(config: str | None, *, dry_run: bool, non_interactive: bool = False) -> int:
    from m3_core.atomic_io import GuardedRewrite, atomic_write
    from m3_core.config.writer import ConfigWriteError, set_values

    loc = locate(config)
    try:
        current = build(loc)
    except (ConfigFileError, ConfigEnvErrors) as e:
        print(format_config_error(e), file=sys.stderr)
        return 2
    verb = "would import" if dry_run else "imported"
    chosen, lines, secrets = _plan(
        loc, current, interactive=console_has_person() and not (dry_run or non_interactive), verb=verb
    )
    print(f"m3 config import-env: {loc.file}")
    for line in lines:
        print(line)
    from m3_core.secret_store import SecretsFileError

    from m3_memory.install.import_secrets import import_secrets

    try:
        for line in import_secrets(
            secrets, interactive=console_has_person() and not (dry_run or non_interactive), dry_run=dry_run
        ):
            print(line)
    except (OSError, SecretsFileError) as e:
        print(f"observed: import-env could not store a secret: {e}; inspect: `m3 doctor --verbose`", file=sys.stderr)
        return 2
    from m3_memory.install.sections import locator_env_leftovers

    for line in locator_env_leftovers():  # a locator, not a setting: named, never moved
        print(line)
    if not chosen:
        print("  nothing to import")
        return 0
    if dry_run:
        return 0

    guard = GuardedRewrite(loc.file, by=BY) if os.path.exists(loc.file) else None
    try:
        if guard is not None:
            guard.begin()
        set_values(loc, chosen)
        after = build(loc)
    except (OSError, ConfigWriteError, ConfigFileError, ConfigEnvErrors) as e:
        print(f"observed: import-env could not write {loc.file}: {e}", file=sys.stderr)
        return _restore(guard, loc, atomic_write, f"the write failed: {e}")
    wrong = [k.name for k, v in chosen.items() if not _resolves_from_file(after, k, v)]
    if wrong:
        print(
            f"observed: after the write, {', '.join(wrong)} did not resolve from {loc.file} to the imported value",
            file=sys.stderr,
        )
        return _restore(guard, loc, atomic_write, f"{', '.join(wrong)} did not resolve from the file")
    if guard is not None:
        guard.end(loc.file.read_bytes())
    note = _rename_llm_file(loc, after)
    if note:
        print(note)
    print(f"  {len(chosen)} setting(s) now in {loc.file}; the legacy places still hold them until you remove them")
    return 0


def _restore(guard, loc: Location, atomic_write, reason: str) -> int:
    if guard is None:  # the file did not exist before: take the new one away
        loc.file.unlink(missing_ok=True)
        print(f"  restored: removed the new {loc.file}", file=sys.stderr)
    else:
        atomic_write(loc.file, guard.bak.read_bytes())
        guard.abort(reason)
        print(f"  restored {loc.file} from {guard.bak}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="m3 config import-env", description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="say what would be imported; write nothing")
    ap.add_argument(
        "--non-interactive", action="store_true", help="never ask: list each disagreement and import nothing for it"
    )
    from m3_core.paths import add_config_arg

    add_config_arg(ap)  # the one --config owner: pins it, so the secrets file follows it
    args = ap.parse_args(argv)
    return run(args.config_file, dry_run=args.dry_run, non_interactive=args.non_interactive)


if __name__ == "__main__":
    sys.exit(main())
