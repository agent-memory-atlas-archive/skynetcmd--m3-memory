"""Messages from `m3 embedder` and the embed servers say what is true on THIS
machine: the token is shown once, an unreadable service state is "unknown"
(never "not installed"), the last health observation is kept, and a server's
embed failure reaches the client as a structured 500.

Hermetic: no service manager, no network, no model, no real secrets.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

from m3_memory import embedder_admin as ea  # noqa: E402


# ── H16: the bearer token is printed only when minted ────────────────────────
@pytest.fixture
def secure_world(monkeypatch, tmp_path):
    import m3_core.secret_store as ss

    secrets: dict[str, str] = {}
    monkeypatch.setattr(ea, "_binary_and_gguf_or_fail", lambda: (tmp_path / "m3-embed-server", tmp_path / "m.gguf"))
    monkeypatch.setattr(ea, "_service_cmd", lambda *a, **k: 0)
    monkeypatch.setattr(ss, "get_secret", lambda n: secrets.get(n))
    monkeypatch.setattr(ss, "set_secret", lambda n, v: secrets.__setitem__(n, v))
    monkeypatch.setattr(ss, "secrets_file_path", lambda: tmp_path / "m3_secrets.toml")
    return secrets, tmp_path


def _secure_args(**kw) -> argparse.Namespace:
    base = {"listen": "100.100.50.63", "cert": "c.pem", "key": "k.pem", "new_token": False, "off": False}
    return argparse.Namespace(**{**base, **kw})


def test_a_minted_token_is_shown_once(secure_world, capsys):
    secrets, _ = secure_world
    assert ea.cmd_secure(_secure_args()) == 0
    assert secrets["M3_EMBED_SERVER_TOKEN"] in capsys.readouterr().out


def test_a_reused_token_is_not_printed_and_its_location_is_named(secure_world, capsys):
    secrets, tmp_path = secure_world
    secrets["M3_EMBED_SERVER_TOKEN"] = "reused-" + "x" * 40
    assert ea.cmd_secure(_secure_args()) == 0
    out = capsys.readouterr().out
    assert secrets["M3_EMBED_SERVER_TOKEN"] not in out
    assert "M3_EMBED_SERVER_TOKEN" in out and str(tmp_path / "m3_secrets.toml") in out
    assert "--new-token" in out


# ── M43: a status probe that cannot run is "unknown", not "not installed" ─────
def test_an_unrunnable_status_is_reported_unknown(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ea, "_STATUS_UNKNOWN_SAID", set())
    monkeypatch.setattr(ea.subprocess, "run", mock.Mock(side_effect=OSError("access denied")))
    assert ea._service_reports_installed(tmp_path / "bin", tmp_path / "m.gguf") is False
    assert ea._LAST_STATUS_UNKNOWN and "access denied" in ea._LAST_STATUS_UNKNOWN
    err = capsys.readouterr().err
    assert "UNKNOWN" in err and "access denied" in err


def test_a_status_that_ran_clears_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(ea, "_LAST_STATUS_UNKNOWN", "earlier failure")
    monkeypatch.setattr(ea.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="stopped\n", stderr=""))
    assert ea._service_reports_running(tmp_path / "bin", tmp_path / "m.gguf") is False
    assert ea._LAST_STATUS_UNKNOWN is None


def test_start_does_not_install_over_an_unknown_state(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ea, "_STATUS_UNKNOWN_SAID", set())
    monkeypatch.setattr(ea, "_binary_and_gguf_or_fail", lambda: (tmp_path / "bin", tmp_path / "m.gguf"))
    monkeypatch.setattr(ea.subprocess, "run", mock.Mock(side_effect=OSError("access denied")))
    installed = mock.Mock(return_value=0)
    monkeypatch.setattr(ea, "cmd_install", installed)
    assert ea.cmd_start(argparse.Namespace()) == 1
    installed.assert_not_called()
    assert "cannot tell whether" in capsys.readouterr().err


# ── M42: the last health observation survives the deadline ───────────────────
def test_wait_serving_reports_what_it_last_saw(monkeypatch, capsys):
    def _refuse(*a, **k):
        raise ConnectionRefusedError("refused")

    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(ea.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr("urllib.request.urlopen", _refuse)
    assert ea._wait_serving_impl(8082, timeout=30) is False
    err = capsys.readouterr().err
    assert "127.0.0.1:8082" in err and "refused" in err and "m3 embedder start" in err


# ── H15: "tier 1 keeps working" only where it does ───────────────────────────
def test_shared_mode_says_embedding_is_down(monkeypatch, tmp_path):
    cfg = tmp_path / ".embed_config.json"
    cfg.write_text(json.dumps({"disable_inproc_embedder": True}), encoding="utf-8")
    monkeypatch.setattr(ea, "_embed_config_path", lambda: str(cfg))
    assert "DOWN" in ea.tier1_availability()


def test_no_config_and_no_opt_in_says_embedding_is_down(monkeypatch, tmp_path, m3_settings):
    monkeypatch.setattr(ea, "_embed_config_path", lambda: str(tmp_path / "absent.json"))
    m3_settings({"embed.inproc": False})
    assert "DOWN" in ea.tier1_availability()


def test_opt_in_says_in_process_continues(monkeypatch, tmp_path, m3_settings):
    monkeypatch.setattr(ea, "_embed_config_path", lambda: str(tmp_path / "absent.json"))
    m3_settings({"embed.inproc": True})
    assert "in-process meanwhile" in ea.tier1_availability()


# ── M38: an embed failure is a structured 500, without the input ─────────────
def test_inproc_embed_failure_is_a_structured_500_without_the_input():
    from conftest import skip_reason

    pytest.importorskip("fastapi", reason=skip_reason("dep", "the embed server needs fastapi"))
    import embed_server_inproc as S
    from fastapi.testclient import TestClient

    async def _boom(texts, interactive=False):
        raise RuntimeError("embed pool mutex poisoned")

    saved = S._embed
    S._embed = _boom
    try:
        r = TestClient(S.app, raise_server_exceptions=False).post("/embedding", json={"input": ["secret memory text"]})
    finally:
        S._embed = saved
    assert r.status_code == 500
    err = r.json()["error"]
    assert err["code"] == "embed_failed" and "mutex poisoned" in err["message"] and err["inputs"] == 1
    assert "secret memory text" not in r.text
