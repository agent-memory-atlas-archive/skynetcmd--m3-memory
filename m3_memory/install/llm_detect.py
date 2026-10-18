"""Find the LLM servers this machine already uses, for memory enrichment.

Setup offers them and doctor reports on them. The signals are other programs'
own variables plus the runtimes' default local ports:

- Ollama: ``OLLAMA_HOST`` (its server address; ``0.0.0.0`` means "this
  machine"). A local Ollama needs no key; ``OLLAMA_API_KEY`` is Ollama's cloud
  key and is sent only to ollama.com, never to a self-hosted server.
- LM Studio: no endpoint variable (default ``localhost:1234``). With "Require
  Authentication" on, its token is ``LM_API_TOKEN`` (alias ``LM_STUDIO_API_KEY``),
  sent as a Bearer header.
- Any OpenAI-compatible client: ``OPENAI_BASE_URL`` / ``OPENAI_API_BASE`` with
  ``OPENAI_API_KEY``.

Names are read through ``env_sources.scan`` (the one owner of where a variable is
set: process, shell files, the Windows registry, MCP entries), once, to make an
offer. What the user picks is written to m3_config.toml (``llm.endpoints``) and a
key that worked to m3's secrets file (``LM_API_TOKEN``); m3 never reads these
variables afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

LOCAL_OLLAMA = "http://localhost:11434/v1"
LOCAL_LMSTUDIO = "http://localhost:1234/v1"
OLLAMA_CLOUD_HOST = "ollama.com"
# The secret m3's LLM jobs send to the local endpoint (registry: LM_API_TOKEN).
LLM_SECRET = "LM_API_TOKEN"

SIGNAL_NAMES = (
    "OLLAMA_HOST",
    "OLLAMA_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "OPENAI_API_KEY",
    "LM_API_TOKEN",
    "LM_STUDIO_API_KEY",
)


@dataclass(slots=True)
class Candidate:
    runtime: str  # "Ollama" | "LM Studio" | "OpenAI-compatible"
    url: str  # OpenAI-compatible base, ending in /v1
    source: str  # where it came from, for a person
    key_name: str | None = None  # the variable the key came from
    key: str | None = field(default=None, repr=False)  # never printed


@dataclass(slots=True)
class Probe:
    candidate: Candidate
    state: str  # "ok" | "works-with-key" | "needs-key" | "unreachable" | "error"
    models: list[str]
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.state in ("ok", "works-with-key")


def ollama_url(host: str) -> str:
    """``OLLAMA_HOST`` as an OpenAI-compatible base URL. Ollama reads it as its
    BIND address too, so ``0.0.0.0`` / ``::`` / empty mean "this machine"; a
    missing port is Ollama's 11434 and a missing scheme is http."""
    h = (host or "").strip()
    if not h:
        return LOCAL_OLLAMA
    if "://" not in h:
        h = "http://" + h
    u = urlparse(h)
    name = u.hostname or ""
    if name in ("", "0.0.0.0", "::"):
        name = "localhost"
    port = u.port or (443 if u.scheme == "https" and name.endswith(OLLAMA_CLOUD_HOST) else 11434)
    shown = f"[{name}]" if ":" in name else name
    return f"{u.scheme}://{shown}:{port}/v1"


def _base(url: str) -> str:
    u = url.strip().rstrip("/")
    return u if u.endswith("/v1") else u + "/v1"


def find_candidates(found: list | None = None) -> list[Candidate]:
    """Candidates from the variables set anywhere m3 looks, then the default
    local ports. One per URL; a variable-named one wins over a default port."""
    if found is None:
        from m3_memory.install.env_sources import scan

        found = scan(list(SIGNAL_NAMES))
    first: dict[str, str] = {}
    where: dict[str, str] = {}
    for f in found:
        value = (f.value or "").strip()
        if value and f.name not in first and f.m3_surface:
            first[f.name], where[f.name] = value, f.place
    lm_key_name = next((n for n in ("LM_API_TOKEN", "LM_STUDIO_API_KEY") if n in first), None)
    cands: list[Candidate] = []
    if "OLLAMA_HOST" in first:
        url = ollama_url(first["OLLAMA_HOST"])
        cloud = (urlparse(url).hostname or "").endswith(OLLAMA_CLOUD_HOST)
        key = first.get("OLLAMA_API_KEY") if cloud else None
        cands.append(
            Candidate("Ollama", url, f"OLLAMA_HOST in {where['OLLAMA_HOST']}", "OLLAMA_API_KEY" if key else None, key)
        )
    base_name = next((n for n in ("OPENAI_BASE_URL", "OPENAI_API_BASE") if n in first), None)
    if base_name:
        key_name = "OPENAI_API_KEY" if "OPENAI_API_KEY" in first else lm_key_name
        cands.append(
            Candidate(
                "OpenAI-compatible",
                _base(first[base_name]),
                f"{base_name} in {where[base_name]}",
                key_name,
                first.get(key_name) if key_name else None,
            )
        )
    cands.append(Candidate("Ollama", LOCAL_OLLAMA, "Ollama's default port on this machine"))
    cands.append(
        Candidate(
            "LM Studio",
            LOCAL_LMSTUDIO,
            "LM Studio's default port on this machine",
            lm_key_name,
            first.get(lm_key_name) if lm_key_name else None,
        )
    )
    seen: dict[str, Candidate] = {}
    for c in cands:
        if c.url not in seen:
            seen[c.url] = c
        elif c.key and not seen[c.url].key:
            seen[c.url].key, seen[c.url].key_name = c.key, c.key_name
    return list(seen.values())


def probe(c: Candidate, timeout: float = 2.0) -> Probe:
    """Ask ``c`` for its models: first without a key (a keyless server needs none),
    then, on a 401/403, with the key its variables gave, if any."""
    import httpx

    def get(headers: dict) -> httpx.Response:
        return httpx.get(c.url.rstrip("/") + "/models", headers=headers, timeout=timeout)

    try:
        r = get({})
        if r.status_code in (401, 403):
            if not c.key:
                return Probe(c, "needs-key", [], f"HTTP {r.status_code} without a key")
            r = get({"Authorization": f"Bearer {c.key}"})
            if r.status_code in (401, 403):
                return Probe(c, "needs-key", [], f"HTTP {r.status_code}, also with {c.key_name}")
            if r.status_code == 200:
                return Probe(c, "works-with-key", _model_ids(r), f"with {c.key_name}")
        if r.status_code == 200:
            return Probe(c, "ok", _model_ids(r))
        return Probe(c, "error", [], f"HTTP {r.status_code}")
    except httpx.HTTPError as e:
        return Probe(c, "unreachable", [], type(e).__name__)


def _model_ids(r) -> list[str]:
    try:
        data = r.json().get("data") or []
    except ValueError:
        return []
    return [str(m.get("id")) for m in data if isinstance(m, dict) and m.get("id")]


def is_cloud(url: str) -> bool | None:
    """True when ``url``'s host is outside this machine and its private networks
    (memory text would leave them); False for loopback, private ranges and the
    tailnet's 100.64.0.0/10; None when the name does not resolve (said as
    unknown, never guessed). A name is judged by the address it resolves to."""
    import ipaddress
    import socket

    host = urlparse(url).hostname or ""
    if not host:
        return None
    try:
        addrs = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            addrs = {ipaddress.ip_address(str(a[4][0]).split("%")[0]) for a in socket.getaddrinfo(host, None)}
        except (OSError, ValueError):
            return None
    tailnet = ipaddress.ip_network("100.64.0.0/10")
    local = all(a.is_loopback or a.is_private or a.is_link_local or (a.version == 4 and a in tailnet) for a in addrs)
    return not local


def describe(p: Probe) -> str:
    """One line for a person: the runtime, URL, where it came from, what it answered."""
    c = p.candidate
    models = ", ".join(p.models[:3]) + (" …" if len(p.models) > 3 else "")
    what = {
        "ok": f"answers ({models or 'no models loaded'})",
        "works-with-key": f"answers with {c.key_name} ({models or 'no models loaded'})",
        "needs-key": f"needs an API key ({p.detail})",
        "unreachable": "does not answer",
        "error": f"answered {p.detail}",
    }[p.state]
    return f"{c.runtime} at {c.url} — {what}  [{c.source}]"
