"""Registry tables [llm], [cloud], [extraction], [promote] (plan 2C.1; survey
2026-10-09 over llm_failover.py, m3_core/llm_config.py, memory/config.py,
memory/extraction.py, promote_pipeline.py, mcp_proxy.py, m3_core/runtime.py).

Today ``.llm_config.json`` (endpoints, url, the LM Studio toggle) and
``extraction.json`` are a second source that competes with env at some sites;
they fold into [llm] / [extraction] in 2D (CONFIG_DESIGN §11), which owns that
precedence. Held for the user: M3_CLOUD_MINIMIZATION_LEVEL (read, never used).
"""
from __future__ import annotations

from .registry import Key, in_range, one_of, parse_bool, parse_float, parse_list, parse_path

_NONNEG = in_range(lo=0)


def _str(name, env, default, doc):
    return Key[str](name=name, parse=str, default=default, env_names=env, reloadable=False, doc=doc)


def _float(name, env, default, doc):
    return Key[float](name=name, parse=parse_float, default=default, validate=_NONNEG,
                      env_names=env, reloadable=False, doc=doc)


def _yes(raw: str) -> bool:  # today's `.lower() in ("1","true","yes")`: "on" read as off
    return raw.lower() in ("1", "true", "yes")


def _lower(raw: str) -> str:
    return raw.strip().lower()


_EXTRACTORS = ("rule_based", "local_llm", "remote_llm", "llm", "gemini", "claude", "lmstudio",
               "custom_script")

LLM: list[Key] = [
    # ── [llm]: the failover endpoint list ──
    Key[list[str]](name="llm.endpoints", parse=parse_list, default=[],
                   env_names=("M3_LLM_ENDPOINTS_CSV", "LLM_ENDPOINTS_CSV"), reloadable=False,
                   doc="LLM endpoints tried in order (base URLs). Empty: llm.url, then LM Studio."),
    _str("llm.url", ("M3_LLM_URL",), None, "A custom LLM endpoint, tried first."),
    Key[bool](name="llm.lmstudio_failover", parse=parse_bool, default=None,
              env_names=("M3_ENABLE_LMSTUDIO_FAILOVER",), reloadable=False, legacy_parse=_yes,
              doc="Include LM Studio in the failover list. Default: on unless llm.url is set."),
    Key[bool](name="llm.ollama_failover", parse=parse_bool, default=False,
              env_names=("M3_ENABLE_OLLAMA_FAILOVER",), reloadable=False, legacy_parse=_yes,
              doc="Include Ollama (localhost:11434) in the failover list."),
    _float("llm.timeout_s", ("M3_LLM_TIMEOUT", "LLM_TIMEOUT"), 45.0, "Seconds an LLM call may take."),
    _float("llm.connect_timeout_s", ("M3_LLM_CONNECT_TIMEOUT",), 0.3,
           "Seconds to connect to an endpoint before trying the next."),
    _float("llm.usability_ttl_s", ("M3_LLM_USABILITY_TTL",), 60.0,
           "Seconds an endpoint's usability result is cached."),
    _float("llm.outage_repeat_s", ("M3_LLM_OUTAGE_REPEAT_S",), 3600.0,
           "Seconds before an ongoing outage is reported again."),
    _float("llm.read_timeout_s", ("M3_LM_READ_TIMEOUT", "LM_READ_TIMEOUT"), 4800.0,
           "Seconds the MCP proxy waits for a model's reply."),
    _str("llm.lmstudio_base", ("LM_STUDIO_BASE",), "http://localhost:1234/v1",
         "LM Studio's base URL for the MCP proxy (no M3_ name exists today)."),
    _str("llm.lmstudio_url", ("M3_LMSTUDIO_URL",), None,
         "LM Studio endpoint the files summarizer and extractor fall back to."),
    # ── [promote]: promote_pipeline's model ──
    _str("promote.llm_url", ("M3_LM_URL", "LM_URL"), "http://localhost:1234/v1/chat/completions",
         "Chat-completions URL (full path) promote_pipeline calls."),
    _str("promote.llm_model", ("M3_LM_MODEL", "LM_MODEL"), "google/gemma-4-26b-a4b",
         "Model promote_pipeline requests."),
    # ── [cloud]: the tier-4 cloud embedder ──
    Key[bool](name="cloud.allow_fallback", parse=parse_bool, default=False,
              env_names=("M3_ALLOW_CLOUD_FALLBACK",), reloadable=False, legacy_parse=_yes,
              doc="Allow embedding through the cloud enclave when local tiers fail (privacy gate)."),
    _str("cloud.enclave_url", ("M3_CLOUD_ENCLAVE_URL",), None, "The cloud enclave's URL."),
    _str("cloud.auth_token_keyring", ("M3_CLOUD_AUTH_TOKEN_KEYRING",), None,
         "Keyring entry holding the enclave token, as service:user or user (service m3_memory). "
         "A REFERENCE, not the secret; the secret is M3_CLOUD_AUTH_TOKEN."),
    # ── [extraction]: fact extraction on write ──
    Key[str](name="extraction.type", parse=_lower, default="rule_based", validate=one_of(*_EXTRACTORS),
             env_names=("M3_EXTRACTION_TYPE",), reloadable=False,
             doc="Extractor: rule_based, an LLM provider, or custom_script. (Today an unknown value "
                 "acts as rule_based; it is now an error.)"),
    _str("extraction.model", ("M3_EXTRACTION_MODEL",), None, "Extraction model; unset picks the best LLM."),
    _str("extraction.url", ("M3_EXTRACTION_URL",), None, "Extraction endpoint."),
    _str("extraction.prompt", ("M3_EXTRACTION_PROMPT",), None, "Replaces the built-in prompt."),
    Key(name="extraction.script", parse=parse_path, default=None, env_names=("M3_EXTRACTION_SCRIPT",),
        reloadable=False, doc="Script for extraction.type = custom_script."),
    _str("extraction.function", ("M3_EXTRACTION_FUNCTION",), "extract",
         "Function the custom script exposes."),
    Key[bool](name="extraction.write_through", parse=parse_bool, default=False,
              env_names=("M3_EXTRACTION_WRITE_THROUGH",), reloadable=False, legacy_parse=_yes,
              doc="Extract inline on write instead of queueing."),
]
