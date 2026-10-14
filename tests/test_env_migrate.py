"""Tests for the env-namespace migration helper (`m3 doctor` / `--fix`).

The refactor renamed 17 env vars (OLD -> M3_OLD, see bin/m3_core/paths.py::
DEPRECATED_ENV_RENAMES). getenv_compat() lets the old names keep working at
read time, but nothing previously MOVED a user's on-disk config. These tests
cover the detect/cure pair added to mirror `_dedupe_mcp_registration` exactly:
  - _deprecated_env_in_config(): read-only scan of config files for old names.
  - _migrate_env_names(apply=...): dry-run/apply cure, .bak backup, idempotent,
    conflict rule (old dropped when new already set), per-file fault isolation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from m3_memory import installer as I  # noqa: E402


def _mk(path: Path, servers: dict) -> None:
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


# ── Detect ────────────────────────────────────────────────────────────────
def test_detect_reports_old_names_in_env_block(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    _mk(
        settings,
        {
            "memory": {
                "command": "py",
                "env": {
                    "CHROMA_BASE_URL": "http://x",
                    "PG_URL": "postgres://y",
                },
            }
        },
    )
    monkeypatch.setattr(I, "_client_config_sources", lambda: {"Claude Code": [settings]})

    found = I._deprecated_env_in_config()
    assert settings in found
    assert found[settings] == {
        "CHROMA_BASE_URL": "M3_CHROMA_BASE_URL",
        "PG_URL": "M3_CDW_PG_URL",  # split by role: PG_URL -> warehouse var
    }


def test_detect_clean_config_returns_empty(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    _mk(settings, {"memory": {"command": "py", "env": {"M3_CHROMA_BASE_URL": "http://x"}}})
    monkeypatch.setattr(I, "_client_config_sources", lambda: {"Claude Code": [settings]})
    assert I._deprecated_env_in_config() == {}


def test_detect_malformed_json_skipped_not_raised(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    bad = tmp_path / ".mcp.json"
    _mk(settings, {"memory": {"command": "py", "env": {"CHROMA_BASE_URL": "http://x"}}})
    bad.write_text("{ not valid json", encoding="utf-8")
    monkeypatch.setattr(I, "_client_config_sources", lambda: {"Claude Code": [settings, bad]})
    found = I._deprecated_env_in_config()
    assert settings in found
    assert bad not in found  # unreadable file skipped, no raise


# ── Cure: dry-run ─────────────────────────────────────────────────────────
def test_migrate_dry_run_lists_renames_without_writing(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    _mk(settings, {"memory": {"command": "py", "env": {"CHROMA_BASE_URL": "http://x"}}})
    monkeypatch.setattr(I, "_client_config_sources", lambda: {"Claude Code": [settings]})
    before = settings.read_text()

    actions = I._migrate_env_names(apply=False)
    assert any("CHROMA_BASE_URL -> M3_CHROMA_BASE_URL" in a for a in actions)
    assert settings.read_text() == before, "dry-run must not modify the file"
    assert not settings.with_suffix(settings.suffix + ".bak").exists()


# ── Cure: apply ─────────────────────────────────────────────────────────────
def test_migrate_apply_rewrites_preserves_value_writes_backup(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    _mk(settings, {"memory": {"command": "py", "env": {"CHROMA_BASE_URL": "http://x:8000"}}})
    monkeypatch.setattr(I, "_client_config_sources", lambda: {"Claude Code": [settings]})
    original = settings.read_text()

    actions = I._migrate_env_names(apply=True)
    assert any("renamed CHROMA_BASE_URL -> M3_CHROMA_BASE_URL" in a for a in actions)

    data = json.loads(settings.read_text())
    env = data["mcpServers"]["memory"]["env"]
    assert env.get("M3_CHROMA_BASE_URL") == "http://x:8000"
    assert "CHROMA_BASE_URL" not in env

    backup = settings.with_suffix(settings.suffix + ".bak")
    assert backup.is_file()
    assert backup.read_text() == original

    # Idempotent: running again on the now-clean file yields no actions.
    assert I._migrate_env_names(apply=True) == []


def test_migrate_apply_is_idempotent_second_run_empty(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    _mk(settings, {"memory": {"command": "py", "env": {"PG_URL": "postgres://z"}}})
    monkeypatch.setattr(I, "_client_config_sources", lambda: {"Claude Code": [settings]})
    assert I._migrate_env_names(apply=True) != []
    assert I._migrate_env_names(apply=True) == []


# ── Conflict rule ─────────────────────────────────────────────────────────
def test_migrate_conflict_drops_old_keeps_new_value(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    _mk(
        settings,
        {
            "memory": {
                "command": "py",
                "env": {
                    "CHROMA_BASE_URL": "http://old",
                    "M3_CHROMA_BASE_URL": "http://new",
                },
            }
        },
    )
    monkeypatch.setattr(I, "_client_config_sources", lambda: {"Claude Code": [settings]})

    actions = I._migrate_env_names(apply=True)
    assert any("dropped superseded CHROMA_BASE_URL" in a for a in actions)

    env = json.loads(settings.read_text())["mcpServers"]["memory"]["env"]
    assert env == {"M3_CHROMA_BASE_URL": "http://new"}  # new value untouched, old gone

    # Idempotent after conflict resolution too.
    assert I._migrate_env_names(apply=True) == []


# ── Fault isolation ─────────────────────────────────────────────────────────
def test_migrate_one_bad_file_does_not_abort_others(tmp_path, monkeypatch):
    good = tmp_path / "settings.json"
    bad = tmp_path / ".mcp.json"
    _mk(good, {"memory": {"command": "py", "env": {"PG_URL": "postgres://z"}}})
    bad.write_text("{ not valid json", encoding="utf-8")
    monkeypatch.setattr(I, "_client_config_sources", lambda: {"Claude Code": [good, bad]})

    # bad file is dropped by the detector itself (unparseable -> skipped),
    # so only the good file's rename should appear, no raise anywhere.
    actions = I._migrate_env_names(apply=True)
    assert any("PG_URL -> M3_CDW_PG_URL" in a for a in actions)
    env = json.loads(good.read_text())["mcpServers"]["memory"]["env"]
    assert env == {"M3_CDW_PG_URL": "postgres://z"}


# ── .env and ~/.claude.json: reported, never rewritten ──────────────────────────────────────────────
def test_dotenv_is_reported_with_the_edit_and_never_rewritten(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    original = "# a comment\nCHROMA_BASE_URL=http://x:8000\nUNRELATED=keepme\n"
    dotenv.write_text(original, encoding="utf-8")
    monkeypatch.setattr(I, "_client_config_sources", lambda: {})
    monkeypatch.chdir(tmp_path)

    assert I._deprecated_env_in_config() == {dotenv: {"CHROMA_BASE_URL": "M3_CHROMA_BASE_URL"}}
    actions = I._migrate_env_names(apply=True)
    assert any(str(dotenv) in a and "rename CHROMA_BASE_URL to M3_CHROMA_BASE_URL by hand" in a for a in actions), (
        actions
    )
    assert dotenv.read_text(encoding="utf-8") == original
    assert not dotenv.with_name(".env.bak").exists() and not dotenv.with_name(".env.backup_history").exists()
    assert I._migrate_env_names(apply=True) == actions, "still reported until the user edits it"


def test_claude_json_is_reported_not_rewritten(tmp_path, monkeypatch):
    from m3_memory.install import env_sources

    claude = env_sources.claude_user_config()
    original = json.dumps({"mcpServers": {"m3_memory": {"env": {"CHROMA_BASE_URL": "http://x"}}}})
    claude.write_text(original, encoding="utf-8")
    monkeypatch.setattr(I, "_client_config_sources", lambda: {})
    monkeypatch.chdir(tmp_path)

    actions = I._migrate_env_names(apply=True)
    assert any(str(claude) in a and "claude mcp remove" in a for a in actions), actions
    assert claude.read_text(encoding="utf-8") == original


def test_a_json_rename_leaves_the_active_backup_and_a_history_section(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    _mk(settings, {"memory": {"command": "py", "env": {"CHROMA_BASE_URL": "http://x"}}})
    monkeypatch.setattr(I, "_client_config_sources", lambda: {"Claude Code": [settings]})
    monkeypatch.chdir(tmp_path)

    I._migrate_env_names(apply=True)
    assert "CHROMA_BASE_URL" in settings.with_name("settings.json.bak").read_text(encoding="utf-8")
    history = settings.with_name("settings.json.backup_history").read_text(encoding="utf-8")
    assert "by m3 doctor --fix (env names)" in history and "END rewrite (ok)" in history


# ── Section renders without error ────────────────────────────────────────
def test_section_renders_without_error_when_clean(monkeypatch):
    monkeypatch.setattr(I, "_deprecated_env_in_config", lambda: {})
    I._deprecated_env_config_section()  # no exception = pass


def test_section_renders_without_error_when_dirty(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(I, "_deprecated_env_in_config", lambda: {settings: {"PG_URL": "M3_CDW_PG_URL"}})
    I._deprecated_env_config_section()  # no exception = pass
