"""The embed-server probe's verdict lines carry their cause.

A missing binary with nothing on the port is "optional" only when m3 does not
embed through that server; a failed status/doctor call names its exit code and
what the binary said.
"""

from __future__ import annotations

import os
import sys
import types

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
for _p in (os.path.join(_ROOT, "bin"), _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from doctor import embed_server_probe as esp  # noqa: E402
from m3_core.tier2_client import Diagnosis  # noqa: E402

_REFUSED = Diagnosis("refused", "nothing accepted the connection at http://127.0.0.1:8082", "`m3 embedder start`")


def _no_binary(monkeypatch, needed):
    monkeypatch.setattr(esp, "_resolve_binary", lambda: None)
    monkeypatch.setattr(esp, "_note_older_copy_on_path", lambda exe: None)
    monkeypatch.setattr(esp, "_port_answers", lambda timeout=2.0: _REFUSED)
    monkeypatch.setattr(esp, "_server_needed", lambda: needed)
    monkeypatch.setattr(esp, "_health_port", lambda: 8082)


def test_a_needed_server_that_is_missing_is_a_warning_not_optional(monkeypatch, capsys):
    _no_binary(monkeypatch, needed=True)
    assert esp.run(brief=True) == 1
    out = capsys.readouterr().out
    assert "optional" not in out and "nothing serves on :8082" in out
    assert "m3 embedder install" in out


def test_per_process_mode_keeps_optional(monkeypatch, capsys):
    _no_binary(monkeypatch, needed=False)
    assert esp.run(brief=True) == 0
    assert "not installed (optional)" in capsys.readouterr().out


def test_a_failed_subcommand_names_its_exit_code_and_output(monkeypatch, capsys):
    monkeypatch.setattr(esp, "_resolve_binary", lambda: "/venv/m3-embed-server")
    monkeypatch.setattr(esp, "_note_older_copy_on_path", lambda exe: None)

    def fake_run(argv, **kw):
        return types.SimpleNamespace(returncode=3, stdout="", stderr="model file not found: /m.gguf")

    monkeypatch.setattr(esp.subprocess, "run", fake_run)
    assert esp.run(brief=True) == 3
    out = capsys.readouterr().out
    assert "exited 3" in out and "model file not found" in out
