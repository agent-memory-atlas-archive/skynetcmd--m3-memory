"""The generated [embed] file for m3-embed-server (CONFIG_DESIGN §6; plan 2C.1's
round-trip guard).

The Rust server drops unknown [embed] keys silently (serde without
deny_unknown_fields), so the generator may emit only the server's own fields,
and every value must read back exactly.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

_RUST_CONFIG = Path(__file__).resolve().parents[2] / "m3-core-rs" / "crates" / "m3-embed-server" / "src" / "config.rs"


def _settings(tmp_path, text=None, environ=None):
    from m3_core.config import Location
    from m3_core.config.settings import build

    p = tmp_path / "m3_config.toml"
    if text is not None:
        p.write_text(text, encoding="utf-8")
    return build(Location(file=p, root=tmp_path, tier="t"), environ=environ or {})


def test_values_round_trip_through_toml(tmp_path):
    from m3_core.config import embed_file

    s = _settings(tmp_path, '[embed]\ngguf = \'C:\\models\\bge "m3".gguf\'\nctx = 4096\n'
                            '[embed.server]\nhost = "0.0.0.0"\nport = 9100\n')
    values = embed_file.server_values(s)
    parsed = tomllib.loads(embed_file.render(values))["embed"]
    assert parsed == {k: v for k, v in values.items() if v is not None}
    assert parsed["gguf"] == str(Path('C:\\models\\bge "m3".gguf').expanduser())
    assert (parsed["host"], parsed["port"], parsed["ctx"]) == ("0.0.0.0", 9100, 4096)


def test_unset_values_are_omitted_so_the_server_keeps_its_default(tmp_path):
    from m3_core.config import embed_file

    parsed = tomllib.loads(embed_file.render(embed_file.server_values(_settings(tmp_path))))["embed"]
    assert "gguf" not in parsed and parsed["port"] == 8082


def test_a_field_the_server_does_not_read_is_refused():
    from m3_core.config import embed_file

    with pytest.raises(ValueError, match="silently ignore n_threads"):
        embed_file.render({"port": 8082, "n_threads": 4})


def test_out_of_range_values_never_reach_the_server(tmp_path):
    """port is a u16 and ctx a u32 in Rust: refuse here, not at the server's parse."""
    from m3_core.config import ConfigFileError

    with pytest.raises(ConfigFileError):
        _settings(tmp_path, "[embed.server]\nport = 70000\n")


@pytest.mark.skipif(not _RUST_CONFIG.is_file(), reason="m3-core-rs checkout not beside this repo")
def test_the_field_list_is_the_rust_structs():
    from m3_core.config import embed_file

    src = _RUST_CONFIG.read_text(encoding="utf-8")
    body = re.search(r"pub struct EmbedSection \{(.*?)\}", src, re.DOTALL).group(1)
    fields = tuple(re.findall(r"pub (\w+):", body))
    assert fields == embed_file.SERVER_FIELDS


def test_the_path_mirrors_the_servers(monkeypatch, tmp_path):
    import sys

    from m3_core.config import embed_file

    if sys.platform == "win32":
        monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
        assert embed_file.server_config_path() == tmp_path / "m3-embed-server" / "config.toml"
    elif sys.platform == "darwin":
        assert embed_file.server_config_path().parts[-4:-2] == ("Library", "Application Support")
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert embed_file.server_config_path() == tmp_path / "m3-embed-server" / "config.toml"
        monkeypatch.setenv("XDG_CONFIG_HOME", "")
        assert embed_file.server_config_path() == Path.home() / ".config" / "m3-embed-server" / "config.toml"
