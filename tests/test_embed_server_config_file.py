"""m3-embed-server reads m3_config.toml and the secrets file:
the registry owns its keys, m3 hands it `--config <file>` and nothing through
env, records what a service needs before `install`, records `secure`'s settings
itself, migrates the retired per-server config.toml, and detects an older
binary (and an older in-process core) by capability.

Hermetic: no service manager, no real binary, no real secrets; the legacy file
locator points at the test's temp dir (conftest).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import logging
import pathlib
import subprocess
import sys
import tomllib
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

from m3_memory import embedder_admin as ea  # noqa: E402


def _config_file() -> pathlib.Path:
    from m3_memory import _roots

    return pathlib.Path(_roots.config_file())


def _file_table() -> dict:
    p = _config_file()
    return tomllib.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


# ── 1. the registry owns the server's keys ───────────────────────────────────
def test_every_key_the_server_reads_is_a_registry_key():
    from m3_core.config.keys import REGISTRY
    from m3_core.config.keys_embed import EMBED_SERVER_M3_WIDE, EMBED_SERVER_READS

    from m3_memory.install.embed_server_legacy import FIELDS

    missing = [n for n in (*EMBED_SERVER_READS, *FIELDS.values()) if REGISTRY.get(n) is None]
    assert not missing, missing
    assert set(FIELDS.values()) == set(EMBED_SERVER_READS)
    assert EMBED_SERVER_M3_WIDE <= set(EMBED_SERVER_READS)


def test_defaults_are_the_rust_defaults_and_derived_ones_are_unset():
    from m3_core.config.keys import REGISTRY

    expected = {
        "embed.streams": 8,
        "embed.seq_max": 32,
        "embed.coalesce_ms": 3,
        "embed.n_batch": None,
        "embed.n_ubatch": None,
        "embed.max_batch_tokens": None,
        "embed.server.streams": None,
        "embed.server.seq_max": 32,
        "embed.server.n_batch": None,  # follows embed.ctx (Rust 98036c4)
        "embed.server.n_ubatch": None,
        "embed.server.coalesce_ms": 3,
        "embed.server.max_batch_tokens": 2048,
        "embed.server.remote_listen": None,
        "embed.server.tls_cert": None,
        "embed.server.tls_key": None,
    }
    assert {n: REGISTRY.get(n).default for n in expected} == expected


def test_the_shared_legacy_names_alias_the_in_process_keys():
    """In an m3 process M3_EMBED_STREAMS & co. only ever reached the in-process
    core; the server reads them in its own environment, below the file."""
    from m3_core.config.keys import REGISTRY

    for leaf in ("streams", "seq_max", "n_batch", "n_ubatch", "coalesce_ms", "max_batch_tokens"):
        assert REGISTRY.by_env(f"M3_EMBED_{leaf.upper()}").name == f"embed.{leaf}"
        assert REGISTRY.get(f"embed.server.{leaf}").env_names == ()


def test_the_token_digest_is_a_registered_secret():
    from m3_core.config import KeyClass
    from m3_core.config.keys import REGISTRY
    from m3_core.secret_store import is_known_secret

    assert REGISTRY.get("M3_EMBED_SERVER_TOKEN_SHA256").klass is KeyClass.SECRET
    assert is_known_secret("M3_EMBED_SERVER_TOKEN_SHA256")


# ── 2. the command line: --config, never an env handoff ──────────────────────
@pytest.fixture
def ran(monkeypatch, tmp_path):
    calls: list[SimpleNamespace] = []

    def fake_run(argv, **kw):
        calls.append(SimpleNamespace(argv=list(argv), env=kw.get("env")))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(ea, "_ensure_executable", lambda b: None)
    monkeypatch.setattr(ea.subprocess, "run", fake_run)
    return calls


def test_the_server_gets_config_before_the_subcommand_and_no_env_handoff(ran, tmp_path):
    from m3_memory import _childenv

    binary = tmp_path / "m3-embed-server"
    assert ea._service_cmd(binary, tmp_path / "m.gguf", "start") == 0
    from m3_memory import _roots

    assert ran[-1].argv == [str(binary), "--config", _roots.config_file(), "start"]
    assert ran[-1].env == _childenv.child_env(), "m3 adds nothing to the server's environment"


def test_the_env_handoff_is_gone():
    import inspect

    from m3_memory import _childenv

    assert not hasattr(_childenv, "RUST_EMBED_SERVER") and not hasattr(_childenv, "Handoff")
    assert "handoff" not in inspect.signature(_childenv.child_env).parameters


def test_an_older_binary_never_gets_config(ran, tmp_path, monkeypatch, capsys):
    """An older server's --config means its own config.toml (and `secure`
    would WRITE to it): service verbs run without it, the rest is refused."""
    monkeypatch.setattr(ea, "_server_reads_m3_config", lambda b: False)
    binary = tmp_path / "m3-embed-server"
    assert ea._service_cmd(binary, tmp_path / "m.gguf", "status") == 0
    assert ran[-1].argv == [str(binary), "status"]
    assert ea._service_cmd(binary, tmp_path / "m.gguf", "install") == ea.EXIT_SERVER_TOO_OLD
    assert len(ran) == 1, "nothing may run for install on an older binary"
    err = capsys.readouterr().err
    assert "does not read m3_config.toml" in err and "3.10.16" in err and "fix:" in err


@pytest.mark.parametrize(
    "help_text, expected",
    [
        ("USAGE:\n  m3-embed-server [--config <m3_config.toml>] [SUBCOMMAND]", True),
        ("USAGE:\n  m3-embed-server [--config <path>] [SUBCOMMAND]\n--config uses that config.toml", False),
    ],
)
def test_the_capability_probe_reads_the_help_text(monkeypatch, tmp_path, help_text, expected):
    binary = tmp_path / f"srv-{expected}"
    binary.write_bytes(b"fake")
    monkeypatch.setattr(ea, "_ensure_executable", lambda b: None)
    monkeypatch.setattr(ea.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", help_text))
    assert ea._server_reads_m3_config_impl(binary) is expected


def test_the_capability_probe_says_unknown_when_help_cannot_run(monkeypatch, tmp_path):
    assert ea._server_reads_m3_config_impl(tmp_path / "absent") is None
    assert "absent" in next(iter(ea._READS_M3_CONFIG_ERROR))


# ── 3. install: the file holds what the service needs ────────────────────────
def test_install_records_the_model_and_values_held_only_outside_the_file(m3_settings, tmp_path, capsys):
    m3_settings({"embed.server.port": 8093, "embed.server.streams": 3})
    gguf = tmp_path / "bge.gguf"
    gguf.write_bytes(b"x")
    assert ea._prepare_service_config(gguf) is True
    server = _file_table()["embed"]
    assert pathlib.Path(server["gguf"]) == gguf
    assert server["server"]["port"] == 8093 and server["server"]["streams"] == 3
    assert "recorded for the service" in capsys.readouterr().out


def test_install_refuses_a_set_flag_and_an_env_only_crypto_value(m3_settings, tmp_path, capsys):
    from m3_core.config import settings as st

    before = _config_file().read_bytes() if _config_file().is_file() else None
    m3_settings({"crypto.fips_mode": True})
    st.set_from_cli({"embed.server.port": 8094})
    try:
        assert ea._prepare_service_config(tmp_path / "bge.gguf") is False
    finally:
        st._reset_for_tests()
    err = capsys.readouterr().err
    assert "m3 config set embed.server.port 8094" in err and "--set" in err
    assert "m3 config set crypto.fips_mode True" in err and "every m3 process" in err
    after = _config_file().read_bytes() if _config_file().is_file() else None
    assert after == before, "a refused install writes nothing"


def test_install_refuses_on_an_older_binary_before_touching_anything(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ea, "_server_binary", lambda: tmp_path / "m3-embed-server")
    monkeypatch.setattr(ea, "_locate_gguf_or_explain", lambda: tmp_path / "m.gguf")
    monkeypatch.setattr(ea, "_server_reads_m3_config", lambda b: False)
    monkeypatch.setattr(ea, "_service_cmd", lambda *a, **k: pytest.fail("ran the server"))
    assert ea.cmd_install(argparse.Namespace(concurrency=None)) == ea.EXIT_SERVER_TOO_OLD
    assert "does not read m3_config.toml" in capsys.readouterr().err


def test_a_running_service_registered_without_config_is_reregistered(monkeypatch, tmp_path):
    """Before: registered + running + current binary returned early, so a
    registration from an earlier core (no --config) was never converged."""
    calls: list[str] = []
    monkeypatch.setattr(ea, "_server_binary", lambda: tmp_path / "m3-embed-server")
    monkeypatch.setattr(ea, "_locate_gguf_or_explain", lambda: tmp_path / "m.gguf")
    monkeypatch.setattr(ea, "_prepare_service_config", lambda g: True)
    monkeypatch.setattr(ea, "_service_reports_installed", lambda *a: True)
    monkeypatch.setattr(ea, "_service_reports_running", lambda *a: True)
    monkeypatch.setattr(ea, "_service_binary_is_stale", lambda b: False)
    monkeypatch.setattr(ea, "_registered_port", lambda: None)
    monkeypatch.setattr(ea, "_stop_own_stray_server", lambda p: [])
    monkeypatch.setattr(ea, "_warn_if_port_busy", lambda a: None)
    monkeypatch.setattr(ea, "_pin_registered_port", lambda: None)
    monkeypatch.setattr(ea, "_verify_started_or_explain", lambda *a, **k: 0)
    line = {"v": r"C:\py\m3_core_rs\m3-embed-server.exe run-as-service"}
    monkeypatch.setattr(ea, "_registered_command_line", lambda: line["v"])

    def svc(binary, gguf, sub, *extra):
        calls.append(sub)
        if sub == "install":
            line["v"] = rf"C:\py\m3_core_rs\m3-embed-server.exe --config {_config_file()} run-as-service"
        return 0

    monkeypatch.setattr(ea, "_service_cmd", svc)
    monkeypatch.setattr(ea, "_service_cmd_quiet", svc)
    assert ea.cmd_install(argparse.Namespace(concurrency=None)) == 0
    assert "install" in calls


def test_doctor_names_a_registration_without_config_and_its_fix(monkeypatch, tmp_path):
    monkeypatch.setattr(ea, "_server_binary", lambda: tmp_path / "m3-embed-server")
    monkeypatch.setattr(ea, "_registered_command_line", lambda: "m3-embed-server.exe run-as-service")
    msg = ea.registration_lacks_config_message()
    assert msg and "without --config" in msg and "m3 embedder install" in msg
    monkeypatch.setattr(ea, "_registered_command_line", lambda: "m3-embed-server.exe --config C:/c.toml run-as-service")
    assert ea.registration_lacks_config_message() is None
    monkeypatch.setattr(ea, "_registered_command_line", lambda: None)
    assert ea.registration_lacks_config_message() is None, "unknown is not a problem"


def test_the_windows_command_line_is_read_from_sc_qc(monkeypatch):
    sched = importlib.import_module("install_schedules")
    out = "SERVICE_NAME: m3-embed-server\n        BINARY_PATH_NAME   : C:\\m3\\m3-embed-server.exe --config C:\\c\\m3_config.toml run-as-service\n"
    monkeypatch.setattr(sched, "_os_name", lambda: "Windows")
    monkeypatch.setattr(sched, "_run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, out, ""))
    assert sched.registered_embed_command_line().endswith("--config C:\\c\\m3_config.toml run-as-service")
    monkeypatch.setattr(sched, "_run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1060, "", ""))
    assert sched.registered_embed_command_line() is None


def test_a_config_registration_carries_no_port_to_move(monkeypatch, tmp_path):
    sched = importlib.import_module("install_schedules")
    unit = tmp_path / "systemd" / "user" / "m3-embed-server.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\nExecStart=/x/m3-embed-server --config /c/m3_config.toml\n", encoding="utf-8")
    monkeypatch.setattr(sched, "_os_name", lambda: "Linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert sched.registered_embed_port() is None
    assert sched.registered_embed_command_line() == "/x/m3-embed-server --config /c/m3_config.toml"


# ── 4. secure: the server checks, m3 records ─────────────────────────────────
@pytest.fixture
def secure_world(monkeypatch, tmp_path):
    import m3_core.secret_store as ss

    secrets: dict[str, str] = {}
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(ea, "_binary_and_gguf_or_fail", lambda: (tmp_path / "m3-embed-server", tmp_path / "m.gguf"))
    monkeypatch.setattr(ea, "_service_cmd", lambda b, g, sub, *e: calls.append((sub, *e)) or 0)
    monkeypatch.setattr(ss, "get_secret", lambda n: secrets.get(n))
    monkeypatch.setattr(ss, "set_secret", lambda n, v: secrets.__setitem__(n, v))
    monkeypatch.setattr(ss, "delete_secret", lambda n: secrets.pop(n, None) is not None)
    return secrets, calls


def test_secure_checks_then_records_the_listener_and_the_digest(secure_world, capsys):
    secrets, calls = secure_world
    args = argparse.Namespace(listen="100.100.50.63", cert="c.pem", key="k.pem", new_token=False, off=False)
    assert ea.cmd_secure(args) == 0
    sub, *extra = calls[-1]
    assert sub == "secure" and extra[0] == "--check"
    token = secrets["M3_EMBED_SERVER_TOKEN"]
    digest = hashlib.sha256(token.encode()).hexdigest()
    assert secrets["M3_EMBED_SERVER_TOKEN_SHA256"] == digest
    server = _file_table()["embed"]["server"]
    assert server["remote_listen"] == "100.100.50.63"
    assert server["tls_cert"].endswith("c.pem") and server["tls_key"].endswith("k.pem")
    assert digest not in capsys.readouterr().out, "the digest is never printed"


def test_secure_off_unsets_the_keys_and_the_digest_without_the_server(secure_world, capsys):
    secrets, calls = secure_world
    secrets["M3_EMBED_SERVER_TOKEN_SHA256"] = "ab" * 32
    ea._config.write({"embed.server.remote_listen": "100.1.1.1", "embed.server.tls_cert": pathlib.Path("c.pem")})
    args = argparse.Namespace(listen=None, cert=None, key=None, new_token=False, off=True)
    assert ea.cmd_secure(args) == 0
    assert calls == [], "--off needs no server: it writes nothing any more"
    assert "server" not in _file_table().get("embed", {}) or not _file_table()["embed"]["server"]
    assert "M3_EMBED_SERVER_TOKEN_SHA256" not in secrets


def test_delete_secret_removes_one_entry_and_keeps_the_rest():
    from m3_core.secret_store import delete_secret, get_secret, set_secret

    set_secret("M3_EMBED_SERVER_TOKEN_SHA256", "ab" * 32)
    set_secret("M3_SERVE_TOKEN", "keep")
    assert delete_secret("M3_EMBED_SERVER_TOKEN_SHA256") is True
    assert get_secret("M3_EMBED_SERVER_TOKEN_SHA256") is None and get_secret("M3_SERVE_TOKEN") == "keep"
    assert delete_secret("M3_EMBED_SERVER_TOKEN_SHA256") is False


# ── 5. the retired per-server config.toml ────────────────────────────────────
def _legacy(text: str) -> pathlib.Path:
    from m3_memory.install import embed_server_legacy

    p = embed_server_legacy.legacy_server_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_the_legacy_path_has_one_owner():
    """The per-OS path of the retired file lives in one function (the conftest
    replaces it per test, so the source is what is checked)."""
    root = pathlib.Path(__file__).resolve().parents[1]
    owner = root / "m3_memory" / "install" / "embed_server_legacy.py"
    needle = '"m3-embed-server" / "config.toml"'
    assert owner.read_text(encoding="utf-8").count(needle) == 3  # Windows, macOS, Linux
    others = [
        p
        for base in (root / "m3_memory", root / "bin")
        for p in base.rglob("*.py")
        if p != owner and needle in p.read_text(encoding="utf-8", errors="replace")
    ]
    assert not others, others


def test_migrate_moves_values_and_the_token_then_removes_the_file(tmp_path):
    import m3_core.secret_store as ss

    from m3_memory.install.embed_server_legacy import migrate

    gguf = (tmp_path / "bge.gguf").as_posix()
    digest = "cd" * 32
    old = _legacy(f'[embed]\ngguf = "{gguf}"\nport = 8093\nstreams = 2\nn_threads = 4\ntoken_sha256 = "{digest}"\n')
    original = old.read_bytes()
    rep = migrate()
    assert rep is not None and rep.ok and rep.removed, rep.lines()
    table = _file_table()["embed"]
    assert table["gguf"] == gguf and table["server"]["port"] == 8093 and table["server"]["streams"] == 2
    assert ss.get_secret("M3_EMBED_SERVER_TOKEN_SHA256") == digest
    assert not old.exists()
    kept = old.with_name("config.toml.migrated")
    assert rep.kept_copy == kept and kept.read_bytes() == original, "kept, byte for byte, for a rollback"
    text = "\n".join(rep.lines())
    assert digest not in text and "n_threads" in text and str(kept) in text
    assert migrate() is None, "idempotent: nothing left to move"


def test_a_failed_rename_says_rename_and_never_suggests_deleting(monkeypatch):
    import m3_memory.install.embed_server_legacy as legacy

    old = _legacy("[embed]\nport = 8096\n")

    real_replace = legacy.os.replace

    def refuse(src, dst):  # only the retired file's rename; atomic_write also uses os.replace
        if pathlib.Path(src) == old:
            raise PermissionError(13, "Access is denied")
        return real_replace(src, dst)

    monkeypatch.setattr(legacy.os, "replace", refuse)
    rep = legacy.migrate()
    text = "\n".join(rep.lines())
    kept = str(old.with_name("config.toml.migrated"))
    assert not rep.ok and old.exists() and not rep.removed, text
    assert "could not rename" in text and kept in text and "PermissionError" in text, text
    assert "Remove-Item" not in text and "rm " not in text, "the copy is the rollback: never suggest deleting it"
    assert "NOT in effect" not in text, "the values were written; only the rename failed"
    assert _file_table()["embed"]["server"]["port"] == 8096


def test_migrate_reports_a_differing_value_and_keeps_the_file(tmp_path):
    from m3_memory.install.embed_server_legacy import migrate

    ea._config.write({"embed.server.port": 8094})
    old = _legacy("[embed]\nport = 8093\nfips_mode = true\n")
    rep = migrate()
    text = "\n".join(rep.lines())
    assert _file_table()["embed"]["server"]["port"] == 8094, "a value already in the file is not overwritten"
    assert not _file_table().get("crypto"), "an m3-wide crypto setting is never switched by a migration"
    assert "m3 config set embed.server.port 8093" in text and "m3 config set crypto.fips_mode true" in text
    assert old.exists() and not rep.removed and "kept" in text


def test_a_differing_token_digest_is_reported_without_either_value(tmp_path):
    import m3_core.secret_store as ss

    from m3_memory.install.embed_server_legacy import migrate

    ss.set_secret("M3_EMBED_SERVER_TOKEN_SHA256", "aa" * 32)
    _legacy(f'[embed]\ntoken_sha256 = "{"bb" * 32}"\n')
    text = "\n".join(migrate().lines())
    assert "differs" in text and "aa" * 32 not in text and "bb" * 32 not in text
    assert ss.get_secret("M3_EMBED_SERVER_TOKEN_SHA256") == "aa" * 32


def test_import_env_runs_the_migration(capsys):
    from m3_memory.install import import_env

    old = _legacy("[embed]\nport = 8095\n")
    assert import_env.run(None, dry_run=False, non_interactive=True) == 0
    assert not old.exists() and _file_table()["embed"]["server"]["port"] == 8095
    assert "retired m3-embed-server config" in capsys.readouterr().out


# ── 6. the in-process embedder: parameters, and an older core ────────────────
def test_the_registry_values_reach_the_core_as_parameters(m3_settings):
    embed = importlib.import_module("memory.embed")
    m3_settings({"embed.streams": 3, "embed.max_batch_tokens": 4096})
    seen = {}

    class Core:
        class EmbeddedEmbedder:
            def __init__(self, path, warmup=None, **kw):
                seen.update(kw, path=path)

    embed.new_embedded_embedder(Core, "m.gguf")
    assert seen == {
        "path": "m.gguf",
        "streams": 3,
        "ctx": 8192,
        "seq_max": 32,
        "coalesce_ms": 3,
        "max_batch_tokens": 4096,
    }


def test_an_older_core_is_built_without_them_and_a_dropped_value_is_said(m3_settings, monkeypatch, caplog):
    embed = importlib.import_module("memory.embed")
    monkeypatch.setattr(embed, "_OLD_CORE_SAID", False)
    m3_settings({"embed.streams": 3})
    built = []

    class Core:
        __version__ = "3.10.7"

        class EmbeddedEmbedder:
            def __init__(self, path, warmup=None):
                built.append(path)

    with caplog.at_level(logging.INFO, logger=embed.logger.name):
        embed.new_embedded_embedder(Core, "m.gguf")
    assert built == ["m.gguf"]
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warned and "embed.streams" in warned[0].getMessage() and "3.10.7" in warned[0].getMessage()


def test_a_real_type_error_is_not_mistaken_for_an_older_core(m3_settings):
    embed = importlib.import_module("memory.embed")
    m3_settings({})

    class Core:
        class EmbeddedEmbedder:
            def __init__(self, path, **kw):
                raise TypeError("model_path must be str")

    with pytest.raises(TypeError, match="must be str"):
        embed.new_embedded_embedder(Core, "m.gguf")


# ── 7. the decoupled roots ───────────────────────────────────────────────────
def test_setup_memory_defaults_to_the_engine_root():
    src = (pathlib.Path(__file__).resolve().parents[1] / "bin" / "setup_memory.py").read_text(encoding="utf-8")
    assert '"memory" / "agent_memory.db"' not in src
    assert 'os.path.join(_roots.engine_root(), "agent_memory.db")' in src


def test_install_os_creates_the_engine_and_logs_roots():
    spec = importlib.util.spec_from_file_location(
        "_m3_install_os_roots", pathlib.Path(__file__).resolve().parents[1] / "install_os.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from m3_memory import _roots

    assert mod._state_dirs() == (_roots.engine_root(), _roots.logs_root())
    assert not hasattr(mod, "M3_ROOT"), "the ~/.m3-memory root is gone"
