"""import-env slice 3: put every m3 secret in the secrets file (user decisions,
m3 c7082b20, 0be66297, a8083888, 18720cb5).

For each m3 secret:

  - already listed in the secrets file: left alone; a legacy copy that differs
    is named (never its value), because m3 reads only the file now;
  - held in the OS keyring and not listed: the file gets a POINTER to that
    entry, ``NAME = { keyring = "system/NAME" }``, so it lists every secret;
  - set in persistent legacy places (rc files, ``.env``, the Windows registry,
    m3's own MCP entries, ``.llm_config.json``) that agree: the value is stored
    in the file and read back;
  - the places disagree: a person at the console picks among MASKED values;
    with no one there, every place is listed and nothing is stored.

A secret seen only in this process's environment is listed, never stored, and
another tool's MCP entry is never a source (report only). Values never reach
stdout, logs, m3_config.toml or a backup: set_secret writes the file directly.
"""

from __future__ import annotations

from collections.abc import Callable

from m3_memory import _bin

_bin.ensure_on_path()

from m3_core.config import REGISTRY, KeyClass  # noqa: E402
from m3_core.secret_store import (  # noqa: E402
    KeyringRef,
    SecretsFileError,
    default_keyring_ref,
    keyring_value,
    mask_secret,
    read_secret_entries,
    secrets_file_path,
    set_secret,
    store_keys_for,
)

from m3_memory.install.env_sources import EnvFound  # noqa: E402


def _places(found: list[EnvFound]) -> str:
    return ", ".join(sorted({f"{f.place} ({f.name})" for f in found}))


def _choose(name: str, by_value: dict[str, list[EnvFound]], ask: Callable[[str], str]) -> str | None:
    """A person picks one of the disagreeing values, shown masked; None skips."""
    options = sorted(by_value)
    print(f"\n{name}: the places that set it disagree (values masked):")
    for i, value in enumerate(options, 1):
        print(f"  {i}. {mask_secret(value)}  from {_places(by_value[value])}")
    answer = ask("  store which (number; Enter to skip)? ").strip()
    if answer.isdigit() and 1 <= int(answer) <= len(options):
        return options[int(answer) - 1]
    if answer:
        print("  not one of those; skipped")
    return None


def import_secrets(
    found: list[EnvFound], *, interactive: bool, dry_run: bool, ask: Callable[[str], str] | None = None
) -> list[str]:
    """Report lines for every m3 secret; stores what the rules above allow
    (nothing on a dry run). ``found`` is the sweep's secret-class hits; ``ask``
    reads a person's answer (``input`` when not given)."""
    ask = ask or input
    try:
        entries = read_secret_entries()
    except SecretsFileError as e:
        return [f"  [!] secrets: the secrets file was refused, so no secret was imported: {e}"]
    path = secrets_file_path()
    verb = "would store" if dry_run else "stored"
    by_key: dict[str, list[EnvFound]] = {}
    for f in found:
        key = REGISTRY.by_env(f.name)
        if key is not None and key.klass is KeyClass.SECRET:
            by_key.setdefault(key.name, []).append(f)

    lines: list[str] = []
    for key in sorted((k for k in REGISTRY if k.klass is KeyClass.SECRET), key=lambda k: k.name):
        name = key.name
        hits = by_key.get(name, [])
        for f in hits:
            if f.kind == "mcp" and not f.m3_server:
                lines.append(f"  [i] {name}: {f.name} in {f.place} belongs to another tool; m3 leaves it there")
            elif f.kind == "process":
                lines.append(
                    f"  [i] {name}: set only in this process's environment ({f.name}); not stored. "
                    f"m3 does not read it there: `m3 secret set {name}`"
                )
        legacy = [f for f in hits if f.persistent and f.value.strip()]
        listed = next((entries[k] for k in store_keys_for(name) if k in entries), None)
        if listed is not None:
            differs = [f for f in legacy if not isinstance(listed, KeyringRef) and f.value.strip() != listed.strip()]
            lines.append(f"  [=] {name}: listed in {path}")
            if differs:
                lines.append(f"      copies that differ, which m3 no longer reads: {_places(differs)}")
            continue
        ref = next(
            (r for r in map(default_keyring_ref, store_keys_for(name)) if (keyring_value(r)[0] or "").strip()), None
        )
        if ref is not None:
            if not dry_run:
                set_secret(name, ref)
            note = "would point" if dry_run else "now points"
            lines.append(f"  [+] {name}: in the OS keyring; {path} {note} at keyring {ref}")
            if legacy:
                lines.append(f"      legacy copies, which m3 no longer reads: {_places(legacy)}")
            continue
        if not legacy:
            continue
        by_value: dict[str, list[EnvFound]] = {}
        for f in legacy:
            by_value.setdefault(f.value.strip(), []).append(f)
        if len(by_value) == 1:
            (value,) = by_value
        elif interactive:
            picked = _choose(name, by_value, ask)
            if picked is None:
                lines.append(f"  [-] {name}: skipped; store it with `m3 secret set {name}`")
                continue
            value = picked
        else:
            lines.append(f"  [!] {name}: the places that set it disagree; nothing stored. Places: {_places(legacy)}")
            lines.append(f"      settle it with `m3 secret set {name}` (prompts for the value)")
            continue
        if not dry_run:
            set_secret(name, value)
            if read_secret_entries().get(name) != value:
                raise SecretsFileError(f"{path}: wrote {name} but the read-back returned a different value")
        lines.append(f"  [+] {verb} {name} in {path} (from {_places(by_value[value])})")
    return lines
