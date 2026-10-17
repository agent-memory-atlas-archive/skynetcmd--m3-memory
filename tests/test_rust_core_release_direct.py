"""The core wheel comes from the release's download URLs, without the GitHub API.

The unauthenticated API allows 60 requests an hour per IP; shared CI runners
exhaust it (HTTP 403 on 2026-10-17), and PyPI is frozen at an old core, so the
install fell back to pure Python. The release's SHA256SUMS, fetched from its
plain download URL, lists every wheel; the API is asked only when it is absent.
"""

from __future__ import annotations

import hashlib
import io
import urllib.error
import urllib.request

from m3_memory import rust_core_install as rci
from m3_memory._pyver import wheel_python_tag

_REPO = "skynetcmd/m3-core-rs"
_TAG = "v2026.10.17"


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _choice():
    return rci.BackendChoice(os_tok="linux", backend="cpu", reason="test")


def _wheel_name():
    py = wheel_python_tag()
    return f"m3_core_rs_linux_cpu-3.10.17-{py}-{py}-manylinux_2_17_x86_64.whl"


def test_the_wheels_are_listed_from_sha256sums_without_the_api(monkeypatch):
    body = b"wheel bytes"
    sums = f"{hashlib.sha256(body).hexdigest()} *{_wheel_name()}\n{'0' * 64} *other-1.0-py3-none-any.whl\n"
    seen: list[str] = []

    def urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        seen.append(url)
        assert "api.github.com" not in url, "the direct path must not call the API"
        if url.endswith("/SHA256SUMS"):
            return _Resp(sums.encode())
        return _Resp(body)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    installed: list = []
    monkeypatch.setattr(rci, "_remove_sibling_core_packages", lambda _p: None)
    monkeypatch.setattr(rci, "_pip_install_with_pep668_fallback", lambda *a: installed.append(a) or 0)
    monkeypatch.setattr(rci, "_core_imports", lambda: (True, ""))
    monkeypatch.setattr(rci, "_installed_core_packages", lambda: [])

    assert rci.install_from_github_release(_choice(), git_tag=_TAG, repo=_REPO) == 0
    assert f"https://github.com/{_REPO}/releases/download/{_TAG}/{_wheel_name()}" in seen
    assert installed and any(str(a).endswith(_wheel_name()) for a in installed[0])


def test_no_sha256sums_falls_back_to_the_api(monkeypatch):
    asked: list[str] = []

    def urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        asked.append(url)
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert rci.install_from_github_release(_choice(), git_tag=_TAG, repo=_REPO) == 1
    assert asked[0].endswith(f"/releases/download/{_TAG}/SHA256SUMS")
    assert any("api.github.com" in u for u in asked[1:])
