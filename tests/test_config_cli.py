"""`m3 config show` (bin/config_cli.py) and the [secrets].file key.

The CLI is a repair command: it must start on an invalid file and print every
problem (exit 2), never a traceback, and it never prints a secret's value.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from m3_core.secret_store import set_secret  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent
_CLI = _ROOT / "bin" / "config_cli.py"


def _cfg(monkeypatch) -> Path:
    import os

    root = Path(os.environ["M3_CONFIG_ROOT"])  # the sandbox's per-test root
    root.mkdir(parents=True, exist_ok=True)
    return root / "m3_config.toml"


def test_show_lists_values_and_origins(monkeypatch, capsys):
    import config_cli

    _cfg(monkeypatch).write_text('[db]\nbackend = "postgres"\n', encoding="utf-8")
    assert config_cli.main(["show", "--origin"]) == 0
    out = capsys.readouterr().out
    import re

    assert re.search(r'^db\.backend\s+= "postgres"', out, re.MULTILINE) and "[db].backend" in out
    assert "secrets (values are never shown; secrets file:" in out


def test_show_json_never_carries_a_secret_value(monkeypatch, capsys):
    import config_cli

    set_secret("M3_SERVE_TOKEN", "s3cret-serve-token")
    assert config_cli.main(["show", "--json"]) == 0
    raw = capsys.readouterr().out
    doc = json.loads(raw)
    serve = next(s for s in doc["secrets"] if s["name"] == "M3_SERVE_TOKEN")
    assert serve["set"] is True and serve["source"] == "secrets-file:M3_SERVE_TOKEN"
    assert "s3cret-serve-token" not in raw


def test_a_retired_name_that_is_set_is_said_to_have_no_effect(monkeypatch, capsys):
    """Retired settings are named, never silently ignored."""
    import config_cli

    monkeypatch.setenv("M3_FILES_DEFAULT_SCOPE", "agent")
    assert config_cli.main(["show"]) == 0
    out = capsys.readouterr().out
    assert "have NO effect" in out and "M3_FILES_DEFAULT_SCOPE: no reader" in out
    assert config_cli.main(["show", "--json"]) == 0
    assert "M3_FILES_DEFAULT_SCOPE" in json.loads(capsys.readouterr().out)["retired_env_set"]


def test_an_invalid_file_is_reported_in_full_with_exit_2(monkeypatch):
    """A subprocess: the real entry point, so a traceback would be visible."""
    import os

    p = _cfg(monkeypatch)
    p.write_text('[db]\nbakend = 1\n[secrets]\nM3_PRIMARY_PG_URL = "postgresql://u:hunter2@h/d"\n', encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(_CLI), "show"], capture_output=True, text=True, env={**os.environ}, timeout=60
    )
    assert r.returncode == 2, r.stderr
    assert "Traceback" not in r.stderr
    assert "2 invalid entries" in r.stderr and "did you mean [db].backend?" in r.stderr
    assert "hunter2" not in r.stderr + r.stdout


def test_m3_config_dispatches_to_the_bin_script(monkeypatch):
    from m3_memory import cli

    seen = {}
    monkeypatch.setattr(cli, "_resolve_bin_script", lambda name: True)
    monkeypatch.setattr(cli, "_run_bin_script", lambda name, argv: seen.update(name=name, argv=argv) or 0)
    ns = type("A", (), {"config_cmd": "show", "origin": True, "json": False, "config": "/x/c.toml"})()
    assert cli._cmd_config(ns) == 0
    assert seen == {"name": "config_cli.py", "argv": ["show", "--origin", "--config", "/x/c.toml"]}


# ── [secrets].file ───────────────────────────────────────────────────────────
def test_the_secrets_file_defaults_to_the_config_root(monkeypatch):
    from m3_core.secret_store import secrets_file_path

    assert secrets_file_path() == _cfg(monkeypatch).parent / "m3_secrets.toml"


def test_a_configured_secrets_file_is_used(monkeypatch, tmp_path):
    from m3_core.secret_store import get_secret, secrets_file_path, set_secret

    target = tmp_path / "elsewhere" / "s.toml"
    target.parent.mkdir()
    _cfg(monkeypatch).write_text(f'[secrets]\nfile = "{target.as_posix()}"\n', encoding="utf-8")
    assert secrets_file_path() == target
    set_secret("M3_SERVE_TOKEN", "1")
    assert target.is_file() and get_secret("M3_SERVE_TOKEN") == "1"


def test_a_relative_secrets_file_is_relative_to_the_config_root(monkeypatch):
    from m3_core.secret_store import secrets_file_path

    cfg = _cfg(monkeypatch)
    cfg.write_text('[secrets]\nfile = "private/s.toml"\n', encoding="utf-8")
    assert secrets_file_path() == cfg.parent / "private" / "s.toml"


def test_an_invalid_config_file_refuses_to_guess_the_secrets_file(monkeypatch):
    from m3_core.secret_store import SecretsFileError, secrets_file_path

    _cfg(monkeypatch).write_text("[secrets]\nfile = 7\n", encoding="utf-8")
    with pytest.raises(SecretsFileError, match=r"cannot be located[\s\S]*\[secrets\]\.file"):
        secrets_file_path()
