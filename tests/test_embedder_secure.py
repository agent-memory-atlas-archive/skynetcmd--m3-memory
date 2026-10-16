"""`m3 embedder secure`: the server gets only the token's SHA-256, the token is
stored only after the server accepted the settings, and a stored token is reused."""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

from m3_memory import embedder_admin as ea  # noqa: E402


@pytest.fixture
def world(monkeypatch, tmp_path):
    import m3_core.secret_store as ss

    secrets: dict[str, str] = {}
    calls: list[tuple[str, ...]] = []
    state = {"rc": 0}
    monkeypatch.setattr(ea, "_binary_and_gguf_or_fail", lambda: (tmp_path / "m3-embed-server", tmp_path / "m.gguf"))

    def service(binary, gguf, sub, *extra):
        calls.append((sub, *extra))
        return state["rc"]

    monkeypatch.setattr(ea, "_service_cmd", service)
    monkeypatch.setattr(ss, "get_secret", lambda n: secrets.get(n))
    monkeypatch.setattr(ss, "set_secret", lambda n, v: secrets.__setitem__(n, v))
    return secrets, calls, state


def _args(**kw) -> argparse.Namespace:
    base = {"listen": "100.100.50.63", "cert": "c.pem", "key": "k.pem", "new_token": False, "off": False}
    return argparse.Namespace(**{**base, **kw})


def _sent_digest(calls) -> str:
    sub, *extra = calls[-1]
    assert sub == "secure"
    return extra[extra.index("--token-sha256") + 1]


def test_a_new_token_is_minted_and_only_its_digest_reaches_the_server(world, capsys):
    secrets, calls, _ = world
    assert ea.cmd_secure(_args()) == 0
    token = secrets["M3_EMBED_SERVER_TOKEN"]
    assert len(token) >= 32
    assert _sent_digest(calls) == hashlib.sha256(token.encode()).hexdigest()
    assert token not in " ".join(calls[-1]), "the token itself must never be on the server's command line"
    assert "--listen" in calls[-1] and "100.100.50.63" in calls[-1]
    assert "tailscale cert" in capsys.readouterr().out


def test_nothing_is_stored_when_the_server_refuses(world):
    secrets, _, state = world
    state["rc"] = 1
    assert ea.cmd_secure(_args()) == 1
    assert secrets == {}


def test_a_stored_token_is_reused_unless_asked(world):
    secrets, calls, _ = world
    secrets["M3_EMBED_SERVER_TOKEN"] = "x" * 43
    assert ea.cmd_secure(_args()) == 0
    assert secrets["M3_EMBED_SERVER_TOKEN"] == "x" * 43
    assert _sent_digest(calls) == hashlib.sha256(b"x" * 43).hexdigest()
    assert ea.cmd_secure(_args(new_token=True)) == 0
    assert secrets["M3_EMBED_SERVER_TOKEN"] != "x" * 43


def test_missing_arguments_and_off(world, capsys):
    _, calls, _ = world
    assert ea.cmd_secure(_args(cert=None)) == 2
    assert "--cert" in capsys.readouterr().err
    assert ea.cmd_secure(_args(off=True)) == 0
    assert calls[-1] == ("secure", "--off")
