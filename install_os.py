import getpass
import ntpath
import os
import posixpath
import subprocess
import sys
import venv


def _os_name() -> str:
    """WMI-safe OS name. Replaces platform.system(), which routes through a WMI
    query that can hang on Py3.14/Windows. This is a standalone pre-install
    script (stdlib only, runs before m3 is importable), so the helper is inlined
    rather than shared. os.name/sys.platform are constants — no WMI."""
    if os.name == "nt":
        return "Windows"
    if sys.platform == "darwin":
        return "Darwin"
    return "Linux"


# On Windows the default console code page is cp1252, which can't encode
# characters outside that 8-bit range (emoji, arrows, box-drawing). A stray
# non-ASCII char in a print() (e.g. the banner's rocket emoji) crashes the
# whole installer with UnicodeEncodeError. Force stdio onto UTF-8 so output is
# safe regardless of console code page. Guard with hasattr: .reconfigure()
# exists on the real TextIOWrapper but not on substitutes (StringIO under tests).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def get_m3_root() -> str:
    """Returns the M3 root directory for user state (~/.m3-memory)."""
    # env-owner: the root locator of a stdlib-only pre-install script (config lives under a root)
    root = os.getenv("M3_MEMORY_ROOT")
    if root:
        # Do NOT abspath a root that is already absolute: os.path.abspath only
        # understands the host convention, so on Windows a POSIX root like
        # /data/.m3 (exported from Git Bash or WSL) reads as relative and would
        # be rewritten to C:\data\.m3. Mirrors bin/m3_core/paths.py::_abs_root.
        p = os.path.expanduser(root)
        if posixpath.isabs(p) or ntpath.isabs(p):
            return p.replace("\\", "/")
        return os.path.abspath(p).replace("\\", "/")
    return os.path.join(os.path.expanduser("~"), ".m3-memory")


M3_ROOT = get_m3_root()


def _is_installed_layout(root: str) -> bool:
    """True when `root` is an INSTALLED package dir (…/site-packages/… or
    …/dist-packages/…) rather than a source checkout.

    Inlined, not imported: this script is stdlib-only and runs BEFORE m3 is
    importable (see _os_name above for the same reasoning), so it cannot reach
    `bin/generate_configs.py::_is_installed_layout`. Keep the two in sync —
    `tests/test_install_os_no_nested_venv.py` asserts they agree.
    """
    r = root.replace("\\", "/")
    return "/site-packages/" in r or "/dist-packages/" in r


# On a wheel/pipx install BASE_DIR IS site-packages/m3_memory, so BASE_DIR/.venv
# is a venv nested INSIDE the installed package — issue #164's artifact, built
# with `--copies` and pinned to an exact interpreter patch version, so it breaks
# silently on the next patch bump. #142 guarded bin/setup_memory.py the same way;
# this script was the remaining creator (it runs from _post_install on EVERY
# install and upgrade, not just first install). On an installed layout the
# interpreter already running this code IS the payload's interpreter: use it and
# create nothing.
_INSTALLED = _is_installed_layout(BASE_DIR)
VENV_DIR = None if _INSTALLED else os.path.join(BASE_DIR, ".venv")
DB_DIR = os.path.join(M3_ROOT, "memory")
LOGS_DIR = os.path.join(M3_ROOT, "logs")
REQ_FILE = os.path.join(BASE_DIR, "requirements.txt")


def run_cmd(cmd, env=None, *, optional=False, label=None, stdin_text=None):
    """Run a command. Aborts the install on failure unless ``optional``.

    ``optional=True`` is for steps whose failure does not break the install:
    they print ONE quiet line instead of a red multi-line dump plus sys.exit.
    The bar is strict -- a step qualifies only when m3 works correctly without
    it. This is not a way to silence a real failure, and the default stays
    fatal.

    Why it exists: the pip self-upgrade below is cosmetic, but a fatal run_cmd
    turned it into "OS setup failed (code 1)" on a routine Windows upgrade. A
    scary error for a step that changes nothing teaches users to ignore the
    next one, which might be real.

    ``stdin_text`` is written to the child's stdin: the channel for a secret,
    which must not travel in argv or the child's environment.
    """
    print(f"Running: {label or ' '.join(cmd)}" if stdin_text is not None else f"Running: {' '.join(cmd)}")
    extra_kw = {}
    if stdin_text is not None:
        extra_kw["input"] = stdin_text
        if not optional:  # optional already passes text=True
            extra_kw["text"] = True
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        extra_kw["startupinfo"] = si
    result = subprocess.run(
        cmd,
        env=env,
        **({"capture_output": True, "text": True} if optional else {}),
        **extra_kw,
    )
    if result.returncode != 0:
        if optional:
            print(f"  -> skipped: {label or ' '.join(cmd)} (non-critical, exit {result.returncode})")
            return False
        print(f"\n[ERROR] Command failed: {' '.join(cmd)}", file=sys.stderr)
        sys.exit(result.returncode)
    return True


# Run in the target interpreter, where the payload's dependencies are installed.
# The key arrives on stdin, never in the child's environment or argv (both are
# readable by other processes of the same user); argv[1] is the payload's bin/.
# m3_core.secret_store is the one owner of storing a secret: it writes the OS
# keyring (service "system"), verifies the read-back, and points the secrets file
# at the entry. Its errors name what failed, never the value.
_STORE_MASTER_KEY = """
import sys
sys.path.insert(0, sys.argv[1])
value = sys.stdin.read().strip()
try:
    from m3_core import secret_store
    where = secret_store.store_in_keyring("AGENT_OS_MASTER_KEY", value)
    print("Saved AGENT_OS_MASTER_KEY (" + where + ").")
except Exception as e:
    print("Failed to store AGENT_OS_MASTER_KEY: " + type(e).__name__ + ": " + str(e))
    print("Store it later from a terminal: m3 secret set AGENT_OS_MASTER_KEY --keyring")
"""


def _store_master_key(python_exe, master_key):
    """Store the key through m3_core.secret_store in ``python_exe``. Non-fatal:
    the install works without it, and the failure is printed with the fix."""
    run_cmd(
        [python_exe, "-c", _STORE_MASTER_KEY, os.path.join(BASE_DIR, "bin")],
        stdin_text=master_key,
        label="store AGENT_OS_MASTER_KEY",
    )


def setup_master_key(python_exe):
    """Prompts the user to configure the AGENT_OS_MASTER_KEY using the python keyring."""
    print("\n" + "=" * 50)
    print("🔐 ZERO-KNOWLEDGE ENCRYPTED VAULT SETUP")
    print("=" * 50)
    print("To sync API keys securely across devices, you must provide the AGENT_OS_MASTER_KEY.")
    print("This key will be securely stored in your native OS keyring (macOS Keychain,")
    print("Windows Credential Manager, or Linux Secret Service) and NEVER synced.")

    # Non-interactive path (Claude Code Bash / SSH / CI): getpass() on a stdin
    # with no TTY raises EOFError, which used to abort the ENTIRE post-install
    # (incl. later config regen). Never prompt without a TTY: skip (it is already
    # "press Enter to skip" for interactive users) and say how to store it later.
    # A key seen only in this process's environment is NAMED, never stored: m3
    # stores secrets from the console or from persistent places only
    # (`m3 config import-env`), and never reads them from the environment.
    if not sys.stdin.isatty():
        # env-owner: names a secret still set in the environment (never reads or stores its value)
        in_env = bool(os.environ.get("AGENT_OS_MASTER_KEY", "").strip())
        print(
            "Non-interactive session: skipping master key prompt."
            + (
                " AGENT_OS_MASTER_KEY is set in the environment, which m3 does not read or store for secrets."
                if in_env
                else ""
            )
            + " Store it later from a terminal: `m3 secret set AGENT_OS_MASTER_KEY --keyring` (prompts),"
            " or `m3 config import-env` (moves it from your rc files, .env, MCP config and registry)."
        )
        return

    while True:
        master_key = getpass.getpass("\nEnter the AGENT_OS_MASTER_KEY (or press Enter to skip for now): ").strip()
        if not master_key:
            print("Skipping master key setup. You will need to configure it later to use synced API keys.")
            break

        confirm_key = getpass.getpass("Confirm AGENT_OS_MASTER_KEY: ").strip()
        if master_key == confirm_key:
            _store_master_key(python_exe, master_key)
            break
        else:
            print("❌ Passwords do not match. Please try again.")


def install_node_manager():
    """
    Handles Node.js version management setup.
    Windows: Prefers nvm-windows (CoreyButler.NVMforWindows).
    Unix/macOS: Prefers nvm or fnm.
    """
    system = _os_name()
    print(f"\n[*] Setting up Node.js Manager for {system}...")

    if system == "Windows":
        # 1. Check if nvm is already installed
        try:
            res = subprocess.run(["nvm", "version"], capture_output=True, text=True)
            if res.returncode == 0:
                print(f"  -> Found existing nvm: {res.stdout.strip()}")
                return
        except (FileNotFoundError, PermissionError):
            pass

        # 2. Check for existing standalone Node.js (vulnerable to conflicts)
        try:
            res = subprocess.run(["node", "--version"], capture_output=True, text=True)
            if res.returncode == 0:
                print(f"  ⚠️ Warning: Found standalone Node.js ({res.stdout.strip()}).")
                print("     nvm-windows works best if standalone Node.js is uninstalled first.")
        except (FileNotFoundError, PermissionError):
            pass

        # 3. Try to install nvm-windows via winget
        print("  -> Attempting to install nvm-windows via winget...")
        try:
            subprocess.run(["winget", "--version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            res = subprocess.run(
                [
                    "winget",
                    "install",
                    "-e",
                    "--id",
                    "CoreyButler.NVMforWindows",
                    "--silent",
                    "--accept-package-agreements",
                    "--accept-source-agreements",
                ]
            )
            if res.returncode == 0:
                print("  ✅ nvm-windows installed successfully!")
                print("     RESTART YOUR TERMINAL to use it.")
                return
            else:
                print("  -> winget install failed. You may need to install it manually.")
        except (subprocess.CalledProcessError, FileNotFoundError, PermissionError):
            print(
                "  -> winget not found. Please install nvm-windows manually: https://github.com/coreybutler/nvm-windows/releases"
            )

    else:
        # Unix/macOS: Prefer fnm or nvm
        print("  -> Setting up Node.js manager for Unix...")
        try:
            subprocess.run(["fnm", "--version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("  -> fnm already installed.")
        except (subprocess.CalledProcessError, FileNotFoundError, PermissionError):
            # PermissionError: fnm binary exists on the system but is not
            # executable by this user (e.g. installed system-wide for another
            # user). Treat as not installed and install into the user's home.
            run_cmd(["bash", "-c", "curl -fsSL https://fnm.vercel.app/install | bash -s -- --skip-shell"])
            print("  -> fnm installed. Please follow the terminal instructions to add it to your shell.")


def setup_oxidation():
    """Prompts the user to install Project Oxidation (Rust compute core).

    Delegates to m3_memory.rust_core_install, which:
      - Auto-detects the right backend (Metal on macOS, CUDA/Vulkan/CPU on
        Linux+Windows) and picks the matching prebuilt PyPI wheel
        (m3-core-rs-<os>-<backend>).
      - Falls back to a source build with the correct Cargo features
        (--features embedded-metal / embedded-cuda / embedded-vulkan) if no
        prebuilt wheel matches this platform/Python.
      - Installs into the *current* Python (sys.executable — the pipx venv
        where mcp-memory itself runs), NOT a sibling repo/.venv that
        mcp-memory can't see.

    The non-interactive code path (env M3_INSTALL_OXIDATION=1/0) lets
    install.sh / install.ps1 drive this unattended. The interactive prompt
    defaults to YES (empty answer or closed stdin opts in); only an explicit
    "n"/"no" — or M3_INSTALL_OXIDATION=0 — skips. This makes unattended SSH
    installs get the recommended native hot path instead of silently
    degrading to pure-Python.
    """
    print("\n" + "=" * 50)
    print("🦀 PROJECT OXIDATION: HIGH-PERFORMANCE RUST CORE")
    print("=" * 50)
    print("Project Oxidation replaces critical Python hot-paths with")
    print("optimized Rust primitives. Per-operation speedups (Rust vs Python,")
    print("FFI-inclusive; see docs/OXIDATION_BENCHMARKS.md):")
    print("  - Packed MMR rerank: up to ~846x at a realistic candidate pool.")
    print("  - Packed batch-cosine: ~97-178x.  Chatlog redaction: 11-15x.")
    print("  - FTS compile + token-Jaccard: 1.4-10x.")
    print("  - Trivially small C-backed ops stay on Python where it's faster.")
    print("\n[NOTE] Prebuilt wheels are tried first (no toolchain needed).")
    print("Source fallback requires Rust + a C/C++ compiler.")

    # Non-interactive override for install.sh / install.ps1.
    env_choice = os.environ.get("M3_INSTALL_OXIDATION", "").strip().lower()
    if env_choice in ("0", "false", "no", "n"):
        choice = "n"
    elif env_choice in ("1", "true", "yes", "y"):
        choice = "y"
    else:
        try:
            ans = input("\nWould you like to install Project Oxidation now? [Y/n]: ").strip().lower()
            # Default ON: installing the prebuilt wheel is a SAFE attempt — it
            # never auto-compiles from source here, and m3 stays fully
            # functional in pure-Python if no wheel matches. An empty answer
            # (just Enter) opts in. Matches setup_wizard Step 3 (default=True).
            choice = "n" if ans in ("n", "no") else "y"
        except EOFError:
            # Piped/closed stdin (e.g. unattended SSH install). Default to the
            # recommended install rather than silently skipping it; the wheel
            # attempt is non-fatal and falls back to pure-Python on a miss.
            choice = "y"

    if choice in ("y", "yes"):
        if _os_name() == "Windows":
            print("\n[*] Checking for Windows C++ Build Tools...")
            script = os.path.join(BASE_DIR, "install_oxidation_buildtools.ps1")
            if os.path.exists(script):
                print("  -> If the source-fallback build fails, run this script in an")
                print("     ADMINISTRATOR PowerShell first:")
                print(f"       powershell -ExecutionPolicy Bypass -File {script}")

        print("\n[*] Installing m3-core-rs (prebuilt wheel preferred)...")
        try:
            # Local import: m3_memory is importable here because install_os.py
            # is launched under the same interpreter that runs mcp-memory (the
            # pipx venv). This avoids the historical bug where setup_oxidation
            # installed into repo/.venv/bin/pip, leaving the pipx-installed
            # mcp-memory unable to import m3_core_rs.
            from m3_memory.rust_core_install import install_rust_core
        except ImportError as e:
            print(f"  ⚠️  m3_memory.rust_core_install not importable ({e}).")
            print("     Skipping Oxidation. Install later via: m3 embedder install-gpu")
            return

        # allow_source_fallback=False: the curl-install.sh flow should not
        # silently launch a multi-minute Rust+cmake build that surprises the
        # user. If both prebuilt paths miss, install_rust_core prints a
        # multi-line recommendation (prereqs + manual command). The user can
        # then opt in deliberately via `m3 embedder install-gpu`.
        rc = install_rust_core(allow_source_fallback=False)
        if rc == 0:
            print("✅ Project Oxidation installed successfully!")
        else:
            # install_rust_core already printed the recommendation. Keep
            # this terse — repeating it would just push the helpful info
            # off the user's screen.
            print(f"\n⚠️  Project Oxidation prebuilt unavailable (exit {rc}).")
            print("    Embeddings still work via the tier-2 HTTP fallback.")
            print("    See above for source-build steps if you want native speed.")
    else:
        print("Skipping Project Oxidation. Install it later via:\n  m3 embedder install-gpu")


def main():
    print("\n" + "=" * 50)
    print("🚀 M3 MAX AGENTIC OS: UNIVERSAL INSTALLER")
    print("=" * 50)

    # 0. Setup Node.js Management
    install_node_manager()

    # 1. Ensure auxiliary directories exist
    print("\n[1/6] Creating auxiliary directories...")
    os.makedirs(DB_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)
    print(f"  -> {DB_DIR}")
    print(f"  -> {LOGS_DIR}")

    # 2. Create Virtual Environment
    print("\n[2/6] Setting up isolated Python environment...")
    if _INSTALLED:
        # Installed layout: the running interpreter already owns the payload and
        # its dependencies. Creating a venv here would nest one inside
        # site-packages (#164/#173). Use `python -m pip` rather than a pip
        # executable — a pipx venv has no `pip.exe` of its own on Windows.
        python_exe = sys.executable
        pip_exe = None
        print(f"  -> Installed layout — using the running interpreter, creating no venv.\n     {python_exe}")
    else:
        if not os.path.exists(VENV_DIR):
            venv.create(VENV_DIR, with_pip=True)
            print("  -> Created new virtual environment.")
        else:
            print("  -> Virtual environment already exists.")

        # Determine paths for pip and python inside venv based on platform
        if sys.platform == "win32":
            pip_exe = os.path.join(VENV_DIR, "Scripts", "pip.exe")
            python_exe = os.path.join(VENV_DIR, "Scripts", "python.exe")
        else:
            pip_exe = os.path.join(VENV_DIR, "bin", "pip")
            python_exe = os.path.join(VENV_DIR, "bin", "python")

    # One spelling for "install a package", valid in both layouts.
    pip_cmd = [pip_exe] if pip_exe else [python_exe, "-m", "pip"]

    # 3. Install dependencies
    print("\n[3/6] Installing cross-platform dependencies...")
    # `python -m pip`, never `pip.exe`: on Windows pip REFUSES to replace its own
    # running executable and exits non-zero with
    #   "ERROR: To modify pip, please run: <python> -m pip install --upgrade pip"
    # That is pip working as designed, not an m3 failure -- but run_cmd's default
    # is fatal, so it aborted the whole OS-setup step behind a red multi-line
    # dump during an upgrade. Users reasonably read that as severe.
    #
    # Optional either way: a current pip is a convenience, not a requirement.
    # Every dependency install below works on the pip the venv shipped with.
    run_cmd([python_exe, "-m", "pip", "install", "--upgrade", "pip"], optional=True, label="pip self-upgrade")
    if os.path.exists(REQ_FILE):
        run_cmd([*pip_cmd, "install", "-r", REQ_FILE])
    else:
        print(f"  -> Warning: {REQ_FILE} not found. Installing defaults...")
        run_cmd([*pip_cmd, "install", "fastmcp", "httpx", "numpy", "keyring", "cryptography", "psycopg2-binary"])

    # 4. Project Oxidation (Rust Core)
    # No pip_exe arg — setup_oxidation installs into the current interpreter
    # (the pipx venv where mcp-memory runs), not into repo/.venv.
    setup_oxidation()

    # 5. Initialize local SQLite Database Schema
    print("\n[5/6] Initializing local Agent Memory schema...")
    migrate_script = os.path.join(BASE_DIR, "bin", "migrate_memory.py")
    if os.path.exists(migrate_script):
        run_cmd([python_exe, migrate_script])
    else:
        print(f"  -> Warning: Migration script {migrate_script} not found.")

    # 6. Secure Auth Setup
    print("\n[6/6] Initializing Security Layer...")
    setup_master_key(python_exe)

    # 7. Initial Data Warehouse Sync
    print("\n[7/7] Connecting to PostgreSQL data warehouse...")
    pg_sync_script = os.path.join(BASE_DIR, "bin", "pg_sync.py")
    if os.path.exists(pg_sync_script):
        print("Executing initial bi-directional synchronization...")
        try:
            # Don't fail the whole install if the homelab is unreachable
            subprocess.run([python_exe, pg_sync_script], check=True)
            print("✅ Initial sync successful!")
        except subprocess.CalledProcessError:
            print("⚠️ Initial sync failed. You may not be on the target private network.")
            print("   The sync will run automatically later when the network is reachable.")
    else:
        print("  -> Warning: Sync script not found.")

    # 8. Generate MCP configs (.mcp.json, claude-settings.json, gemini-settings.json)
    print("\n[8/8] Generating MCP configuration files...")
    gen_config_script = os.path.join(BASE_DIR, "bin", "generate_configs.py")
    if os.path.exists(gen_config_script):
        run_cmd([python_exe, gen_config_script])
    else:
        print(f"  -> Warning: {gen_config_script} not found. Run it manually after install.")

    print("\n" + "=" * 50)
    print("🎉 INSTALLATION COMPLETE!")
    print("=" * 50)
    print(f"M3 Memory is now fully initialized for {_os_name()}.")
    print("\nMCP bridges are configured in .mcp.json — Claude Code will load them automatically.")
    print("\nNext Steps:")
    print("  1. (Recommended) Install a self-contained local embedder for Hybrid Search:")
    print(f"     {python_exe} -m m3_memory.cli install-embedder")
    print("\nNext Steps for scheduling automated background syncs:")

    if sys.platform == "win32":
        print("  Use Windows Task Scheduler to run:")
        print(f"    {python_exe} {pg_sync_script} hourly.")
    else:
        print("  Add the following to your crontab (crontab -e):")
        print(
            f"    0 * * * * {os.path.join(BASE_DIR, 'bin', 'pg_sync.sh')} >> {os.path.join(LOGS_DIR, 'cron.log')} 2>&1"
        )

    print("=" * 50 + "\n")


if __name__ == "__main__":
    main()
