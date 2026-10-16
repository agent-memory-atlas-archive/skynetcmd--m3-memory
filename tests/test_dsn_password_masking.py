"""mask_dsn_passwords / loggable_url hide a DSN password in every libpq form.

The masker matched only URI userinfo and stopped at the first '/', so a keyword
DSN (`password=p`), a URI query (`?password=p`) and a URI password containing
'/' all reached error lines in the clear. loggable_url relied on it, and
urlsplit made the tail of a '/'-password into the path.
"""

from __future__ import annotations

import pytest
from m3_core.secret_store import loggable_url, mask_dsn_passwords

_PW = "Zq9-secret"


@pytest.mark.parametrize(
    "text",
    [
        f"postgresql://u:{_PW}@h:5432/db",
        f"postgresql://u:{_PW}/tail@h/db",  # unencoded '/' in the password
        f"postgresql://u:ab:{_PW}@h/db",  # unencoded ':' in the password
        f"postgresql://u:a@{_PW}@h/db",  # unencoded '@' in the password
        f"postgresql://:{_PW}@h/db",  # no user
        f"host=h dbname=db user=u password={_PW}",
        f"host=h password = {_PW} dbname=db",
        f"host=h password='{_PW} with space' dbname=db",
        f"postgresql://u@h/db?sslmode=require&password={_PW}",
        f"PASSWORD={_PW}",
    ],
)
def test_no_form_leaks_the_password(text):
    out = mask_dsn_passwords(f"connect failed for {text} (timeout)")
    assert _PW not in out, f"observed: {out!r}"
    assert "***" in out
    assert out.startswith("connect failed for ") and out.endswith(" (timeout)")


def test_what_is_not_a_credential_is_left_alone():
    for text in ("no dsn here", "postgresql://u@h:5432/db", "dbname=db host=h", "passwords=are-not-keys"):
        assert mask_dsn_passwords(text) == text


def test_the_keyword_form_keeps_the_other_keys():
    assert mask_dsn_passwords(f"host=h password={_PW} dbname=db") == "host=h password=*** dbname=db"


@pytest.mark.parametrize(
    "url",
    [
        f"postgresql://u:12/{_PW}@h:5432/db",  # parsed as host 'u', port 12 before
        f"postgresql://u:{_PW}/x@h/db",
        f"host=h password={_PW}",
    ],
)
def test_loggable_url_never_shows_a_slash_password(url):
    out = loggable_url(url)
    assert _PW not in out, f"observed: {out!r}"


def test_loggable_url_still_reduces_a_plain_url():
    assert loggable_url(f"postgresql://u:{_PW}@db.example:5433/m3?sslmode=require") == "postgresql://db.example:5433/m3"
