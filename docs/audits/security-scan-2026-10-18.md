# Security scan — 2026-10-18

Scope: the shipped library (`bin/`, `m3_memory/`) at commit `eec4857f`,
version `2026.10.19.0`, and the dependencies it installs.

**Headline: no HIGH-severity static-analysis findings, no secrets in the tree,
no known vulnerabilities in what `pip install m3-memory` installs. One optional
extra, `[crewai]`, pulls in a ChromaDB release with four unpatched server-side
advisories.**

Every command below is reproducible on your own checkout. No private tooling.

---

## 1. Static analysis: `bandit`

```bash
python -m bandit -c pyproject.toml -r bin m3_memory      # with the project skip-list
python -m bandit -r bin m3_memory                        # unfiltered
```

| Metric | Value |
|---|---|
| Files scanned | 399 |
| Lines of code | 134,287 |
| Findings (with project skip-list) | **0** |
| Findings (unfiltered) | 1,283 |
| **HIGH severity (unfiltered)** | **0** |

The skip-list lives in `pyproject.toml` under `[tool.bandit]`, each entry with
its reasoning. The unfiltered counts, by rule:

| Test | Severity | Count | Why it is suppressed |
|---|---|---|---|
| B608 | MEDIUM | 704 | f-string SQL. Every site inlines generated placeholder lists or static column names; user values reach the database through the parameter tuple. |
| B110 / B112 | LOW | 186 | `try/except/pass` and `/continue` in fail-open bridge and daemon paths. |
| B603 / B607 | LOW | 227 | `subprocess` with `shell=False` and static argv. |
| B404 | LOW | 83 | Importing `subprocess` is not itself a vulnerability. |
| B101 | LOW | 57 | `assert` used as an internal invariant. |
| B105 | LOW | 20 | See below. |
| B311 | LOW | 4 | `random.sample` for statistical sampling, not entropy. |
| B606 | LOW | 2 | See below. |

### Reviewed this scan

- **B105 "hardcoded password string" ×20** (6 at the last scan). Each was read:
  the names under which a secret is stored (`SECRET_NAME =
  "M3_EMBED_SERVER_TOKEN"`, `TOKEN_SECRET = ...`), never a secret's value;
  dictionary keys in symbol- and setting-routing tables (`"TOKEN_BUDGET":
  "embed.token_budget"`); the enum value `"secret"`; a `{"pass": 0, ...}`
  counter; and a log message that names a secret. No secret is present.
- **B606 "start process with no shell" ×2**: the same two
  `os.execv(sys.executable, …)` re-execs of the current interpreter for UTF-8
  stdio on Windows (`bin/m3_core/runtime.py`, `m3_memory/cli.py`). No shell; the
  executable is `sys.executable`.

## 2. Secrets scan

```bash
git ls-files -z | xargs -0 grep -lE \
  'sk-ant-[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_-]{30,}|AKIA[A-Z0-9]{16}|ASIA[A-Z0-9]{16}|ghp_[A-Za-z0-9]{30,}'
```

Three files match, all expected: `bin/chatlog_redaction.py` (the redaction
engine, which holds these patterns to scrub them from chat logs) and its tests,
`tests/test_chatlog_redaction.py` and `tests/test_redaction_parity.py`, whose
fixtures are synthetic: AWS's documented example key `AKIAIOSFODNN7EXAMPLE`,
`ASIAEXAMPLE…`, and alphabet sequences. No credential material in the tree.

## 3. Dependency CVEs: `pip-audit`

### What `pip install m3-memory` installs

Measured on a fresh virtual environment with only the built wheel installed, so
the full transitive set is audited, not the developer's environment:

```bash
python -m build --wheel
uv venv /tmp/audit && uv pip install --python /tmp/audit/bin/python dist/m3_memory-2026.10.19.0-py3-none-any.whl
uv pip freeze --python /tmp/audit/bin/python | grep -v '^m3-memory' > core.txt
python -m pip_audit -r core.txt --no-deps --disable-pip
```

**101 packages, no known vulnerabilities.**

### Optional extras

Each extra resolved from `uv.lock` and audited the same way:

```bash
uv export --frozen --no-hashes --no-emit-project --extra <name> --format requirements-txt > extra.txt
# crewai and entity-ner conflict with the dev/test groups: add --no-default-groups
python -m pip_audit -r extra.txt --no-deps --disable-pip
```

| Extra | Result |
|---|---|
| dashboard, dev, embed-server, entity-ner, iwork, langchain, postgres, pydantic-ai, vector, web, wiki | no known vulnerabilities |
| **crewai** | **`chromadb` 1.5.9: 4 advisories, no fixed release** |

The `[crewai]` advisories, all in the **ChromaDB server**:

| ID | Affects |
|---|---|
| CVE-2026-45829 (GHSA-f4j7-r4q5-qw2c) | Pre-auth code injection via a model repository with `trust_remote_code` on the collections endpoint |
| CVE-2026-45833 (GHSA-36p7-vc44-83pf) | Authenticated code injection, same mechanism, with `UPDATE_COLLECTION` |
| CVE-2026-45831 (GHSA-xph7-9rjv-w5fr) | `SimpleRBACAuthorizationProvider` ignores which tenant a permission applies to |
| CVE-2026-45830 (GHSA-2wm9-hf6c-p5cr) | Authenticated users can read and write any tenant's collections |

**Does this reach m3?** m3 does not run a ChromaDB server. The `[crewai]` extra
lets CrewAI use m3 as its memory store; ChromaDB is installed only as a CrewAI
dependency. A user is exposed only if they start ChromaDB's HTTP server
themselves. The extra is opt-in and not part of a default install.

No fixed ChromaDB release exists, so the floor cannot be raised. The extra stays
listed here until either a fix ships or the extra changes.

---

## Gap since the last scan

The previous published audit was **2026-09-08**, within the quarterly promise.
This scan was run because 2026.10.17.3 changed the security surface (the audit
trail's cross-process lock, the secrets chain, and memory ownership checks).
The core dependency list also grew since then, from 3 declared packages to 28
(101 installed with their dependencies); all of it is covered by the
fresh-environment audit above.
