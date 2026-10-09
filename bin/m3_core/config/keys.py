"""The registered keys, table by table (plan 2C.1: framework first, then tables).

Each table lands in its own commit with its own tests. A name the env scanner
finds that is in no table yet is listed in ``tests/data/config_unregistered.txt``,
and that list may only shrink (tests/test_config_registry_complete.py).
"""
from __future__ import annotations

from m3_core.paths import SELECTABLE_BACKENDS

from .registry import Key, KeyClass, Registry, Scope, one_of, parse_path


def _parse_backend(raw: str) -> str:
    # Today's reading exactly (paths.resolve_backend_name): strip, lowercase.
    return raw.strip().lower()


# ── [db] ─────────────────────────────────────────────────────────────────────
DB_BACKEND = Key[str](
    name="db.backend",
    parse=_parse_backend,
    default="sqlite",
    validate=one_of(*SELECTABLE_BACKENDS),   # S6: the selector owns the set
    env_names=("M3_DB_BACKEND", "DB_BACKEND"),
    env_override=True,                       # U9: the only env_override key
    reloadable=False,
    doc="Primary storage backend. The one key env may override (CI, containers).",
)

# ── [secrets] ────────────────────────────────────────────────────────────────
SECRETS_FILE = Key(
    name="secrets.file",
    parse=parse_path,
    default=None,  # <config root>/m3_secrets.toml, resolved by m3_core.secret_store
    scope=Scope.LOCATOR,
    reloadable=False,
    doc="The user-only plaintext secrets file, for hosts where no keyring serves "
        "m3's headless jobs (CONFIG_DESIGN §10). Default: <config root>/m3_secrets.toml.",
)


def _secret(name: str, doc: str, *, aliases: tuple[str, ...] = (), bootstrap: bool = False) -> Key:
    return Key[str](name=name, parse=str, default=None, klass=KeyClass.SECRET,
                    bootstrap=bootstrap, env_names=(name, *aliases), doc=doc)


# CONFIG_DESIGN §10. Bootstrap secrets never come from the vault: it lives in the
# store a DSN unlocks, and it decrypts with the master key.
SECRETS = [
    _secret("M3_PRIMARY_PG_URL", "DSN of a PostgreSQL primary store.",
            aliases=("M3_PG_URL",), bootstrap=True),
    _secret("M3_CDW_PG_URL", "DSN of the pg_sync data warehouse.", aliases=("PG_URL",)),
    _secret("AGENT_OS_MASTER_KEY", "Key that decrypts the vault.", bootstrap=True),
    _secret("M3_AGENT_OS_SALT_HEX", "The vault's device salt.", bootstrap=True),
    _secret("LM_API_TOKEN", "Token for the local LLM endpoint.", aliases=("LM_STUDIO_API_KEY",)),
    _secret("ANTHROPIC_API_KEY", "Anthropic API key."),
    _secret("OPENAI_API_KEY", "OpenAI API key."),
    _secret("GEMINI_API_KEY", "Gemini API key."),
    _secret("M3_CLOUD_AUTH_TOKEN", "Bearer token for the tier-4 cloud embedder."),
    _secret("M3_SERVE_TOKEN", "Bearer token `m3 serve` requires."),
    _secret("OPENCLAW_GATEWAY_TOKEN", "OpenClaw gateway token."),
]

REGISTRY = Registry([DB_BACKEND, SECRETS_FILE, *SECRETS])
