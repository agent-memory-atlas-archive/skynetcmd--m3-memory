"""`m3 config import-env` (design 2026.10.12-IMPORT_ENV_DESIGN).
Every legacy source is read (env_sources.scan); a key is imported only when all
sources agree; on a disagreement a person at the console chooses, and with no
one there nothing is written for that key and every source is listed. The file is backed up (GuardedRewrite) and every
imported key must resolve from it afterwards, or the backup is restored."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A config file, a home with rc files, a cwd .env; no registry store, no
    process env for the names used, no one at the console."""
    from m3_memory import _platform, installer
    from m3_memory.install import env_sources, import_env

    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setattr(env_sources, "_home", lambda: home)
    monkeypatch.setattr(installer, "_client_config_sources", lambda: {})
    monkeypatch.setattr(_platform, "_winreg", None)
    monkeypatch.setattr(import_env, "console_has_person", lambda: False)
    monkeypatch.chdir(cwd)
    cfg = tmp_path / "config" / "m3_config.toml"
    cfg.parent.mkdir(exist_ok=True)  # the sandbox may have made it
    return {"home": home, "cwd": cwd, "cfg": cfg}


def _run(world, *extra):
    from m3_memory.install import import_env

    return import_env.main(["--config", str(world["cfg"]), *extra])


def _file(world):
    f = world["cfg"]
    return tomllib.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def _rc(world, text):
    (world["home"] / ".bashrc").write_text(text, encoding="utf-8")


def test_a_single_source_is_imported_in_its_toml_type(world, capsys):
    _rc(world, "export M3_SEARCH_ROW_CAP=700\n")
    assert _run(world) == 0
    assert _file(world)["search"]["row_cap"] == 700
    out = capsys.readouterr().out
    assert "imported search.row_cap = 700" in out and ".bashrc" in out


def test_dry_run_writes_nothing(world, capsys):
    _rc(world, "export M3_SEARCH_ROW_CAP=700\n")
    assert _run(world, "--dry-run") == 0
    assert not world["cfg"].exists()
    assert "would import search.row_cap = 700" in capsys.readouterr().out


def test_a_second_run_writes_nothing(world, capsys):
    _rc(world, "export M3_SEARCH_ROW_CAP=700\n")
    _run(world)
    before = world["cfg"].read_bytes()
    capsys.readouterr()
    assert _run(world) == 0
    assert world["cfg"].read_bytes() == before
    assert "nothing to import" in capsys.readouterr().out


def test_agreeing_sources_are_one_value(world):
    _rc(world, "export M3_SEARCH_ROW_CAP=700\n")
    (world["cwd"] / ".env").write_text("M3_SEARCH_ROW_CAP=700\n", encoding="utf-8")
    assert _run(world) == 0
    assert _file(world)["search"]["row_cap"] == 700


def test_a_disagreement_with_no_one_at_the_console_writes_nothing_and_names_every_source(world, capsys):
    _rc(world, "export M3_SEARCH_ROW_CAP=700\n")
    (world["cwd"] / ".env").write_text("M3_SEARCH_ROW_CAP=800\n", encoding="utf-8")
    assert _run(world) == 0
    assert "row_cap" not in _file(world).get("search", {})
    out = capsys.readouterr().out
    assert "search.row_cap" in out and "700" in out and "800" in out and ".bashrc" in out and ".env" in out
    assert "m3 config set search.row_cap" in out


def test_a_person_at_the_console_chooses(world, monkeypatch):
    from m3_memory.install import import_env

    _rc(world, "export M3_SEARCH_ROW_CAP=700\n")
    (world["cwd"] / ".env").write_text("M3_SEARCH_ROW_CAP=800\n", encoding="utf-8")
    monkeypatch.setattr(import_env, "console_has_person", lambda: True)
    answers = iter(["800"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert _run(world) == 0
    assert _file(world)["search"]["row_cap"] == 800


def test_a_key_already_in_the_file_is_left_alone(world, capsys):
    world["cfg"].write_text("schema_version = 1\n[search]\nrow_cap = 5\n", encoding="utf-8")
    _rc(world, "export M3_SEARCH_ROW_CAP=700\n")
    assert _run(world) == 0
    assert _file(world)["search"]["row_cap"] == 5
    assert "already set in" in capsys.readouterr().out


def test_an_unparseable_value_is_skipped_with_its_source(world, capsys):
    _rc(world, "export M3_SEARCH_ROW_CAP=lots\n")
    assert _run(world) == 0
    assert "row_cap" not in _file(world).get("search", {})
    out = capsys.readouterr().out
    assert "'lots'" in out and ".bashrc" in out


def _secrets():
    from m3_core.secret_store import read_secret_entries

    return read_secret_entries()


def test_a_secret_goes_to_the_secrets_file_never_the_config_or_stdout(world, capsys):
    """Slice 3: stored, read back, under the registry name (the
    M3_PG_URL alias stores as M3_PRIMARY_PG_URL); never shown, never in
    m3_config.toml or its backup."""
    _rc(world, 'export M3_PG_URL="postgresql://u:s3cret@h/db"\n')
    assert _run(world, "--dry-run") == 0
    assert _secrets() == {} and "would store M3_PRIMARY_PG_URL" in capsys.readouterr().out
    assert _run(world) == 0
    out = capsys.readouterr().out
    assert _secrets() == {"M3_PRIMARY_PG_URL": "postgresql://u:s3cret@h/db"}
    # --config X puts the secrets beside X, not beside the default config.
    assert (world["cfg"].parent / "m3_secrets.toml").is_file()
    assert "stored M3_PRIMARY_PG_URL" in out and ".bashrc" in out and "s3cret" not in out
    for f in world["cfg"].parent.glob("m3_config.toml*"):
        assert "s3cret" not in f.read_text(encoding="utf-8"), f
    assert _run(world) == 0 and "[=] M3_PRIMARY_PG_URL: listed in" in capsys.readouterr().out


def test_a_secret_only_in_this_process_is_listed_not_stored(world, capsys, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-process-only-1234567890")
    assert _run(world) == 0
    out = capsys.readouterr().out
    assert _secrets() == {}
    assert "ANTHROPIC_API_KEY: set only in this process's environment" in out and "sk-process" not in out


def test_another_tools_mcp_entry_is_never_a_source_but_m3s_own_is(world, capsys, monkeypatch):
    import json

    from m3_memory import installer

    cfg = world["home"] / "client.json"
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "other-tool": {"command": "npx", "env": {"OPENAI_API_KEY": "sk-other-tool-0123456789"}},
                    "memory": {"command": "m3", "env": {"GEMINI_API_KEY": "gm-m3s-own-0123456789"}},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(installer, "_client_config_sources", lambda: {"Client": [cfg]})
    assert _run(world) == 0
    out = capsys.readouterr().out
    assert _secrets() == {"GEMINI_API_KEY": "gm-m3s-own-0123456789"}
    assert "OPENAI_API_KEY in" in out and "belongs to another tool" in out and "sk-other" not in out


def test_a_secret_in_the_keyring_becomes_a_pointer(world, capsys, monkeypatch):
    import auth_utils
    from m3_core.secret_store import KeyringRef

    held = {"XAI_API_KEY": "xai-in-the-keyring-0123"}
    monkeypatch.setattr(auth_utils, "_keyring_tier", lambda k, *, service="system": (held.get(k), None))
    _rc(world, "export XAI_API_KEY=xai-a-legacy-copy-0123\n")
    assert _run(world) == 0
    out = capsys.readouterr().out
    assert _secrets() == {"XAI_API_KEY": KeyringRef("system", "XAI_API_KEY")}
    assert "now points at keyring system/XAI_API_KEY" in out and "legacy copies" in out and "xai-" not in out


def test_disagreeing_copies_store_nothing_without_a_person_and_masked_with_one(world, capsys, monkeypatch):
    from m3_memory.install import import_env

    _rc(world, "export OPENAI_API_KEY=sk-from-bashrc-0123456789\n")
    (world["cwd"] / ".env").write_text("OPENAI_API_KEY=sk-from-dotenv-0123456789\n", encoding="utf-8")
    assert _run(world) == 0
    out = capsys.readouterr().out
    assert _secrets() == {} and "disagree; nothing stored" in out and "m3 secret set OPENAI_API_KEY" in out
    assert "sk-from" not in out

    monkeypatch.setattr(import_env, "console_has_person", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "2")
    assert _run(world) == 0
    out = capsys.readouterr().out
    assert "sk-f***89" in out and "sk-from-" not in out, out  # masked choices only
    assert _secrets() == {"OPENAI_API_KEY": "sk-from-dotenv-0123456789"}  # sorted: bashrc, dotenv


def test_a_listed_secret_is_left_and_a_differing_copy_named(world, capsys):
    from m3_core.secret_store import set_secret

    set_secret("GEMINI_API_KEY", "gm-in-the-file-0123456789")
    _rc(world, "export GEMINI_API_KEY=gm-an-old-copy-0123456789\n")
    assert _run(world) == 0
    out = capsys.readouterr().out
    assert _secrets() == {"GEMINI_API_KEY": "gm-in-the-file-0123456789"}
    assert "copies that differ, which m3 no longer reads" in out and ".bashrc" in out


def test_an_existing_file_is_backed_up_with_a_history_section(world):
    world["cfg"].write_text("schema_version = 1\n", encoding="utf-8")
    _rc(world, "export M3_SEARCH_ROW_CAP=700\n")
    assert _run(world) == 0
    bak = world["cfg"].with_name("m3_config.toml.bak")
    hist = world["cfg"].with_name("m3_config.toml.backup_history")
    assert bak.read_text(encoding="utf-8") == "schema_version = 1\n"
    assert "by m3 config import-env" in hist.read_text(encoding="utf-8")
    assert "END rewrite (ok)" in hist.read_text(encoding="utf-8")


def test_the_llm_file_is_renamed_once_its_values_are_in_the_file(world):
    import json

    llm = world["cfg"].parent / ".llm_config.json"
    llm.write_text(json.dumps({"M3_ENABLE_OLLAMA_FAILOVER": "1"}), encoding="utf-8")
    from m3_memory import _roots

    _roots.pin_config_file(world["cfg"])
    assert _run(world) == 0
    assert _file(world)["llm"]["ollama_failover"] is True
    assert not llm.exists() and llm.with_name(".llm_config.json.imported").exists()


def test_the_llm_file_is_kept_while_a_value_is_not_imported(world):
    import json

    llm = world["cfg"].parent / ".llm_config.json"
    llm.write_text(json.dumps({"M3_ENABLE_OLLAMA_FAILOVER": "1"}), encoding="utf-8")
    _rc(world, "export M3_ENABLE_OLLAMA_FAILOVER=0\n")
    from m3_memory import _roots

    _roots.pin_config_file(world["cfg"])
    assert _run(world) == 0
    assert llm.exists(), "a disagreement left a value only there"


def test_a_write_that_does_not_resolve_is_rolled_back(world, monkeypatch, capsys):
    from m3_memory.install import import_env

    world["cfg"].write_text("schema_version = 1\n", encoding="utf-8")
    _rc(world, "export M3_SEARCH_ROW_CAP=700\n")
    monkeypatch.setattr(import_env, "_resolves_from_file", lambda settings, key, value: False)
    assert _run(world) == 2
    assert world["cfg"].read_text(encoding="utf-8") == "schema_version = 1\n"
    assert "restored" in capsys.readouterr().err
    history = world["cfg"].with_name("m3_config.toml.backup_history").read_text(encoding="utf-8")
    assert "ROLLED BACK" in history, "a rolled-back rewrite closes its section, so BEGIN alone means a crash"


def test_m3_config_import_env_dispatches(world, capsys, monkeypatch):
    """In-process through the real entry point (a subprocess would read the
    developer's real rc files, registry and ~/.claude.json)."""
    from m3_memory import cli

    _rc(world, "export M3_SEARCH_ROW_CAP=700\n")
    monkeypatch.setattr(sys, "argv", ["m3", "config", "import-env", "--dry-run", "--config", str(world["cfg"])])
    with pytest.raises(SystemExit) as done:
        cli.main()
    assert done.value.code == 0
    assert "would import search.row_cap = 700" in capsys.readouterr().out
    assert not world["cfg"].exists()


def test_m3_database_is_named_with_the_fix_and_never_moved(world, capsys):
    """M3_DATABASE design Q2: a database path is a locator, so import-env names
    where it is set and how to replace it, and writes nothing for it."""
    _rc(world, "export M3_DATABASE=/data/other.db\n")
    assert _run(world) == 0
    out = capsys.readouterr().out
    assert "M3_DATABASE is set in" in out and ".bashrc" in out and "--database PATH" in out and "--store NAME" in out
    assert "M3_DATABASE" not in (world["cfg"].read_text(encoding="utf-8") if world["cfg"].exists() else "")


def test_a_chatlog_store_name_is_named_with_the_fix_and_never_moved(world, capsys):
    """Chatlog design phase 2: the chatlog store's legacy env names are locators
    too; import-env names them with --chatlog-db / db_path and writes nothing."""
    _rc(world, "export CHATLOG_DB_PATH=/data/chat.db\n")
    assert _run(world) == 0
    out = capsys.readouterr().out
    assert (
        "CHATLOG_DB_PATH is set in" in out and "--chatlog-db PATH" in out and "db_path in .chatlog_config.json" in out
    )
    assert "CHATLOG_DB_PATH" not in (world["cfg"].read_text(encoding="utf-8") if world["cfg"].exists() else "")
