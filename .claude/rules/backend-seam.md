---
paths:
  - "bin/memory/backends/**/*.py"
  - "bin/memory/search_pg.py"
  - "bin/pg_sync.py"
  - "bin/pg_fdw_sync.py"
  - "bin/migrate_pg.py"
  - "tests/conftest.py"
---

# The backend seam

- **Registry key `db.backend` is the ONLY backend selector** (default `sqlite`):
  a flag (`set_from_cli` / `--set`), then `[db] backend` in m3_config.toml, then
  `M3_DB_BACKEND` only as a legacy name BELOW the file (warned once; until N+3).
  A test switches it with
  `select_backend(name)` (a flag) or `use_backend(name, dsn)` (the file), never
  `setenv`. `M3_PRIMARY_PG_URL` never selects the
  backend — it only says where PostgreSQL is. A lane that sets the DSN without the selector silently tests SQLite; that
  shipped once, and a CI job titled "PostgreSQL lane" had no PG coverage.
- **`conftest.pg_dsn()`** is the `pytest --m3-pg-url` option and nothing else:
  no env fallback, and **never** a warehouse DSN (that is production). A test
  selects PostgreSQL with `use_backend("postgres", pg_dsn())`, which writes the
  sandbox's config and secrets files the way a user's setup does.
- **Secrets never come from the environment**: the chain is the
  secrets file, then the keyring entry it points at (or `system/NAME`), then the
  vault. A test gives m3 a secret with `secret_store.set_secret`, not `setenv`.
- **`requires_pg` gates on REACHABILITY**, not on the seam being PG. A DSN being
  *present* is not the cluster being *up*; treating it as such costs a connect
  timeout per test instead of a skip.
- **The backend NAME is not memoized** — `resolve_backend_name()` reads the
  setting on every call (a flag is live at once; a file change within the
  snapshot's TTL). A
  module-global memo lives per module instance, and two selector instances with disagreeing memos sent PG-marked tests to SQLite ("the
  23"); do not reintroduce one. Backend INSTANCES (`_backends`) are still cached
  and cleared by `_reset_for_tests()`. Anything that resolves a backend at
  import — module constants, class bodies, class setup — runs before the test
  sandbox can reach it and will bind the wrong store.
  `tests/test_selector_instances_agree.py` pins this.
- **Vary the DSN SHAPE, not just the host.** Socket vs TCP, user vs no user,
  password vs peer are different code paths. `postgresql:///db` carries no host
  and no user; writing either into SQL as `NULL` is a syntax error. Three TCP
  boxes tested one shape three times and missed it.

# `dialect` is a callable whose name collides with a submodule

`memory.backends` re-exports a `dialect` **accessor function** and also has a
`dialect` **submodule**. Binding the submodule onto the package turns every
`from memory.backends import dialect; dialect()` call site into
`TypeError: 'module' object is not callable`, raised far from the cause with
nothing naming it. That shipped twice: 96 of it across 89 tests (2026-09-14) and
141 across 135 tests on the Windows PostgreSQL lane (2026-10-02).

Two defences exist and both must stay:

- the import-time `if not callable(dialect)` check, which catches a REORDERING
  of the imports in `__init__.py`;
- `_BackendsModule.__setattr__`, which refuses a LATER rebind and logs the file
  that attempted it. The import-time check cannot see that case, which is why
  both incidents produced no diagnostic.

So: never replace or stub `sys.modules["memory.backends"]`, and never bind the
submodule onto the package. Import the accessor as
`from memory.backends import dialect` and module symbols as
`from memory.backends.dialect import dialect_for, ...`.
`tests/test_backends_dialect_not_rebound.py` pins it.

# Registration lands in whichever registry instance is live

`@register_backend` resolves `register_backend` through
`sys.modules["memory.backends.registry"]`. Code holding an earlier instance sees
an empty `_REGISTRY` and raises "no dialect registered" for a shipped backend,
so `_ensure_registered` adopts the live entry. When testing that, load the rival
instance from file — copying a module's `__dict__` leaves its functions pointing
at the original globals, and the test cannot fail.
