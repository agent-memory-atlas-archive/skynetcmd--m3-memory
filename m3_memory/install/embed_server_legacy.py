"""The retired m3-embed-server config.toml: where it was, and moving it into m3's files.

Cores before 3.10.16 kept the server's settings in a file of their own; the
server now reads m3_config.toml and the secrets file only (m3 ``9655f30a``) and
merely warns that the old file exists. ``migrate`` moves its values: settings to
m3_config.toml, ``token_sha256`` to the secret ``M3_EMBED_SERVER_TOKEN_SHA256``,
then renames the file to ``config.toml.migrated`` (inert, kept for a rollback
to an older core). A value m3_config.toml (or the secrets file) already holds
differently is reported and kept, and so is the old file, until the person
settles it. Idempotent: run by `m3 config import-env` (so `m3 upgrade`)
and `m3 embedder install`.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from m3_memory import _bin
from m3_memory._platform import is_macos, is_windows

_bin.ensure_on_path()

# The old file's [embed] field -> the m3 key that replaces it (m3-core-rs
# embed-server config.rs legacy_file_notice lists the same mapping).
FIELDS: dict[str, str] = {
    "gguf": "embed.gguf",
    "ctx": "embed.ctx",
    "host": "embed.server.host",
    "port": "embed.server.port",
    "streams": "embed.server.streams",
    "seq_max": "embed.server.seq_max",
    "n_batch": "embed.server.n_batch",
    "n_ubatch": "embed.server.n_ubatch",
    "coalesce_ms": "embed.server.coalesce_ms",
    "max_batch_tokens": "embed.server.max_batch_tokens",
    "remote_listen": "embed.server.remote_listen",
    "tls_cert": "embed.server.tls_cert",
    "tls_key": "embed.server.tls_key",
    "fips_mode": "crypto.fips_mode",
    "fips_strict": "crypto.fips_strict",
}
TOKEN_FIELD = "token_sha256"
TOKEN_SECRET = "M3_EMBED_SERVER_TOKEN_SHA256"


def legacy_server_config_path() -> Path:
    """Where cores before 3.10.16 kept the server's config.toml (m3-core-rs
    embed-server config.rs ``legacy_config_path``). The one copy in m3: used
    only to migrate and remove that file."""
    if is_windows():
        return Path(os.environ.get("PROGRAMDATA") or "C:\\ProgramData") / "m3-embed-server" / "config.toml"
    if is_macos():
        return Path.home() / "Library" / "Application Support" / "m3-embed-server" / "config.toml"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "m3-embed-server" / "config.toml"


@dataclass(slots=True)
class Report:
    old: Path
    config: Path
    moved: list[str] = field(default_factory=list)  # "key = value" written to m3_config.toml
    secret_moved: bool = False
    same: list[str] = field(default_factory=list)  # keys already holding the old value
    ignored: list[str] = field(default_factory=list)  # fields the server never read
    conflicts: list[str] = field(default_factory=list)  # one line each, with its fix
    errors: list[str] = field(default_factory=list)  # nothing could be decided or written
    removed: bool = False
    kept_copy: Path | None = None  # where the retired file went (renamed, inert)
    written: bool = False  # the moved values reached m3's files
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors

    def lines(self) -> list[str]:
        out: list[str] = []
        verb = "would move" if self.dry_run else "moved"
        if self.moved:
            out.append(f"  {verb} from {self.old} into {self.config}: {', '.join(self.moved)}")
        if self.ignored:
            out.append(f"  not moved (no server ever read them): {', '.join(self.ignored)}")
        if self.secret_moved:
            out.append(f"  {verb} its token_sha256 to the secret {TOKEN_SECRET} (the secrets file)")
        out += [f"  [!] {c}" for c in self.conflicts]
        out += [f"  [!] {e}" for e in self.errors]
        if self.removed:
            out.append(
                f"  {'would retire' if self.dry_run else 'retired'} {self.old}: m3-embed-server no longer reads it"
            )
            if self.kept_copy is not None:
                out.append(
                    f"  kept it as {self.kept_copy} (not read); a core before 3.10.16 needs it renamed back to "
                    f"{self.old.name}"
                )
        elif self.conflicts or (self.errors and not self.written):
            # Only when the old file still holds values m3 does not use; after a
            # successful write that failed only to rename, the values ARE in effect.
            out.append(
                f"  kept {self.old} until the above is settled; m3-embed-server does not read it, so its values "
                "are NOT in effect. Re-run `m3 config import-env` afterwards."
            )
        return out


def _shown(value: object) -> str:
    if isinstance(value, Path):
        return f'"{value.as_posix()}"'
    if isinstance(value, bool):
        return "true" if value else "false"
    return f'"{value}"' if isinstance(value, str) else str(value)


def _same(a: object, b: object) -> bool:
    if isinstance(a, Path) and isinstance(b, Path):
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    return a == b


def migrate(*, config: str | None = None, dry_run: bool = False, old: Path | None = None) -> Report | None:
    """Move the old file's values into m3's files and retire it (renamed). None when there
    is no old file. Never raises: every failure is a line of the report."""
    from m3_core.config import REGISTRY, locate
    from m3_core.config.keys_embed import EMBED_SERVER_M3_WIDE
    from m3_core.config.loader import ConfigFileError, format_config_error
    from m3_core.config.registry import ConfigValueError
    from m3_core.config.resolve import ConfigEnvErrors
    from m3_core.config.settings import build
    from m3_core.config.writer import ConfigWriteError, set_values
    from m3_core.secret_store import SecretsFileError, get_secret, set_secret

    old = old or legacy_server_config_path()
    if not os.path.isfile(old):  # Path.is_file raises PermissionError on 3.12/3.13
        return None
    loc = locate(config)
    rep = Report(old=old, config=loc.file, dry_run=dry_run)
    try:
        table = tomllib.loads(old.read_text(encoding="utf-8")).get("embed", {})
        if not isinstance(table, dict):
            raise ValueError(f"[embed] is a {type(table).__name__}, not a table")
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError) as e:
        rep.errors.append(f"could not read {old} ({type(e).__name__}: {e}); nothing was moved")
        return rep
    try:
        current = build(loc)
    except (ConfigFileError, ConfigEnvErrors) as e:
        rep.errors.append(f"{loc.file} is invalid, so nothing was moved from {old}:\n{format_config_error(e)}")
        return rep

    to_write = {}
    for name, raw in table.items():
        if name == TOKEN_FIELD:
            continue
        key_name = FIELDS.get(name)
        key = REGISTRY.get(key_name) if key_name else None
        if key is None:
            rep.ignored.append(name)
            continue
        try:
            value = key.coerce_file_value(raw)
        except ConfigValueError as e:
            rep.errors.append(
                f"{old}: [embed] {name} = {raw!r} is not a valid {key_name} ({e}); fix or remove that line"
            )
            continue
        origin = current.origin(key)
        in_file = origin.startswith("file:")
        if _same(current[key], value):
            if in_file:
                rep.same.append(key.name)
            else:
                to_write[key] = value
                rep.moved.append(f"{key.name} = {_shown(value)}")
        elif origin == "default" and key_name not in EMBED_SERVER_M3_WIDE:
            to_write[key] = value
            rep.moved.append(f"{key.name} = {_shown(value)}")
        else:
            now = _shown(current[key])
            where = f"{loc.file} has {now}" if in_file else f"m3 runs with {now} ({origin})"
            scope = " It applies to every m3 process." if key_name in EMBED_SERVER_M3_WIDE else ""
            rep.conflicts.append(
                f"{old} has {key_name} = {_shown(value)}, but {where}; kept the latter.{scope} "
                f"To use the old value: m3 config set {key_name} {_shown(value)}"
            )

    token = table.get(TOKEN_FIELD)
    token_to_store: str | None = None
    if token is not None:
        try:
            stored = get_secret(TOKEN_SECRET)
        except SecretsFileError as e:
            rep.errors.append(f"could not read the secrets file to move token_sha256 ({e})")
        else:
            digest = str(token).strip().lower()
            if not stored:
                token_to_store = digest
            elif stored.strip().lower() != digest:
                # Neither digest is shown: a digest of a guessable token can be brute-forced offline.
                rep.conflicts.append(
                    f"{old}'s token_sha256 differs from the secret {TOKEN_SECRET} already stored; kept the stored "
                    "one. Clients holding the older token are refused; `m3 embedder secure --new-token` mints a "
                    "new one for all of them"
                )
    if rep.errors or dry_run:
        rep.secret_moved = token_to_store is not None
        rep.removed = dry_run and not (rep.errors or rep.conflicts)
        return rep
    try:
        if to_write:
            set_values(loc, to_write)
        if token_to_store is not None:
            set_secret(TOKEN_SECRET, token_to_store)
            rep.secret_moved = True
    except (OSError, ConfigWriteError, SecretsFileError) as e:
        rep.errors.append(f"could not write the moved values ({type(e).__name__}: {e}); {old} is unchanged")
        return rep
    rep.written = True
    if not rep.conflicts:
        # Renamed, not deleted: a core before 3.10.16 reads only config.toml, so
        # the copy is inert, but a rollback to one needs it back (renamed again).
        kept = migrated_copy_path(old)
        try:
            os.replace(old, kept)
            rep.removed = True
            rep.kept_copy = kept
        except OSError as e:
            if is_windows():
                fix = f'Move-Item "{old}" "{kept}"  (in an Administrator terminal)'
            else:
                fix = f'mv "{old}" "{kept}"'
            rep.errors.append(
                f"could not rename {old} to {kept} ({type(e).__name__}: {e}); its values are moved, and the "
                f"server ignores the old file either way. fix: {fix}"
            )
    return rep


def migrated_copy_path(old: Path) -> Path:
    """Where the retired file is kept after its values move (inert: no core reads it)."""
    return old.with_name(old.name + ".migrated")
