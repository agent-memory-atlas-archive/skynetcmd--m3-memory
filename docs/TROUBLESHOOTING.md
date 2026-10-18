# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> m3 Memory: Troubleshooting

## Installation & Upgrade Issues

### "I ran `pipx upgrade m3-memory` and nothing changed"
- **Cause**: m3 was installed via standard `pip`, `pip --user`, or virtualenv rather than `pipx`. `pipx upgrade` exits with status 0 without modifying environments it does not manage.
- **Solution**: Run `m3 upgrade` (or `m3 upgrade --dry-run` to inspect first). It detects the active installation method (`pip`, `pipx`, `pip --user`, or a host plugin) and executes the correct upgrade steps, identically on Windows, macOS and Linux. A host-plugin install is **refused** rather than guessed at, pointing you to the host's own update flow.
- **From a git checkout** you can also invoke it directly: `python bin/m3_upgrade.py`.

### `m3 upgrade` stops: "Windows keeps these m3 programs locked while they run"
- **Cause**: a process is running from one of m3's launchers (`m3.exe`,
  `mcp-memory.exe`): an agent's m3 server, a chat-capture hook, or `m3 upgrade`
  itself when its launcher is a symlink into the install. Replacing a held
  launcher would remove m3 and then fail, so the upgrade stopped first. Nothing
  was changed.
- **Solution**: follow the lines it prints under "To upgrade": close the agent
  session it names (or end the process), upgrade, then reconnect the agent
  (Claude Code: `/mcp`). From 2026.10.6.0, when someone is at the console, a
  launcher held only by `m3 upgrade` itself is no longer an error: the upgrade
  continues in a new window. Unattended runs and earlier versions: run the
  printed `python.exe … m3_upgrade.py` command.

### "m3 is NOT installed now" after a failed upgrade
- **Cause**: the package manager removed m3 and then could not install the new
  version.
- **Solution**: run the restore command the message prints (a
  `pip install --force-reinstall --no-deps m3-memory==<previous version>` using the
  install's own Python), then `m3 setup`.

### m3 doctor lists `M3_*` variables as deprecated
- **What it means**: m3 reads one environment variable, `M3_CONFIG_ROOT`, and
  only when your config root is not the default `~/.m3/config`. Every setting
  lives in `m3_config.toml`; the other `M3_*` names are **not read**. `m3 doctor`,
  `m3 setup` and `m3 upgrade` list each one still set, where it is set, and what
  m3 uses instead (the file's value, or the default when the file does not set
  the key). Nothing warns per tool call.
- **Why it matters**: a leftover variable looks like it controls m3 and does
  not. If you relied on one, its value is no longer in effect.
- **Fix**:
  1. To keep a value, put it in the file: `m3 config set KEY VALUE` (doctor names
     the key). `m3 config import-env` copies them all on request (it shows what it
     will write and asks first); setup does this by itself only when no
     `m3_config.toml` exists yet — an existing file is never overwritten.
  2. Delete the variable where doctor says it is set: the `export` line in
     `~/.zshenv`, `~/.zshrc`, `~/.bashrc` or `~/.profile`; a Windows user
     variable (`setx NAME ""`, or System Properties → Environment Variables);
     an `env` entry in an agent's MCP config; or an `Environment=` line in an m3
     systemd unit / `EnvironmentVariables` in a launchd plist. `m3 doctor --fix`
     removes the pins m3 itself wrote into agent configs and hooks.
  3. Open a new terminal (on macOS, log out and back in if the variable came from
     `launchctl`), restart the agent, and run `m3 doctor` again.
- A `M3_CONFIG_ROOT` that equals the default is flagged as redundant: remove it.
  Secrets (API keys, DSNs) are never read from variables; doctor's secrets section
  says where each one is stored, and `m3 secret set NAME` stores one.

---

## Database Issues

### "database is locked" (SQLite)
- **Cause**: Multiple agents writing simultaneously.
- **Solution**: m3 uses WAL mode and busy timeouts. If the error persists, check for orphaned Python processes:
  - `ps aux | grep python` (Linux/Mac)
  - `tasklist | findstr python` (Windows)

### PostgreSQL sync failures
- **Check**: Scheduled jobs do not see your shell's environment. Store the warehouse DSN with `m3 secret set M3_CDW_PG_URL` (a vault copy under `PG_URL` also works); m3 never reads it from the environment — `m3 config import-env` moves an exported one, and `m3 doctor` names it. See [SYNC.md](SYNC.md#setup). If your *local* store is PostgreSQL too, `M3_PRIMARY_PG_URL` must point at the primary, not the warehouse — see [SYNC_PG_TO_PG.md](SYNC_PG_TO_PG.md).
- **Check**: Confirm the PostgreSQL server is reachable from this machine.
- **Note**: Sync is optional. m3 Memory works fully without PostgreSQL.

---

## Embedding Issues

### "Embedding failed" or "Connection refused"
- **Cause**: The shared local embed server isn't running. m3 ships its own (`m3-embed-server`, from the `m3-core-rs` wheel) and uses it by default on `127.0.0.1:8082` — Ollama and LM Studio are optional alternatives, not the default.
- **Solution**: Check it with `m3 embedder status`, then `m3 embedder start`. If you deliberately route to an external provider, verify `M3_EMBED_URL` points at a live OpenAI-compatible `/v1/embeddings` endpoint.

### Semantic search returning poor results
- **Solution**: Run `memory_maintenance` to decay importance of stale items.
- **Solution**: Check `m3 embedder status` — m3's embedding model is BGE-M3 (1024-dim). Don't point m3 at a different embedding model: vectors from mixed models aren't comparable, so mixing them breaks search.
- **Solution**: Ensure all devices use the same embedding model and dimension (`EMBED_DIM`, default 1024). Mismatched dimensions break cosine similarity.

## FIPS Crypto Issues

FIPS settings live in `m3_config.toml` (`[crypto]`). The older variables
(`M3_FIPS_MODE`, `M3_FIPS_STRICT`, `M3_WOLFSSL_SHA256`) are not read; see [m3 doctor lists `M3_*` variables](#m3-doctor-lists-m3_-variables-as-deprecated).

### Crash / RuntimeError: "FIPS mode enabled but … wolfSSL … could not be loaded"
- **Cause**: `crypto.fips_mode` (or `crypto.fips_strict`) is on, but the wolfSSL
  library isn't installed in a trusted path. FIPS mode **fails closed** by design —
  it will not silently fall back to non-wolfCrypt crypto.
- **Solution** — install wolfSSL, then retry:
  ```bash
  m3 fips install-wolfssl      # builds open-source wolfSSL into ~/.m3/lib
  m3 doctor                    # confirm the "crypto (FIPS)" section shows it loaded
  ```
- **Or** turn FIPS off for now: `m3 config set crypto.fips_mode false` (and
  `crypto.fips_strict false`), and remove any `M3_FIPS_*` variable still set.

### "crypto.fips_strict requires the CMVP-validated wolfCrypt FIPS module … OPEN-SOURCE build"
- **Cause**: strict mode requires the **commercial, CMVP-validated** wolfCrypt
  FIPS module, but you have the free **open-source** wolfSSL build.
- **Solution**: For a validated FIPS 140-3 module, obtain it through wolfSSL's
  commercial channel. For homelab/dev (hardened wolfCrypt without the license),
  keep `crypto.fips_mode true` and set `crypto.fips_strict false`.

### "wolfSSL … failed integrity pin"
- **Cause**: The wolfSSL library's SHA-256 doesn't match `crypto.wolfssl_sha256`.
  Expected after you intentionally rebuild/upgrade wolfSSL; **investigate** if you
  didn't.
- **Solution**: If you trust the new build, re-pin: `m3 doctor` prints the loaded
  library's SHA-256; `m3 config set crypto.wolfssl_sha256 <that value>`.

### FIPS mode refuses the PostgreSQL connection
Symptom: sync to the warehouse (or a PostgreSQL primary store) fails with *"FIPS
mode refuses a PostgreSQL connection that can fall back to plaintext or skip the
server's certificate"*, and `m3 doctor`'s crypto section lists the path as
refused, with a `fix:` line.

- **Cause**: under FIPS mode a networked PostgreSQL connection must verify the
  server: `sslmode=verify-full` plus the server's CA certificate as
  `sslrootcert`. A DSN with `sslmode=prefer` (libpq's default), `require` or
  `verify-ca` can skip that check, so m3 refuses it. A local Unix socket is not
  affected.
- **Solution**:
  1. Get the CA certificate that signed the server's certificate. Ask whoever runs
     the server, or read the chain the server presents and keep its last
     (root) certificate:
     ```bash
     openssl s_client -starttls postgres -connect <host>:5432 -showcerts </dev/null
     ```
     Copy the last `-----BEGIN CERTIFICATE-----` … `-----END CERTIFICATE-----`
     block into a file in your config root, e.g. `~/.m3/config/pg_root.pem`.
     **Check its fingerprint** with the server's operator
     (`openssl x509 -in ~/.m3/config/pg_root.pem -noout -fingerprint -sha256`)
     before trusting it: reading it over the network alone proves nothing.
  2. Build the DSN (placeholders; never paste a real password into a ticket or
     chat):
     ```text
     postgresql://<user>:<password>@<host in the certificate>:5432/<db>?sslmode=verify-full&sslrootcert=<config root>/pg_root.pem
     ```
     If you connect by an address or a name that is not in the certificate, keep
     the certificate's name as the host and add `hostaddr=<address>`:
     `postgresql://…@<name in certificate>:5432/<db>?hostaddr=<address>&sslmode=verify-full&sslrootcert=…`.
     On Windows, write the `sslrootcert` path with forward slashes.
  3. Store it (it is read from standard input, so it never lands in your shell
     history):
     ```bash
     m3 secret set M3_CDW_PG_URL --keyring --stdin       # the sync warehouse
     m3 secret set M3_PRIMARY_PG_URL --keyring --stdin   # a PostgreSQL primary store
     ```
  4. Run `m3 doctor`: the refused path should be gone. Then run a sync.
- **For an AI agent helping a user**: never ask for or echo the DSN or password.
  Have the user run the `m3 secret set … --stdin` command themselves (in Claude
  Code, `! <command>`), and confirm with `m3 doctor`, which prints DSNs masked.

See [FIPS_MODULE_BOUNDARY.md](FIPS_MODULE_BOUNDARY.md) for the full model.

## Scheduled Jobs

### The hourly sync (or another background job) is not running
- **Check**: `m3 schedules verify` — it checks each job is present, loaded, and
  has the interval and command its spec defines (launchd agents on macOS, m3's
  crontab block on Linux, Task Scheduler on Windows).
- **Solution**: `m3 schedules repair` re-installs them. On macOS a hand-made
  `com.m3memory.sync_all.plist` is replaced and saved as a `.bak-` copy.

## Scheduled Task Visibility

### Focus-stealing command prompt windows (Windows)
- **Cause**: Older installs registered the `AgentOS_*` scheduled tasks to run
  through `cmd.exe`, which draws a console window on screen every time a task
  fires (every 15-30 minutes for the busy ones).
- **Fix**: Run the fix script — it self-elevates (accept the UAC prompt), so
  you can start it from a normal terminal:
  ```powershell
  powershell -ExecutionPolicy Bypass -File bin\fix_scheduled_tasks.ps1
  ```
  It re-registers all tasks with `pythonw.exe` (no console subsystem → no
  window) and prints a before/after summary.
- **Equivalent manual fix**: in an **Administrator** terminal, run the
  installer directly:
  ```powershell
  m3 schedules repair
  ```
- **Note**: the older `-Hidden` / `Set-ScheduledTask ... Hidden` trick does
  **not** fix this — it only hides the task's entry in the Task Scheduler UI,
  not the console window. Use the fix above instead.
- **macOS / Linux**: not affected — cron jobs never draw a window. Just run
  `m3 schedules add all` normally.

---

## Installation Issues

### "m3: command not found"
- **Cause**: The package isn't installed or isn't on your PATH.
- **Solution**:
  ```bash
  pip install m3-memory
  which m3  # should return a path (the older `mcp-memory` alias also works)
  ```

### Memory tools vanish mid-session ("m3 is unreachable", tools disappear)

The server was working and then the tools disappeared partway through a session
— often after a long build or test run.

**Run `/mcp` in that session.** The tools come back immediately. This is a
low-risk, routine action: ~0.9 s including cold start, idempotent, and it
cannot lose data — the bridge is a stateless adapter in front of the database,
so a fresh one opens the same store.

**Your memory is not lost.** Chatlog capture writes to the database directly and
does not travel over the MCP connection, so a dropped connection costs you
searching and writing for a few seconds, not captured turns. Confirm with:

```bash
m3 chatlog doctor          # capture.healthy + a recent last_write_at = nothing lost
```

This is a client-side MCP lifecycle limitation, not an m3 failure — the client
owns the pipe to the server process, so nothing on the m3 side can reconnect it.
**Any agent that reaches m3 over stdio is exposed to this** (Claude Code,
Antigravity, Gemini CLI, OpenCode all are); we have confirmed it in Claude Code
and not yet verified the others.
**[MCP_DISCONNECTS.md](MCP_DISCONNECTS.md)** has the full explanation: the
mechanism, the measured evidence that no data is lost, the upstream issue
references, and why we deliberately did not switch the default transport.

(Distinct from the entry below, which covers a server that never appeared at all.)

### Memory server doesn't appear in agent
- Verify the JSON in your agent's config file is valid.
- Make sure the key is `"mcpServers"` (case-sensitive).
- **Claude Code** ignores `mcpServers` in `~/.claude/settings.json`; check
  `claude mcp list` for `m3_memory` (or the plugin), and re-run `m3 setup` or
  `claude mcp add --scope user -- m3_memory m3` if it's missing.
- Restart the agent completely (not just a new session).

### Agent can't find previous memories
- Memories live under the **engine root**: `~/.m3/engine/agent_memory.db` by
  default, or `paths.engine_root` in `m3_config.toml`. Ask m3 rather than
  guessing: `m3 chatlog status --json` reports the resolved backend and roots,
  and `m3 config show` says where each setting came from. `M3_ENGINE_ROOT` and
  `M3_MEMORY_ROOT` are not read; what can split the store is a non-default
  `M3_CONFIG_ROOT` set in one place (a shell profile, an agent's MCP `env` block)
  and not another. `m3 doctor` names the config file in use and every place that
  points elsewhere.
- ⚠ `~/.m3-memory/` is the legacy memory root. It holds no live database — a DB
  found there is a leftover from the old unified layout, not your live store.
  `m3 doctor --fix` migrates its state into `~/.m3` and leaves a `MOVED.txt`.
- The bridge resolves the DB from those roots regardless of the directory
  `m3` was launched from.
