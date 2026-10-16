"""How m3 reaches the shared embed server (tier 2): its URL, TLS trust, the
bearer token, and what a failed request means.

The rule has one owner, this module:

* ``fallback_url()`` is the URL every reader uses: a set ``embed.fallback_url``,
  else the legacy ``.embed_config.json`` ``fallback_url``, else this machine's
  server on ``embed.server.port``. It says which of the three answered.
* ``https``: the server is verified with crypto_provider's TLS 1.3 context,
  plus the CA file in ``embed.fallback_ca`` when its certificate is not
  publicly trusted (a private CA; a ``tailscale cert`` certificate needs none).
* The token (secret ``M3_EMBED_SERVER_TOKEN``) goes over https, or to a
  loopback address; never in plaintext across a network, where it could be
  read on the wire. m3's server only listens for other machines over TLS, so a
  plain ``http://`` URL to another machine is said once and gets no token.
* ``classify()`` turns any outcome of a tier-2 request (an exception from
  urllib or httpx, or an HTTP status) into one ``Diagnosis``: a state, what was
  observed, and the fix. Every probe and request path reports through it, so a
  401 is never called "down" and a refused port is never called "unhealthy".

The token is generation-cached (a changed secret is picked up within seconds);
the TLS context is built once per process (a changed CA takes effect on restart).

Only the stdlib is imported at module level: the embed watchdog imports this
module and must run when the rest of the package is half-upgraded.
"""

from __future__ import annotations

import errno
import functools
import ipaddress
import json
import logging
import socket
import ssl
import threading
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

logger = logging.getLogger("m3_core.tier2_client")

SECRET_NAME = "M3_EMBED_SERVER_TOKEN"
DEFAULT_PORT = 8082


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


# ── say-once ──────────────────────────────────────────────────────────────────
_SAID: set[str] = set()
_SAID_LOCK = threading.Lock()


def _say_once(key: str, level: int, msg: str, *args: object) -> None:
    """Log ``msg`` the first time ``key`` is seen in this process. For faults a
    hot path would otherwise repeat per request."""
    with _SAID_LOCK:
        if key in _SAID:
            return
        _SAID.add(key)
    logger.log(level, msg, *args)


# ── the URL ───────────────────────────────────────────────────────────────────
def _legacy_embed_config() -> tuple[dict, str]:
    """(.embed_config.json contents, its path). Empty when absent; a malformed
    file is said once and read as empty (the caller's other sources still apply)."""
    try:
        from m3_core.seams import roots

        path = Path(roots.config_root()) / ".embed_config.json"
    except Exception as e:  # noqa: BLE001 — no config root: only the setting and the default apply
        _say_once(
            "legacy-root",
            logging.WARNING,
            "observed: the config root cannot be resolved (%s: %s), so .embed_config.json is not read for "
            "the tier-2 URL. inspect: M3_CONFIG_ROOT",
            type(e).__name__,
            e,
        )
        return {}, ".embed_config.json"
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}, str(path)
    except (OSError, ValueError) as e:
        _say_once(
            f"legacy-bad:{path}",
            logging.WARNING,
            "observed: %s is unreadable (%s: %s); its fallback_url is ignored. fix: repair the file, or "
            "`m3 embedder shared` to rewrite it",
            path,
            type(e).__name__,
            e,
        )
        return {}, str(path)
    return (data if isinstance(data, dict) else {}), str(path)


def fallback_url(legacy: Mapping[str, object] | None = None) -> tuple[str, str]:
    """(tier-2 base URL, where it came from). Precedence: a SET
    ``embed.fallback_url`` (m3_config.toml or env) > ``fallback_url`` in the
    legacy .embed_config.json > ``http://127.0.0.1:<embed.server.port>``.

    ``legacy``: the already-read .embed_config.json, for a caller that has it;
    read here when None."""
    from m3_core.config import settings

    if settings.explicit("embed.fallback_url"):
        value = str(settings.setting("embed.fallback_url") or "").strip()
        if value:
            return value.rstrip("/"), f"embed.fallback_url ({settings.origin_of('embed.fallback_url')})"
    if legacy is None:
        cfg, path = _legacy_embed_config()
    else:
        cfg, path = dict(legacy), ".embed_config.json"
    value = str(cfg.get("fallback_url") or "").strip()
    if value:
        return value.rstrip("/"), f"fallback_url in {path}"
    port = settings.setting("embed.server.port") or DEFAULT_PORT
    return f"http://127.0.0.1:{port}", "default (this machine, embed.server.port)"


def where(url: str) -> str:
    """``scheme://host:port`` of ``url``: what a message names. Never the path,
    query or userinfo, which may carry a credential."""
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        return repr(url[:80])
    from m3_core.secret_store import loggable_url  # lazy: this module imports only the stdlib

    return loggable_url(url, path=False)


# ── the token ─────────────────────────────────────────────────────────────────
def _token() -> str | None:
    # Generation-cached: resolved once per process, re-resolved within seconds
    # of any secret write (m3_sdk.get_secret_cached), so the hot path never
    # reads the secrets file per request and a new token needs no restart.
    try:
        from m3_sdk import get_secret_cached

        return (get_secret_cached(SECRET_NAME) or "").strip() or None
    except Exception as e:  # noqa: BLE001 — a secrets fault must not fail an embed; it is said once, below
        # Said once at WARNING, not via log_swallowed: a secrets fault is not a
        # code defect, so log_swallowed would put it at DEBUG, and the operator
        # would then see only the 401 without its cause.
        _say_once(
            f"token:{type(e).__name__}",
            logging.WARNING,
            "observed: cannot resolve secret %s (%s: %s); tier-2 requests go without a token, so a "
            "token-protected embed server will answer 401. inspect: the secrets file, or "
            "`m3 secret set %s`",
            SECRET_NAME,
            type(e).__name__,
            e,
            SECRET_NAME,
        )
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


# ── TLS ───────────────────────────────────────────────────────────────────────
# Set when embed.fallback_ca could not be loaded; classify() names it on a
# certificate failure, which is how that fault reaches the operator per request.
_CA_FAILURE: str | None = None


@functools.cache
def ssl_context() -> ssl.SSLContext:
    """crypto_provider's TLS 1.3 client context, trusting ``embed.fallback_ca`` too.

    A CA file that cannot be loaded is said once at ERROR and the context is
    returned WITHOUT it: the context also verifies tier 3 and cloud endpoints on
    the shared embed client, which a bad private-CA path must not take down.
    Only a tier-2 server whose certificate needs that CA then fails, and its
    certificate error names this setting (classify)."""
    global _CA_FAILURE
    from crypto_provider import provider

    from m3_core.config import settings

    # Untyped upstream: Python's ssl context, or wolfSSL's when that package is
    # installed, which implements the same client interface.
    ctx = cast("ssl.SSLContext", provider.get_ssl_context())
    ca = settings.setting("embed.fallback_ca")
    if ca:
        try:
            ctx.load_verify_locations(cafile=str(ca))
        except (OSError, ssl.SSLError, ValueError) as e:
            _CA_FAILURE = f"embed.fallback_ca {str(ca)!r} could not be loaded ({type(e).__name__}: {e})"
            logger.error(
                "observed: %s; https tier-2 requests verify with the default trust only, so a server whose "
                "certificate comes from a private CA will fail verification. fix: `m3 config set "
                "embed.fallback_ca <readable CA .pem>`, or `m3 config unset embed.fallback_ca` when the "
                "certificate is publicly trusted",
                _CA_FAILURE,
            )
    return ctx


def verify_for(url: str) -> ssl.SSLContext | bool:
    """What an httpx client should verify ``url`` with."""
    return ssl_context() if urllib.parse.urlsplit(url).scheme == "https" else True


# ── what an outcome means ─────────────────────────────────────────────────────
# States in which something at the URL took part (an HTTP reply or a TLS
# handshake). A supervisor's start cannot cure these: the server is up.
_ANSWERING = frozenset({"ok", "loading", "auth-rejected", "tls-untrusted", "tls-failed", "bad-response"})
_SNIPPET = 200


@dataclass(frozen=True, slots=True)
class Diagnosis:
    """One classified tier-2 outcome.

    state:  ok | loading | auth-rejected | tls-untrusted | tls-failed | refused |
            timeout | unresolvable | unreachable | bad-scheme | bad-response |
            unhealthy-<code> | error
    detail: what was observed, naming the server (scheme://host:port).
    fix:    the setting or command to change; empty when nothing is wrong.
    """

    state: str
    detail: str
    fix: str = ""
    status: int | None = None

    @property
    def serving(self) -> bool:
        """The server answered its health contract (``ok``, or ``loading`` its model)."""
        return self.state in ("ok", "loading")

    @property
    def answering(self) -> bool:
        """Something at the URL took part: an HTTP reply or a TLS handshake."""
        return self.state in _ANSWERING or self.state.startswith("unhealthy-")

    def message(self) -> str:
        return f"{self.detail}. fix: {self.fix}" if self.fix else self.detail


def snippet(body: object) -> str:
    """At most 200 characters of a response body for a message, bearer tokens
    redacted (secret_store.redacted_snippet, the one redaction)."""
    from m3_core.secret_store import redacted_snippet  # lazy: this module imports only the stdlib

    return redacted_snippet(body, _SNIPPET)


def _causes(exc: BaseException):
    """``exc`` and everything it wraps: __cause__, __context__ and urllib's
    URLError.reason, breadth-first, each once."""
    queue: list[BaseException] = [exc]
    seen: set[int] = set()
    while queue:
        e = queue.pop(0)
        if id(e) in seen:
            continue
        seen.add(id(e))
        yield e
        for nxt in (getattr(e, "reason", None), e.__cause__, e.__context__):
            if isinstance(nxt, BaseException):
                queue.append(nxt)


def _status_of(exc: BaseException) -> tuple[int, str] | None:
    """(status, body) when ``exc`` carries an HTTP reply: urllib's HTTPError
    (``code``) or httpx's HTTPStatusError (``response``)."""
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        try:
            return code, str(getattr(response, "text", ""))
        except Exception:  # noqa: BLE001 — a body we cannot read still has its status
            return code, ""
    code = getattr(exc, "code", None)
    if isinstance(code, int) and hasattr(exc, "read"):
        try:
            return code, getattr(exc, "read")().decode("utf-8", "replace")  # noqa: B009 — duck-typed urllib HTTPError
        except Exception:  # noqa: BLE001 — as above
            return code, ""
    return None


def _auth_fix(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    token = _token()
    if token and parts.scheme != "https" and not is_loopback_host(parts.hostname):
        return (
            f"{SECRET_NAME} is set but was not sent: {where(url)} is plain http to another machine. Use the "
            "server's https:// address: `m3 config set embed.fallback_url https://<server>:<port>`"
        )
    if token:
        return (
            f"the server rejected this machine's {SECRET_NAME}. Store the server's current token with "
            f"`m3 secret set {SECRET_NAME}` (`m3 embedder secure` on the server mints it)"
        )
    return (
        f"no {SECRET_NAME} is set on this machine. Store the server's token with `m3 secret set {SECRET_NAME}` "
        "(`m3 embedder secure` on the server mints it)"
    )


def _refused_fix(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if is_loopback_host(parts.hostname):
        return "start it: `m3 embedder start` (or `m3 doctor --fix`, which also restores its keep-alive)"
    return (
        f"the server on {parts.hostname} is not listening there; start it on that machine "
        "(`m3 embedder start`), or correct embed.fallback_url"
    )


def _classify_status(status: int, body: str, url: str) -> Diagnosis:
    at = where(url)
    parsed: object = None
    try:
        parsed = json.loads(body) if body else None
    except ValueError:
        parsed = None
    reported = parsed.get("status") if isinstance(parsed, dict) else None
    if status in (401, 403):
        return Diagnosis("auth-rejected", f"{at} answered HTTP {status} (bearer token refused)", _auth_fix(url), status)
    if isinstance(reported, str) and reported.startswith("loading"):
        return Diagnosis("loading", f"{at} is loading its model (HTTP {status})", "", status)
    if status == 200:
        return Diagnosis("ok", f"{at} answered HTTP 200", "", status)
    if isinstance(parsed, dict) and reported == "degraded":
        reason = snippet(parsed.get("reason") or "no reason given")
        fix = (
            "inspect: the embed server's log and `m3 embedder status`; it reports itself degraded "
            "(its circuit breaker is open), so a restart is not the first step"
        )
        return Diagnosis(f"unhealthy-{status}", f"{at} reports degraded (HTTP {status}): {reason}", fix, status)
    shown = snippet(body) or "empty body"
    if status == 404:
        fix = f"no such route on {at}: check that embed.fallback_url names an m3 embed server"
    elif is_loopback_host(urllib.parse.urlsplit(url).hostname):
        fix = "inspect: the embed server's log and `m3 embedder status`"
    else:
        fix = f"inspect: the embed server's log on {urllib.parse.urlsplit(url).hostname}"
    return Diagnosis(f"unhealthy-{status}", f"{at} answered HTTP {status}: {shown}", fix, status)


def classify(outcome: BaseException | int, url: str, body: str = "", *, timeout: float | None = None) -> Diagnosis:
    """The Diagnosis of a tier-2 request to ``url`` that ended in ``outcome``: an
    exception (urllib, httpx, ssl, OS) or an HTTP status with its ``body``."""
    at = where(url)
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        return Diagnosis(
            "bad-scheme",
            f"the tier-2 URL {at} is not http(s); it is refused unread",
            "`m3 config set embed.fallback_url http://127.0.0.1:8082` (or the server's https:// address)",
        )
    if isinstance(outcome, int):
        return _classify_status(outcome, body, url)
    chain = list(_causes(outcome))
    for e in chain:
        got = _status_of(e)
        if got is not None:
            return _classify_status(got[0], got[1], url)
    for e in chain:
        if isinstance(e, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(e):
            reason = getattr(e, "verify_message", None) or snippet(e)
            fix = (
                "if the server's certificate comes from a private CA: `m3 config set embed.fallback_ca <ca.pem>`; "
                "if the name does not match, use the name on the certificate in embed.fallback_url"
            )
            if _CA_FAILURE:
                fix = f"{_CA_FAILURE}; {fix}"
            return Diagnosis(
                "tls-untrusted", f"{at} presented a certificate this machine does not trust: {reason}", fix
            )
    for e in chain:
        if isinstance(e, ssl.SSLError):
            return Diagnosis(
                "tls-failed",
                f"the TLS handshake with {at} failed: {snippet(e)}",
                "check the scheme and port in embed.fallback_url (an https:// URL to a plain-http port fails this way)",
            )
    for e in chain:
        refused = isinstance(e, ConnectionRefusedError) or (
            isinstance(e, OSError) and e.errno in (errno.ECONNREFUSED, 10061)
        )
        if refused or "refused" in str(e).lower():
            return Diagnosis(
                "refused", f"nothing accepted the connection at {at} (connection refused)", _refused_fix(url)
            )
    for e in chain:
        if isinstance(e, TimeoutError) or "Timeout" in type(e).__name__ or "timed out" in str(e).lower():
            within = f" within {timeout:g}s" if timeout else ""
            host = urllib.parse.urlsplit(url).hostname
            fix = (
                "the server may be loading its model or stuck; inspect: `m3 embedder status` and its log"
                if is_loopback_host(host)
                else f"inspect: the network path to {host} and the server there"
            )
            return Diagnosis("timeout", f"{at} did not answer{within} ({type(e).__name__})", fix)
    for e in chain:
        if isinstance(e, socket.gaierror):
            return Diagnosis(
                "unresolvable",
                f"the host in {at} does not resolve ({snippet(e)})",
                "correct embed.fallback_url: `m3 config set embed.fallback_url <url>`",
            )
    for e in chain:
        if isinstance(e, json.JSONDecodeError):
            return Diagnosis(
                "bad-response",
                f"{at} answered with a body that is not JSON ({snippet(e)})",
                f"check that embed.fallback_url names an m3 embed server, not another service on {at}",
            )
    for e in chain:
        if isinstance(e, OSError):
            return Diagnosis(
                "unreachable",
                f"{at} could not be reached ({type(e).__name__}: {snippet(e)})",
                "inspect: the network path and embed.fallback_url",
            )
    return Diagnosis("error", f"the request to {at} failed ({type(outcome).__name__}: {snippet(outcome)})")


def classify_health(status: int, raw: str, url: str) -> tuple[Diagnosis, dict]:
    """The Diagnosis of a /health reply, and its JSON body (``{}`` when none).

    Two contracts: the Rust server's plaintext ``OK`` (older builds) and JSON
    ``{"status": "ok"|"loading", "model": ...}``. A 200 whose body says neither
    is not online."""
    at = where(url)
    raw = (raw or "").strip()
    if status == 200 and raw == "OK":
        return Diagnosis("ok", f"{at} is serving (plaintext OK)", "", 200), {"status": "ok"}
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = None
    body = parsed if isinstance(parsed, dict) else {}
    if status != 200:
        return _classify_status(status, raw, url), body
    if not isinstance(parsed, dict):
        return Diagnosis(
            "bad-response",
            f"{at}/health answered 200 with a body that is neither OK nor health JSON: {snippet(raw) or 'empty body'}",
            f"check that embed.fallback_url names an m3 embed server, not another service on {at}",
            200,
        ), body
    reported = parsed.get("status")
    if reported == "ok":
        return Diagnosis("ok", f"{at} is serving", "", 200), body
    if isinstance(reported, str) and reported.startswith("loading"):
        return Diagnosis("loading", f"{at} is up and loading its model", "", 200), body
    detail = f"{at}/health answered 200 but reports status {reported!r}"
    if parsed.get("reason"):
        detail += f": {snippet(parsed['reason'])}"
    return Diagnosis("unhealthy-200", detail, "inspect: the embed server's log and `m3 embedder status`", 200), body


def health(url: str, timeout: float) -> tuple[Diagnosis, dict]:
    """GET ``url``/health once and classify it: THE health check behind doctor,
    the watchdog, recovery and the probes. Never raises."""
    base = url.rstrip("/")
    if urllib.parse.urlsplit(base).scheme not in ("http", "https"):
        return classify(ValueError("not http(s)"), base), {}
    try:
        with urlopen(f"{base}/health", timeout=timeout) as r:
            status = int(getattr(r, "status", 200) or 200)
            raw = r.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 — a failed probe is the answer, classified
        return classify(e, base, timeout=timeout), {}
    return classify_health(status, raw, base)


def probe(url: str, timeout: float, source: str | None = None) -> dict:
    """Health and metrics of the tier-2 server at ``url``: THE probe behind
    `m3 doctor` and the embedder_status tool.

    ``status`` is ``online`` when the server answers its health contract (ok or
    loading), else the Diagnosis state (``refused``, ``auth-rejected``,
    ``unhealthy-503``...), with ``error`` (what was observed) and ``fix``.
    /metrics is optional (the Python server has none), so a 404 there is not a fault.
    """
    import time
    import urllib.error

    parts = urllib.parse.urlsplit(url)
    try:
        port = parts.port or (443 if parts.scheme == "https" else DEFAULT_PORT)
    except ValueError:
        port = None
    res: dict = {
        "status": "offline",
        "state": None,
        "url": url,
        "source": source,
        "host": parts.hostname or "127.0.0.1",
        "port": port,
        "health": None,
        "model": None,
        "metrics": None,
        "latency_ms": None,
        "error": None,
        "fix": None,
    }
    base = url.rstrip("/")
    t0 = time.perf_counter()
    diag, body = health(base, timeout)
    res["latency_ms"] = int((time.perf_counter() - t0) * 1000)
    res["state"] = diag.state
    res["health"] = body or None
    if not diag.serving:
        res["status"] = diag.state
        res["error"] = diag.detail
        res["fix"] = diag.fix or None
        return res
    res["model"] = body.get("model")
    try:
        with urlopen(f"{base}/metrics", timeout=timeout) as r:
            metrics = json.loads(r.read().decode())
        if isinstance(metrics, dict):
            res["metrics"] = metrics
            res["model"] = metrics.get("model") or res["model"]
    except (urllib.error.HTTPError, ValueError, AttributeError):
        pass  # no /metrics on this server, or not JSON: optional
    except OSError as e:
        # /health answered a moment ago, so this is worth a line, not a verdict.
        logger.debug("tier-2 /metrics at %s not read: %s", where(url), e)
    res["status"] = "online"
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
