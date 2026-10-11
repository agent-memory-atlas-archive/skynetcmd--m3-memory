# Good First Issues

New to m3 Memory? These are great starting points. Each issue is self-contained, well-scoped, and comes with existing scripts or tests to guide you.

Open an issue using the [bug report](../.github/ISSUE_TEMPLATE/bug_report.yml), [feature request](../.github/ISSUE_TEMPLATE/feature_request.yml) or [documentation](../.github/ISSUE_TEMPLATE/documentation.yml) template and mention which item below you'd like to work on.

---

## Beginner (no deep internals required)

### 1. Improve error messages in `validate_env.py`
When a dependency is missing or the LLM server is unreachable, the error messages are terse. Add suggestions (e.g., "Try: ollama serve" or "Check LM_ENDPOINTS_CSV in .env").
- File: `validate_env.py`
- Skills: Python, UX

### 2. Add `--json` output flag to `bench_memory.py`
The benchmark script prints results as plain text. Add a `--json` flag to emit structured JSON for CI integration.
- File: `bin/bench_memory.py`
- Skills: Python, JSON

---

## Intermediate (some internals knowledge helpful)

### 3. Add `memory_search` result count to `memory_cost_report`
`memory_cost_report()` tracks embed calls, tokens, writes. Add search call count tracking.
- File: `bin/memory_core.py` (`_COST_COUNTERS`, `memory_cost_report_impl`)
- Skills: Python, SQLite

### 4. Write a `docker-compose.yml` for the full stack
Create a `docker-compose.yml` that spins up PostgreSQL alongside m3 Memory for easy local sync setup.
- Skills: Docker, docker-compose, networking

---

## How to contribute

See [CONTRIBUTING.md](./CONTRIBUTING.md) for full setup instructions, test commands, and PR guidelines.
