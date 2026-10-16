"""How m3 reaches the shared embed server (tier 2): TLS trust and the bearer token.

Callers keep their own URL (``embed.fallback_url``); this module decides from
that URL what goes with the request, so the rule has one owner:

* ``https``: the server is verified with crypto_provider's TLS 1.3 context,
  plus the CA file in ``embed.fallback_ca`` when its certificate is not
  publicly trusted (a private CA; a ``tailscale cert`` certificate needs none).
* The token (secret ``M3_EMBED_SERVER_TOKEN``) goes over https, or to a
  loopback address; never in plaintext across a network, where it could be
  read on the wire. m3's server only listens for other machines over TLS, so a
  plain ``http://`` URL to another machine is said once and gets no token.

The token is generation-cached (a changed secret is picked up within seconds);
the TLS context is built once per process (a changed CA takes effect on restart).
"""

from __future__ import annotations

import functools
import ipaddress
import logging
import ssl
import urllib.parse
import urllib.request
from typing import cast

logger = logging.getLogger("m3_core.tier2_client")

SECRET_NAME = "M3_EMBED_SERVER_TOKEN"


# Exit status of a server that refuses its configuration (EX_CONFIG), the same
# as m3-embed-server's: a restart cannot cure it, so supervisors stop retrying.
EXIT_CONFIG_REFUSED = 78


def plaintext_bind_refusal(host: str | None, server: str) -> str | None:
    """Why ``server`` (a plaintext HTTP embed server) must not bind ``host``, or
    None when it may. Embed servers receive raw memory text, so only a loopback
    address may serve it in plaintext; other machines go through
    m3-embed-server's TLS listener with a bearer token."""
    if is_loopback_host(host):
        return None
    return (
        f"refusing to start {server} on {host!r}: it serves raw memory text in plaintext, and any "
        "address other than loopback can be reached from other machines. Bind 127.0.0.1; to serve "
        "other machines, use m3-embed-server's TLS listener: `m3 embedder secure --listen <address>`"
    )


def is_loopback_host(host: str | None) -> bool:
    """True for ``localhost`` or a loopback IP literal (127.0.0.0/8, ::1)."""
    h = (host or "").strip().strip("[]").lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _token() -> str | None:
    # Generation-cached: resolved once per process, re-resolved within seconds
    # of any secret write (m3_sdk.get_secret_cached), so the hot path never
    # reads the secrets file per request and a new token needs no restart.
    from m3_sdk import get_secret_cached

    try:
        return (get_secret_cached(SECRET_NAME) or "").strip() or None
    except Exception as e:  # noqa: BLE001 — a secrets fault must not fail an embed; the server's 401 says the rest
        from m3_core.errors import log_swallowed

        log_swallowed(logger, e, "cannot resolve %s (%s); tier-2 requests go without it", SECRET_NAME, e)
        return None


@functools.cache
def _plaintext_warned(host: str) -> bool:
    logger.warning(
        "observed: %s is set but %s is a plain http:// address on another machine, so the token is not "
        "sent (it would cross the network unencrypted). fix: use the server's https:// address "
        "(`m3 embedder secure` on the server prints it)",
        SECRET_NAME,
        host,
    )
    return True


def headers(url: str) -> dict[str, str]:
    """The Authorization header for ``url``, when a token is set and may be sent there."""
    token = _token()
    if not token:
        return {}
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" or is_loopback_host(parts.hostname):
        return {"Authorization": f"Bearer {token}"}
    _plaintext_warned(parts.hostname or "")
    return {}


@functools.cache
def ssl_context() -> ssl.SSLContext:
    """crypto_provider's TLS 1.3 client context, trusting ``embed.fallback_ca`` too."""
    from crypto_provider import provider

    from m3_core.config import settings

    # Untyped upstream: Python's ssl context, or wolfSSL's when that package is
    # installed, which implements the same client interface.
    ctx = cast("ssl.SSLContext", provider.get_ssl_context())
    ca = settings.setting("embed.fallback_ca")
    if ca:
        ctx.load_verify_locations(cafile=str(ca))
    return ctx


def verify_for(url: str) -> ssl.SSLContext | bool:
    """What an httpx client should verify ``url`` with."""
    return ssl_context() if urllib.parse.urlsplit(url).scheme == "https" else True


def probe(url: str, timeout: float) -> dict:
    """Health and metrics of the tier-2 server at ``url``: THE probe behind
    `m3 doctor` and the embedder_status tool.

    /health has two contracts: the Rust server's plaintext ``OK`` (older
    builds) and JSON ``{"status": "ok"|"loading", "model": ...}``. A 200 whose
    body says neither is unhealthy, not online. /metrics is optional (the
    Python server has none), so a 404 there is not a fault.
    """
    import json
    import time
    import urllib.error

    parts = urllib.parse.urlsplit(url)
    res: dict = {
        "status": "offline",
        "url": url,
        "host": parts.hostname or "127.0.0.1",
        "port": parts.port or (443 if parts.scheme == "https" else 8082),
        "health": None,
        "model": None,
        "metrics": None,
        "latency_ms": None,
        "error": None,
    }
    base = url.rstrip("/")
    t0 = time.perf_counter()
    try:
        try:
            with urlopen(f"{base}/health", timeout=timeout) as r:
                status, body = r.status, r.read().decode(errors="replace").strip()
        except urllib.error.HTTPError as e:
            status, body = e.code, ""
        res["health"] = body
        res["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        healthy = status == 200
        if healthy and body != "OK":
            try:
                hj = json.loads(body)
                healthy = isinstance(hj, dict) and hj.get("status") in ("ok", "loading")
                if healthy:
                    res["model"] = hj.get("model")
            except json.JSONDecodeError:
                healthy = False
        if not healthy:
            res["status"] = f"unhealthy-{status}"
            return res
        try:
            with urlopen(f"{base}/metrics", timeout=timeout) as r:
                metrics = json.loads(r.read().decode())
            res["metrics"] = metrics
            res["model"] = metrics.get("model") or res["model"]
        except (urllib.error.HTTPError, json.JSONDecodeError, AttributeError):
            pass  # no /metrics on this server, or not JSON: optional
        res["status"] = "online"
    except Exception as e:  # noqa: BLE001 — a failed probe is the answer, never a crash
        res["error"] = f"{type(e).__name__}: {e}"
    return res


def urlopen(url: str, *, timeout: float, data: bytes | None = None, extra_headers: dict[str, str] | None = None):
    """``urllib.request.urlopen`` for an http(s) tier-2 URL, with the token and
    TLS trust above. Any other scheme raises ValueError: a config value must not
    turn a probe into a local file read."""
    scheme = urllib.parse.urlsplit(url).scheme
    if scheme not in ("http", "https"):
        raise ValueError(f"{url!r} is not an http(s) URL")
    req = urllib.request.Request(url, data=data, headers={**headers(url), **(extra_headers or {})})
    ctx = ssl_context() if scheme == "https" else None
    # nosec B310: the scheme is restricted to http/https immediately above.
    return urllib.request.urlopen(req, timeout=timeout, context=ctx)  # noqa: S310  # nosec B310
