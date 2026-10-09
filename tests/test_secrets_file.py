"""m3_core.secret_store: the user-only secrets file tier (plan 2B.1, CONFIG_DESIGN §10)."""
from __future__ import annotations

import os
import stat
import sys

import pytest

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")


def test_round_trip_including_awkward_values():
    from m3_core import secret_store as secrets

    tricky = 'postgresql://u:p"a\\ss\nw@h/db'
    secrets.set_secret("M3_PRIMARY_PG_URL", tricky)
    secrets.set_secret("OTHER", "x")
    assert secrets.get_secret("M3_PRIMARY_PG_URL") == tricky
    assert secrets.read_secrets() == {"M3_PRIMARY_PG_URL": tricky, "OTHER": "x"}


def test_no_file_is_empty_not_an_error():
    from m3_core import secret_store as secrets

    assert not secrets.secrets_file_path().exists()
    assert secrets.read_secrets() == {}


def test_malformed_file_is_loud():
    from m3_core import secret_store as secrets

    p = secrets.secrets_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[secrets\nX = 1", encoding="utf-8")
    if sys.platform != "win32":
        p.chmod(0o600)
    with pytest.raises(secrets.SecretsFileError, match="not valid TOML"):
        secrets.read_secrets()


def test_a_non_string_entry_is_named_but_its_value_is_not_echoed():
    from m3_core import secret_store as secrets

    p = secrets.secrets_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('[secrets]\nGOOD = "ok"\nBAD = 424242\n', encoding="utf-8")
    if sys.platform != "win32":
        p.chmod(0o600)
    with pytest.raises(secrets.SecretsFileError) as exc:
        secrets.read_secrets()
    assert "BAD" in str(exc.value) and "424242" not in str(exc.value)


def test_bad_names_are_refused():
    from m3_core import secret_store as secrets

    with pytest.raises(ValueError):
        secrets.set_secret("BAD NAME", "x")


@posix_only
def test_new_file_is_user_only_and_a_loose_one_is_refused():
    from m3_core import secret_store as secrets

    p = secrets.set_secret("X", "1")
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    os.chmod(p, 0o640)
    with pytest.raises(secrets.SecretsFileError, match="chmod 600"):
        secrets.read_secrets()
