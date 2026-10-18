"""m3_core.config precedence and the snapshot.

m3_config.toml > default. A key's former env names, .env lines and the LLM
file are never read: each set one is reported in ``shadowed``. The environment
is passed in as a mapping; no test touches os.environ.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest


def _loc(tmp_path: Path, text: str | None):
    from m3_core.config import Location

    p = tmp_path / "m3_config.toml"
    if text is not None:
        p.write_text(text, encoding="utf-8", newline="\n")
    return Location(file=p, root=tmp_path, tier="test")


def _build(tmp_path, text, environ):
    from m3_core.config.settings import build

    return build(_loc(tmp_path, text), environ=environ)


def _key(name):
    from m3_core.config import REGISTRY

    return REGISTRY.get(name)


# ── precedence ───────────────────────────────────────────────────────────────
def test_default_when_nothing_is_set(tmp_path):
    s = _build(tmp_path, None, {})
    assert s[_key("db.backend")] == "sqlite" and s.origin(_key("db.backend")) == "default"


def test_the_file_beats_the_default(tmp_path):
    s = _build(tmp_path, '[db]\nbackend = "postgres"\n', {})
    assert s[_key("db.backend")] == "postgres"
    assert s.origin(_key("db.backend")).endswith("[db].backend")


def test_the_file_beats_env_for_the_backend_too(tmp_path):
    """db.backend is an ordinary key. The file wins and
    the env name it shadows is reported, not dropped."""
    s = _build(tmp_path, '[db]\nbackend = "postgres"\n', {"M3_DB_BACKEND": "SQLite "})
    assert s[_key("db.backend")] == "postgres" and s.origin(_key("db.backend")).startswith("file:")
    assert "M3_DB_BACKEND" in s.shadowed_env()


def test_a_legacy_alias_is_not_read_even_with_no_file_value(tmp_path):
    s = _build(tmp_path, None, {"DB_BACKEND": "postgres"})
    assert s[_key("db.backend")] == "sqlite" and s.origin(_key("db.backend")) == "default"
    assert "DB_BACKEND" in s.shadowed_env()


def test_an_env_name_is_reported_not_read_with_or_without_a_file_value(tmp_path):
    """`m3 config set` wins at once; the stale env name is reported, never read."""
    from m3_core.config import Key, Registry, parse_bool
    from m3_core.config import keys as k
    from m3_core.config.settings import build

    knob = Key[bool](name="log.verbose", parse=parse_bool, default=False, doc="t", env_names=("M3_VERBOSE",))
    reg = Registry([*k.REGISTRY, knob])
    file_wins = build(_loc(tmp_path, "[log]\nverbose = false\n"), registry=reg, environ={"M3_VERBOSE": "1"})
    assert file_wins[knob] is False and file_wins.shadowed_env() == ["M3_VERBOSE"]

    env_wins = build(
        _loc(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path, None),
        registry=reg,
        environ={"M3_VERBOSE": "yes"},
    )
    assert env_wins[knob] is False and env_wins.origin(knob) == "default"
    assert env_wins.shadowed_env() == ["M3_VERBOSE"]


def test_a_bad_env_value_is_not_read_so_it_cannot_break_startup(tmp_path):
    """An unread name is not parsed: a typo in a leftover variable must not stop m3."""
    s = _build(tmp_path, None, {"M3_DB_BACKEND": "postgre"})
    assert s[_key("db.backend")] == "sqlite" and "M3_DB_BACKEND" in s.shadowed_env()


def test_secrets_are_not_resolved_into_the_snapshot(tmp_path):
    s = _build(tmp_path, None, {"M3_PRIMARY_PG_URL": "postgresql://u:p@h/db"})
    assert "M3_PRIMARY_PG_URL" not in s.resolved, "a secret's value sits in the settings snapshot"


# ── the snapshot and its reload ──────────────────────────────────────────────
@pytest.fixture
def cache(monkeypatch, tmp_path):
    """The process cache pointed at a temp config file, reset after."""
    from m3_core.config import settings as st

    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path))
    st._reset_for_tests()
    st.set_ttl_for_tests(0.0)
    yield tmp_path / "m3_config.toml"
    st._reset_for_tests()
    st.set_ttl_for_tests(st.DEFAULT_TTL_S)


def test_an_atomic_replace_is_seen(cache):
    """§3: identity + content hash, so a same-size, same-second replace is not missed."""
    import os

    from m3_core.config import settings as st

    cache.write_text('[secrets]\nfile = "/a/1"\n', encoding="utf-8")
    first = st.current()
    tmp = cache.with_name("new.toml")
    tmp.write_text('[secrets]\nfile = "/a/2"\n', encoding="utf-8")  # same size
    os.replace(tmp, cache)
    second = st.current()
    # Detected: the snapshot carries the new file's identity. (secrets.file itself is
    # not reloadable, so its VALUE stays; that rule has its own test.)
    assert second.identity != first.identity and second.identity.sha256 != first.identity.sha256


def test_a_bad_reload_keeps_the_last_good_snapshot(cache, caplog):
    from m3_core.config import settings as st

    cache.write_text('[secrets]\nfile = "/a/1"\n', encoding="utf-8")
    good = st.current()
    cache.write_text("[secrets]\nfile = 7\n", encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        again = st.current()
    assert again is good and "reload failed" in caplog.text and "[secrets].file" in caplog.text
    assert "[secrets].file" in (st.last_error() or "")


def test_a_non_reloadable_key_keeps_its_startup_value(cache, caplog):
    from m3_core.config import settings as st

    cache.write_text('[db]\nbackend = "sqlite"\n', encoding="utf-8")
    assert st.current()[_key("db.backend")] == "sqlite"
    cache.write_text('[db]\nbackend = "postgres"\n', encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert st.current()[_key("db.backend")] == "sqlite"
    assert "restart required" in caplog.text


def test_a_bad_file_at_startup_raises(cache):
    from m3_core.config import ConfigFileError
    from m3_core.config import settings as st

    cache.write_text("[db]\nbakend = 1\n", encoding="utf-8")
    with pytest.raises(ConfigFileError):
        st.current()


def test_override_is_dataclasses_replace(tmp_path):
    from m3_core.config.settings import override

    s = _build(tmp_path, None, {})
    t = override(s, {_key("db.backend"): "postgres"})
    assert t[_key("db.backend")] == "postgres" and s[_key("db.backend")] == "sqlite"
    with pytest.raises(Exception):
        override(s, {_key("db.backend"): "postgre"})


# ── .env: parsed as the lowest source, never injected ──
def _with_dotenv(tmp_path, text, environ, dotenv):
    from m3_core.config.resolve import DotEnv
    from m3_core.config.settings import build

    p = tmp_path / "m3_config.toml"
    if text is not None:
        p.write_text(text, encoding="utf-8")
    from m3_core.config import Location

    return build(
        Location(file=p, root=tmp_path, tier="t"),
        environ=environ,
        dotenv=DotEnv(path=str(tmp_path / ".env"), values=dotenv),
    )


def test_dotenv_is_never_read_only_reported(tmp_path):
    k = _key("search.row_cap")
    dotenv = f"M3_SEARCH_ROW_CAP (in {tmp_path / '.env'})"
    only = _with_dotenv(tmp_path, None, {}, {"M3_SEARCH_ROW_CAP": "700"})
    assert only.origin(k) == "default" and only.shadowed_env() == [dotenv]
    both = _with_dotenv(tmp_path, None, {"M3_SEARCH_ROW_CAP": "800"}, {"M3_SEARCH_ROW_CAP": "700"})
    assert both.origin(k) == "default" and set(both.shadowed_env()) == {"M3_SEARCH_ROW_CAP", dotenv}
    file_wins = _with_dotenv(tmp_path, "[search]\nrow_cap = 900\n", {}, {"M3_SEARCH_ROW_CAP": "700"})
    assert file_wins[k] == 900 and file_wins.shadowed_env() == [dotenv]


def test_a_bad_dotenv_value_is_not_read(tmp_path):
    s = _with_dotenv(tmp_path, None, {}, {"M3_SEARCH_ROW_CAP": "lots"})
    assert s.origin(_key("search.row_cap")) == "default"


def test_dotenv_is_never_injected(monkeypatch, tmp_path):
    """The real loader parses the file; os.environ is untouched."""
    import os

    from m3_core.config import env as cfg_env

    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path))
    monkeypatch.delenv("M3_PROBE_DOTENV_ONLY", raising=False)
    (tmp_path / ".env").write_text("M3_PROBE_DOTENV_ONLY=1\nBARE_NAME\n", encoding="utf-8")
    path, values = cfg_env.dotenv()
    from pathlib import Path

    assert Path(path) == tmp_path / ".env" and values == {"M3_PROBE_DOTENV_ONLY": "1"}
    assert "M3_PROBE_DOTENV_ONLY" not in os.environ
