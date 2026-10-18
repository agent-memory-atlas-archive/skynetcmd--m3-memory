"""WMI-safe OS detection.

On Windows + CPython 3.14, ``platform.system()`` / ``platform.machine()`` route
through ``platform.uname()`` → a WMI query (``_win32_ver`` / ``_wmi_query``) that
can hang indefinitely on a slow or contended WMI service. That freezes any code
path calling them — installer, setup wizard, scheduled-task entrypoints, the
dashboard. ``os.name`` / ``sys.platform`` are module constants that give the same
OS distinction with no WMI, no stall.

Use ``os_name()`` anywhere the legacy code called ``platform.system()`` for a
Windows/Linux/Darwin comparison.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from types import ModuleType
from typing import Any, NoReturn

# The sys.platform value that names each supported OS. sys.platform is the ONE
# knob this module reads to tell OSes apart, so it is also the one a test turns to
# present another OS (tests/conftest.py simulate_os). Not os.name: pathlib picks
# WindowsPath/PosixPath from os.name, so patching it breaks every later Path().
# On a real system the two always agree (win32 <-> nt).
PLATFORM_OF: dict[str, str] = {"Windows": "win32", "Darwin": "darwin", "Linux": "linux"}

_UNSUPPORTED_WARNED = False

# The persistent per-user environment store, where the OS has one (Windows:
# HKCU\Environment, what `setx` writes). Bound at import: a test that later
# installs a fake `winreg` in sys.modules cannot redirect these readers.
try:
    import winreg as _winreg_module

    _winreg: ModuleType | None = _winreg_module
except ImportError:  # POSIX: no such store (shell rc files are text, not this)
    _winreg = None


def user_env_snapshot(reg: Any | None = None) -> dict[str, tuple[object, int]] | None:
    """The persistent per-user environment as {name: (value, registry type)}, or
    None where the OS keeps no such store. The capability is the store, not the
    OS name. ``reg``: the registry module to read through (the test sandbox's
    guard passes the real one, which a test's fake may have replaced here)."""
    reg = reg if reg is not None else _winreg
    if reg is None:
        return None
    out: dict[str, tuple[object, int]] = {}
    with reg.OpenKey(reg.HKEY_CURRENT_USER, "Environment") as key:
        i = 0
        while True:
            try:
                name, value, kind = reg.EnumValue(key, i)
            except OSError:
                break
            out[name] = (value, kind)
            i += 1
    return out


def user_env_get(name: str) -> str | None:
    """One value from the persistent per-user environment; None when unset or
    where the OS keeps no such store. Unreadable is treated as unset."""
    if _winreg is None:
        return None
    try:
        with _winreg.OpenKey(_winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _kind = _winreg.QueryValueEx(key, name)
    except OSError:  # FileNotFoundError: not set; others: unreadable
        return None
    return str(value)


def vscode_user_dir() -> str:
    """VS Code's per-user data directory (extensions such as Cline keep their
    settings under its ``globalStorage``): ``%APPDATA%/Code/User`` on Windows,
    ``~/Library/Application Support/Code/User`` on macOS, ``~/.config/Code/User``
    elsewhere. A string, joined with os.path, so a simulated OS keeps the host's
    separator (as the roots do)."""
    home = os.path.expanduser("~")
    if is_windows():
        base = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")  # its default when unset
        return os.path.join(base, "Code", "User")
    if is_macos():
        return os.path.join(home, "Library", "Application Support", "Code", "User")
    return os.path.join(home, ".config", "Code", "User")


def local_app_data() -> str | None:
    """Windows' per-user local application folder (``%LOCALAPPDATA%``, where
    per-user installers such as agy's put programs); None on other OSes."""
    if not is_windows():
        return None
    return os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")


@dataclass(frozen=True, slots=True)
class VolumeEncryption:
    """Whether the disk holding a path is encrypted: True / False, or None when
    it could not be told; ``detail`` says what was read (or why not)."""

    encrypted: bool | None
    detail: str
    keyring: str  # what this OS's keyring is called, for a message


def _probe(argv: list[str]) -> tuple[int, str]:
    """Run a short read-only OS query windowless; (exit code, stdout)."""
    import subprocess

    try:
        cp = subprocess.run(  # nosec B603 - argv list, no shell
            argv, capture_output=True, text=True, timeout=20, check=False, **hidden_window_kwargs()
        )
    except (OSError, subprocess.SubprocessError) as e:
        return -1, f"{type(e).__name__}: {e}"
    return cp.returncode, (cp.stdout or "").strip()


@dataclass(frozen=True, slots=True)
class GitCheckout:
    """The git working tree holding a directory: its top-level directory (an
    absolute path in this OS's form) and its branch. ``branch`` is the short
    branch name, a short commit id when HEAD is detached, or "" when git could
    not say (an unborn branch whose name could not be read).

    ``repo`` is the repository's main working tree: the same as ``root`` in an
    ordinary checkout, and the checkout the worktree was added from when ``root``
    is a linked worktree (``git worktree add``, an agent's isolated copy), so a
    session there is named and grouped by its repository."""

    root: str
    branch: str
    repo: str = ""


# Seconds one git query may take. Capture reads the checkout once per ingest,
# inside a hook the client waits on: a hung git (a network filesystem, a
# credential prompt) must cost a bounded delay, never a stalled session.
GIT_QUERY_TIMEOUT_S = 3.0


def _git(git: str, cwd: str, *args: str) -> str | None:
    """One read-only git query in ``cwd``; its stripped stdout, or None on any
    failure (non-zero exit, timeout, OS error). No shell; no console window."""
    import subprocess

    try:
        cp = subprocess.run(  # nosec B603 - argv list, no shell
            [git, "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=GIT_QUERY_TIMEOUT_S,
            check=False,
            stdin=subprocess.DEVNULL,
            **hidden_window_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if cp.returncode != 0:
        return None
    return (cp.stdout or "").strip()


def git_checkout(cwd: str) -> GitCheckout | None:
    """The git working tree that holds ``cwd``, or None when there is none:
    git is not installed (the capability: an executable on PATH, not the OS),
    ``cwd`` is not a directory, it is outside any repository, or git did not
    answer within GIT_QUERY_TIMEOUT_S per query. Never raises.

    Up to four queries: ``rev-parse --show-toplevel`` for the root,
    ``symbolic-ref`` for the branch, which also names an unborn branch (a new
    repository with no commit, where ``rev-parse --abbrev-ref HEAD`` fails), the
    short commit id when HEAD is detached, and ``rev-parse --git-common-dir`` for
    the repository (see GitCheckout.repo)."""
    import shutil

    if not cwd or not os.path.isdir(cwd):
        return None
    git = shutil.which("git")
    if git is None:
        return None
    top = _git(git, cwd, "rev-parse", "--show-toplevel")
    if not top:
        return None
    branch = _git(git, cwd, "symbolic-ref", "--short", "-q", "HEAD")
    if not branch:
        branch = _git(git, cwd, "rev-parse", "--short", "HEAD") or ""
    # git prints a forward-slash path on every OS; normpath gives this OS's form.
    root = os.path.normpath(top)
    return GitCheckout(root=root, branch=branch, repo=_main_tree(git, cwd) or root)


def _main_tree(git: str, cwd: str) -> str | None:
    """The main working tree of the repository holding ``cwd``: the parent of
    the shared git directory when that is a ``.git`` directory. None otherwise
    (a bare repository, a submodule's ``.git/modules/<name>``, git not
    answering), where the caller keeps the checkout's own root.

    ``--git-common-dir`` (git 2.5+) is relative to ``cwd`` unless absolute;
    ``--path-format=absolute`` would need git 2.31."""
    common = _git(git, cwd, "rev-parse", "--git-common-dir")
    if not common:
        return None
    common = os.path.normpath(os.path.join(cwd, common))
    if os.path.basename(common) != ".git":
        return None
    return os.path.dirname(common)


# System.Volume.BitLockerProtection, as Windows' Shell reports it to any user
# (manage-bde needs admin). Undocumented by Microsoft: only the two values
# relied on are mapped; anything else is reported, not guessed.
_BITLOCKER = {"1": (True, "BitLocker is on"), "2": (False, "BitLocker is off")}


def volume_encryption(path: str) -> VolumeEncryption | None:
    """Whether the disk holding ``path`` is encrypted, where this OS can be asked
    without admin: Windows (BitLocker, through the Shell) and macOS (FileVault,
    ``fdesetup isactive``). None where it is not checked (other OSes)."""
    if is_windows():
        drive = os.path.splitdrive(os.path.abspath(path))[0] or "C:"
        query = (
            f"(New-Object -ComObject Shell.Application).NameSpace('{drive}\\').Self"
            ".ExtendedProperty('System.Volume.BitLockerProtection')"
        )
        rc, out = _probe(["powershell", "-NoProfile", "-NonInteractive", "-Command", query])
        state, detail = _BITLOCKER.get(out, (None, f"BitLocker status not read (code {out or rc!r})"))
        return VolumeEncryption(state, f"{drive} {detail}", "Windows Credential Manager")
    if is_macos():
        rc, out = _probe(["fdesetup", "isactive"])
        state = {"true": True, "false": False}.get(out)
        if state is None:
            detail = f"FileVault status not read ({out or rc!r})"
        else:
            detail = "FileVault is on" if state else "FileVault is off"
        return VolumeEncryption(state, detail, "the macOS login keychain")
    return None


def synced_folder_roots() -> list[tuple[str, str]]:
    """``(service, directory)`` for each folder a sync client copies off this
    machine, where this OS puts them by default or says where they are. A file
    inside one is uploaded. Only directories that exist are returned.

    Windows: the OneDrive env vars, ``~/Dropbox``, ``~/iCloudDrive``. macOS:
    ``~/Library/Mobile Documents`` (iCloud Drive) and each provider under
    ``~/Library/CloudStorage``, plus ``~/Dropbox``. Elsewhere: ``~/Dropbox``,
    ``~/OneDrive``. Not detected: a sync client pointed at a custom folder.
    """
    home = os.path.expanduser("~")
    found: list[tuple[str, str]] = []
    if is_windows():
        # Literal names: the OneDrive client sets each (personal / work), and the
        # env checker allows them by name (bin/check_env_access.py OS_NAMES).
        found += [
            ("OneDrive", p)
            for p in (
                os.environ.get("OneDrive"),
                os.environ.get("OneDriveConsumer"),
                os.environ.get("OneDriveCommercial"),
            )
            if p
        ]
        found += [("Dropbox", os.path.join(home, "Dropbox")), ("iCloud Drive", os.path.join(home, "iCloudDrive"))]
    elif is_macos():
        found.append(("iCloud Drive", os.path.join(home, "Library", "Mobile Documents")))
        cloud = os.path.join(home, "Library", "CloudStorage")
        try:
            found += [(name.split("-")[0], os.path.join(cloud, name)) for name in sorted(os.listdir(cloud))]
        except OSError:
            pass
        found.append(("Dropbox", os.path.join(home, "Dropbox")))
    else:
        found += [("Dropbox", os.path.join(home, "Dropbox")), ("OneDrive", os.path.join(home, "OneDrive"))]
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for service, path in found:
        if path not in seen and os.path.isdir(path):
            seen.add(path)
            out.append((service, path))
    return out


# The persistent environment scopes: (label, hive attribute, subkey, writable
# without admin). User is what `setx` writes; Machine needs elevation.
PERSISTENT_ENV_SCOPES = (
    ("User", "HKEY_CURRENT_USER", "Environment", True),
    ("Machine", "HKEY_LOCAL_MACHINE", r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment", False),
)


def _env_store() -> Any | None:
    """The registry module when this OS keeps a persistent environment there.
    Both the store (measured) and the seam's answer (is_windows) must say so: a
    run simulating POSIX (conftest.simulate_os, the one knob) sees no store."""
    return _winreg if _winreg is not None and is_windows() else None


def persistent_env_read(names: list[str] | dict[str, str], unchecked: list[str] | None = None) -> list[dict]:
    """Each of ``names`` set in a persistent environment scope, as {scope, label,
    hive_name, subkey, writable, name, value, kind}; [] where the OS keeps no such
    store. Never raises: a scope it could not read is named in ``unchecked``."""
    reg = _env_store()
    if reg is None:
        return []
    skipped = unchecked if unchecked is not None else []
    hits: list[dict] = []
    for scope, hive_name, subkey, writable in PERSISTENT_ENV_SCOPES:
        try:
            with reg.OpenKey(getattr(reg, hive_name), subkey) as k:
                for name in names:
                    try:
                        value, kind = reg.QueryValueEx(k, name)
                    except FileNotFoundError:
                        continue  # not set at this scope
                    hits.append(
                        {
                            "scope": scope,
                            "label": f"Windows {scope} env ({hive_name}\\{subkey})",
                            "hive_name": hive_name,
                            "subkey": subkey,
                            "writable": writable,
                            "name": name,
                            "value": str(value),
                            "kind": kind,
                        }
                    )
        except FileNotFoundError:
            continue  # scope key absent
        except OSError as e:  # e.g. no read permission on HKLM
            skipped.append(f"Windows {scope} env ({hive_name}\\{subkey}): {type(e).__name__}: {e}")
    return hits


def user_env_rename(old: str, new: str) -> str:
    """Rename ``old`` to ``new`` in the persistent per-user environment, carrying
    the value and its registry type; when ``new`` is already set, ``old`` is
    dropped (``new`` wins). Returns "renamed" or "dropped"; raises ``OSError``
    (including where there is no such store). Running shells are asked to
    reload their environment afterwards."""
    reg = _env_store()
    if reg is None:
        raise OSError("this OS keeps no persistent per-user environment store")
    _scope, hive_name, subkey, _w = PERSISTENT_ENV_SCOPES[0]
    with reg.OpenKey(getattr(reg, hive_name), subkey, 0, reg.KEY_READ) as k:
        old_val, old_type = reg.QueryValueEx(k, old)
        try:
            reg.QueryValueEx(k, new)
            new_exists = True
        except FileNotFoundError:
            new_exists = False
    with reg.OpenKey(getattr(reg, hive_name), subkey, 0, reg.KEY_SET_VALUE) as k:
        if not new_exists:
            reg.SetValueEx(k, new, 0, old_type, old_val)
        reg.DeleteValue(k, old)
    broadcast_env_change()
    return "dropped" if new_exists else "renamed"


def broadcast_env_change() -> None:
    """Ask running processes to reload the environment after a persistent env
    write (WM_SETTINGCHANGE 'Environment'). Best-effort: the change is already
    stored; this only nudges live shells. Never raises."""
    if _env_store() is None:
        return
    try:
        import ctypes

        hwnd_broadcast, wm_settingchange, smto_abortifhung = 0xFFFF, 0x1A, 0x0002
        _ct: Any = ctypes  # Any: typeshed has ctypes.windll only for win32
        _ct.windll.user32.SendMessageTimeoutW(
            hwnd_broadcast, wm_settingchange, 0, "Environment", smto_abortifhung, 5000, ctypes.byref(ctypes.c_ulong())
        )
    except Exception:  # noqa: BLE001 -- a nudge, never a failure
        pass


def os_name() -> str:
    """Return 'Windows', 'Darwin', or 'Linux' without touching WMI.

    Drop-in for ``platform.system()`` in OS-branch comparisons. An OS outside the
    support contract (FreeBSD, ...) is answered 'Linux', the nearest POSIX family,
    as before, but no longer silently: it warns once.
    """
    if is_windows():
        return "Windows"
    if is_macos():
        return "Darwin"
    if not is_linux():
        global _UNSUPPORTED_WARNED
        if not _UNSUPPORTED_WARNED:
            _UNSUPPORTED_WARNED = True
            logging.getLogger("m3_memory.platform").warning(
                "unsupported OS; observed: sys.platform=%r, treated as Linux. m3 supports "
                "Windows, macOS and Linux. inspect: m3_memory/_platform.py",
                sys.platform,
            )
    return "Linux"


def is_windows() -> bool:
    return sys.platform == "win32"


def executable_name(base: str) -> str:
    """``base`` as this OS names an executable file (``.exe`` on Windows)."""
    return base + ".exe" if is_windows() else base


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def is_apple_silicon() -> bool:
    """macOS on arm64. platform.machine() is only consulted on Darwin, where it
    reads uname() directly (the WMI hang is Windows-only)."""
    if sys.platform != "darwin":
        return False
    import platform

    return platform.machine() == "arm64"


def hidden_window_kwargs() -> dict:
    """subprocess kwargs that hide the child's console window on Windows while
    KEEPING its stdout/stderr. No-op off Windows, so call sites stay portable.

        subprocess.run([...], **hidden_window_kwargs())

    Use this — not ``creationflags=CREATE_NO_WINDOW`` — for anything whose
    output matters. Measured 2026-09-13, emitting on both streams and exiting 7:

        python.exe + SW_HIDE, capture_output=True   -> stdout, stderr, rc intact
        python.exe + SW_HIDE, inherited handles     -> output still reaches the
                                                       parent's console
        CREATE_NO_WINDOW,     inherited handles     -> output LOST (see
                                                       bin/_task_runtime.py)

    That difference is why this exists as its own helper. ``no_window_kwargs``
    in ``bin/_task_runtime.py`` is the right tool for a *background* scheduled
    task that always captures; this is the right tool for a *foreground* step a
    user is watching. PR #154 nearly shipped the former into the installer,
    where a failing ``pip install`` would have printed "OS setup failed (code 1)"
    with the reason gone — a §3 silent failure.

    Why hiding is needed at all: a child spawned from a console-less parent gets
    its OWN console, which steals focus mid-keystroke. Five orphaned
    ``install_os.py`` consoles were found on one box 2026-09-13 (16 h old, 0.1 s
    CPU, every parent exited), each parked on a prompt behind a window nobody
    asked for.
    """
    import subprocess

    # The capability decides, never the OS name: STARTUPINFO exists only where a
    # console window does, so a simulated Windows on POSIX gets {} as well, and
    # tests/test_window_flags_capability.py provides it on POSIX and expects the
    # flags. (A sys.platform test added for mypy broke that contract on the
    # POSIX lanes, 2026-10-10.) The Any alias is the codebase's convention for
    # names typeshed defines only for win32 (installer._migrate_registry_env_names).
    if not hasattr(subprocess, "STARTUPINFO"):
        return {}
    sp: Any = subprocess
    si = sp.STARTUPINFO()
    si.dwFlags |= sp.STARTF_USESHOWWINDOW
    si.wShowWindow = sp.SW_HIDE
    return {"startupinfo": si}


def current_uid() -> int | None:
    """This process's real user id where the OS has POSIX uids, else None.

    The capability decides (``os.getuid`` exists only off Windows). Owner checks
    on user-only files compare against this; on Windows they use ACLs instead.
    """
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid is not None else None


def user_service_runtime_dir() -> str | None:
    """The runtime directory through which `systemctl --user` reaches this user's
    systemd manager, or None when no manager runs: a `su` shell from root has no
    login session, and without linger the user manager is not started.

    Linux only (None elsewhere). Found under XDG_RUNTIME_DIR, or /run/user/<uid>
    when a `su` shell dropped that variable. The manager answers on the D-Bus
    `bus` socket or, where the user D-Bus is not installed, on `systemd/private`
    (measured on a Proxmox host: no `bus`, systemctl --user worked).
    """
    if not is_linux():
        return None
    uid = current_uid()
    runtime = os.environ.get("XDG_RUNTIME_DIR") or (f"/run/user/{uid}" if uid is not None else "")
    if runtime and any(os.path.exists(os.path.join(runtime, *s)) for s in (("bus",), ("systemd", "private"))):
        return runtime
    return None


def os_fips_mode() -> bool | None:
    """Whether the operating system reports FIPS mode, or None where there is no
    such switch (macOS) or it cannot be read.

    Linux: ``/proc/sys/crypto/fips_enabled``. Windows: the FipsAlgorithmPolicy
    registry value. Informational only: it says what the OS enforces for its own
    crypto, not what a library bundling its own OpenSSL (Python's ssl on
    Windows, psycopg2-binary) does, so it can never prove validated TLS.
    """
    if is_linux():
        try:
            with open("/proc/sys/crypto/fips_enabled", encoding="ascii") as f:
                return f.read().strip() == "1"
        except OSError:
            return None
    if is_windows():
        if _winreg is None:
            return None
        try:
            key = r"SYSTEM\CurrentControlSet\Control\Lsa\FipsAlgorithmPolicy"
            with _winreg.OpenKey(_winreg.HKEY_LOCAL_MACHINE, key) as k:
                value, _ = _winreg.QueryValueEx(k, "Enabled")
            return bool(value)
        except OSError:
            return None
    return None


def dispatch_endpoint(engine_root: str) -> str:
    """Where this engine root's dispatch doorbell (m3-dispatchd) listens.

    One per engine root, so two stores (or a test's sandbox) never share a
    daemon. Windows: a named pipe, per user and per root. Elsewhere: a Unix
    socket in the root's ``.internal`` directory, or under the temp directory
    when that path would exceed the socket path limit (104 bytes on macOS).
    The name suffix is a non-security hash of the root (usedforsecurity=False:
    outside the FIPS boundary, as for content keys).
    """
    import hashlib

    root = os.path.abspath(engine_root)
    tag = hashlib.sha256(root.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
    if is_windows():
        import getpass

        return rf"\\.\pipe\m3-dispatch-{getpass.getuser()}-{tag}"
    import tempfile

    # The first that fits the limit. A long TMPDIR made the temp fallback itself
    # too long (125 bytes in a macOS pytest sandbox), and the daemon could not
    # bind; /tmp always fits. The socket is 0600 and peer-checked, so a shared
    # directory exposes nothing.
    name = f"m3-dispatch-{current_uid()}-{tag}.sock"
    candidates = (
        os.path.join(root, ".internal", "dispatch.sock"),
        os.path.join(tempfile.gettempdir(), name),
        os.path.join("/tmp", name),  # nosec B108 - a 0600 socket named per uid; the daemon checks the peer uid
    )
    return next(c for c in candidates if len(c.encode("utf-8")) < 100)


def port_listener_is_mine(port: int, procs: Any) -> bool | None:
    """Whether one of this user's processes listens on TCP ``port``.

    ``procs`` is an iterable of psutil-like processes (the caller owns psutil; this
    seam stays stdlib-only). True: a listener among this user's processes (or ones
    whose owner the OS hides, which may be ours). False: none of them listens.
    Reads only this user's own processes, so it needs no privilege.
    """
    for proc in procs:
        if owned_by_current_user(proc) is False:
            continue
        try:
            conns = (getattr(proc, "net_connections", None) or proc.connections)(kind="tcp")
        except Exception:  # noqa: BLE001 — a process vanishing or refusing is skipped
            continue
        for c in conns:
            if str(c.status).upper() == "LISTEN" and c.laddr and c.laddr.port == port:
                return True
    return False


def owned_by_current_user(proc: Any) -> bool | None:
    """Whether a psutil process belongs to the user running this code.

    True or False when the OS can say; None when it cannot (on Windows another
    session's, or an elevated, process may refuse its owner). A caller that must
    not miss a process of its own treats None as "maybe mine"; one that must not
    touch another user's treats it as "not known to be mine".
    """
    uid = current_uid()
    try:
        if uid is not None:
            return bool(proc.uids().real == uid)
        import getpass

        owner = proc.username() or ""
        return owner.rsplit("\\", 1)[-1].lower() == getpass.getuser().lower()
    except Exception:  # noqa: BLE001 — psutil AccessDenied / NoSuchProcess, or no owner API
        return None


def new_process_group_kwargs() -> dict:
    """Popen kwargs that start the child in its own process group, so a console
    Ctrl+C aimed at this process does not also reach it.

    Windows: ``CREATE_NEW_PROCESS_GROUP``. POSIX: ``{}`` (unchanged behaviour:
    the embed server's llama-server child has always shared the group there).
    The capability decides, as for the other window/process flags.
    """
    import subprocess

    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        sp: Any = subprocess
        return {"creationflags": sp.CREATE_NEW_PROCESS_GROUP}
    return {}


def is_elevated() -> bool:
    """True when this process already has admin rights: uid 0 where the OS has
    uids, else a Windows administrator token. The capability decides. Callers
    that self-elevate check it first: `m3 schedules add` asked "Run it elevated
    now?" from an ELEVATED shell, and in a hidden window waited for ever
    (2026-10-11)."""
    uid = current_uid()  # the seam's own uid reader
    if uid is not None:
        return uid == 0
    try:
        import ctypes

        windll: Any = getattr(ctypes, "windll", None)
        return bool(windll.shell32.IsUserAnAdmin()) if windll is not None else False
    except (AttributeError, OSError):
        return False


def killable_tree_kwargs() -> dict:
    """Popen kwargs so :func:`kill_tree` can end the child AND everything it
    started. POSIX: ``start_new_session`` (the child leads its own process group).
    Windows: ``{}`` (taskkill walks the tree by parent pid). The capability
    (``os.killpg``) decides, not the OS name."""
    return {"start_new_session": True} if hasattr(os, "killpg") else {}


def kill_tree(pid: int) -> None:
    """Kill ``pid`` and its descendants now (a child started with
    :func:`killable_tree_kwargs`). Killing only the child leaves grandchildren
    alive and holding its pipes, so a caller reading them waits on the orphans
    (measured 2026-10-11: a 5 s deadline returned after 64 s)."""
    if hasattr(os, "killpg"):
        import signal

        try:
            os.killpg(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except ProcessLookupError:
            pass
        return
    import subprocess

    subprocess.run(  # nosec B603 B607 - fixed argv, the OS's own tool
        ["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, check=False, **hidden_window_kwargs()
    )


def detached_spawn_kwargs() -> dict:
    """Popen kwargs for a BACKGROUND child that outlives this process and never
    shows a console window.

        subprocess.Popen([...], stdout=DEVNULL, stderr=DEVNULL, **detached_spawn_kwargs())

    Windows: ``CREATE_NO_WINDOW | DETACHED_PROCESS``. DETACHED_PROCESS alone
    still lets the OS give a console-subsystem python.exe its own window; a
    stray one sat on a desktop for as long as the embed server ran, and closing
    it killed the embedder (2026-08-10). POSIX: ``start_new_session``, which
    detaches from the controlling terminal. Output is not kept: for a child
    whose output matters, see :func:`hidden_window_kwargs`.

    The dashboard daemon and doctor's embed-server fix each carried this pair.
    """
    import subprocess

    if hasattr(subprocess, "CREATE_NO_WINDOW"):  # the capability, as above
        sp: Any = subprocess
        return {"creationflags": sp.CREATE_NO_WINDOW | sp.DETACHED_PROCESS}
    return {"start_new_session": True}


def reexec(argv: list[str], env: dict[str, str] | None = None) -> NoReturn:
    """Run ``argv`` in place of this process; its exit code becomes ours.

    POSIX: ``os.execv``, a true exec (same pid). Windows has no exec: ``os.exec*``
    there starts a new process and ends this one at once, and measured on
    2026-10-11 that loses three things: the caller sees exit code 0 after 0.1 s
    (the child exited 7), an argument containing a space is split, and a console
    child of a windowless parent (a scheduled task) gets its own visible window.
    So on Windows the child runs to completion, hidden, with ``argv`` passed as a
    list, and we exit with its code. Output handles are inherited either way.
    ``env`` (built by m3_memory._childenv) replaces the environment; None keeps it.
    """
    if is_windows():
        import subprocess

        raise SystemExit(subprocess.call(argv, env=env, **hidden_window_kwargs()))  # nosec B603
    if env is None:
        os.execv(argv[0], argv)  # nosec B606
    os.execve(argv[0], argv, env)  # nosec B606


def python_exe(windowless: bool = False) -> str:
    """Return a real Python interpreter path, never a console-script shim.

    Use this instead of ``sys.executable`` whenever spawning a child process,
    because ``sys.executable`` is NOT always an interpreter. Under a pip/pipx
    console script (``m3.exe``, ``mcp-memory.exe``) it is the SHIM, so

        subprocess.run([sys.executable, "script.py", "--flag"])

    re-enters the m3 CLI with a .py path as its subcommand. argparse rejects it
    ("invalid choice: 'script.py'"), the child exits 2, and — because the shim
    is the m3 CLI — it can also spawn stray ``m3.exe`` processes that outlive
    the run.

    That is not hypothetical: it silently truncated `m3 setup`. The dashboard
    boot-task step ran ``[sys.executable, install_schedules.py, ...]``, which
    under ``m3 setup`` failed, aborted the wizard before Step 5, and exited 1 —
    while ``python -m m3_memory.cli setup`` completed and exited 0. Same code,
    same venv, different launcher.

    Resolution order:
      1. ``sys.executable`` when it really is an interpreter (the common case:
         ``python -m ...``, and every ``bin/*.py`` script).
      2. The interpreter beside the shim — a console script always lives in the
         environment's ``Scripts``/``bin`` dir next to ``python``.
      3. ``sys.executable`` unchanged, so callers still get the old behaviour
         rather than an exception if neither check resolves.

    ``windowless=True`` (Windows only) prefers ``pythonw.exe`` in step 2, for a
    BACKGROUND spawn where a console flash is pure noise. It is opt-in, NOT the
    default.

    CORRECTED 2026-09-13 — the two claims this docstring used to make for that
    default were both wrong, and neither is the real reason:

    * "pythonw.exe has no stdout" is imprecise; it has none only when no handle
      is attached. Measured with a child printing to both streams and exiting 7:
      ``pythonw.exe`` + ``capture_output=True`` returned stdout, stderr and rc
      identically to ``python.exe``. (Consistent with the #153 note below.)
    * "every caller today is a foreground step whose output the user is reading"
      is false: of the 11 ``python_exe()`` spawns in installer.py and
      setup_wizard.py, 9 capture output and only 2 inherit.

    The default stands for a different reason: **this flag does not control the
    window.** It only reorders which binary is found beside a console-script
    shim, so when ``sys.executable`` is already a real interpreter — the common
    case — passing ``windowless=True`` changes NOTHING. Hiding the window is a
    property of the SPAWN, not the launcher: use ``hidden_window_kwargs()``
    above, which works regardless of which interpreter is resolved and keeps the
    pipes. Reach for ``windowless=True`` only when you genuinely want the
    GUI-subsystem binary (a detached background process that must never own a
    console), not as a flash fix.

    Not needed for stdio MCP servers -- `generate_configs._windowless` already
    resolves those (and the capture hooks and statusline) to pythonw.exe at
    registration time. NB the earlier claim here, that pythonw.exe would kill a
    stdio transport, was WRONG: it has no stdout only when no pipe is attached.
    Measured 2026-09-13 -- with pipes it serves MCP identically (#153).
    """
    exe = sys.executable or ""
    stem = os.path.splitext(os.path.basename(exe))[0].lower()
    # "python", "python3", "python3.14", "pythonw" -> a real interpreter.
    if stem.startswith("python"):
        return exe
    if exe:
        here = os.path.dirname(exe)
        if is_windows():
            # python.exe FIRST by default: every caller today is an installer or
            # wizard step that runs in the FOREGROUND and shows the child's
            # output to the user. pythonw.exe has no stdout, so defaulting to it
            # would silently swallow install diagnostics -- a worse bug than the
            # cosmetic flash, and a silent one.
            #
            # windowless=True flips the order for a genuinely BACKGROUND spawn,
            # where no one is reading stdout and a console flash is pure noise.
            names = ("pythonw.exe", "python.exe") if windowless else ("python.exe", "pythonw.exe")
        else:
            names = ("python3", "python")
        for name in names:
            cand = os.path.join(here, name)
            if os.path.isfile(cand):
                return cand
    return exe


# ── Console keys (the live status screen) ────────────────────────────────────
# Windows reads the console through msvcrt; POSIX puts the tty in raw mode and
# selects on stdin. The capability decides (msvcrt imports only where it
# exists), not the OS name, and failures are logged at DEBUG: an interactive
# key read that fails falls back to waiting, exactly as before.
def _msvcrt() -> Any | None:
    try:
        import msvcrt
    except ImportError:
        return None
    return msvcrt  # Any: typeshed defines msvcrt's API only for win32


def read_key(timeout: float) -> str | None:
    """A keypress within ``timeout`` seconds, lower-cased, or None."""
    import time

    start_time = time.time()
    ms = _msvcrt()
    if ms is not None:
        while time.time() - start_time < timeout:
            if ms.kbhit():
                try:
                    ch = ms.getch()
                    val = (ch.decode("utf-8", errors="ignore") if isinstance(ch, bytes) else str(ch)).lower()
                    if val:
                        return val
                except (OSError, UnicodeError) as e:  # one unreadable key; keep waiting
                    logging.getLogger("m3_memory.platform").debug("read_key: getch failed: %s", e)
            time.sleep(0.05)
        return None

    fd = _stdin_tty_fd()
    if fd is None:
        time.sleep(timeout)
        return None
    import select
    import termios as _termios
    import tty as _tty

    termios: Any = _termios  # Any: typeshed defines these only off win32
    tty: Any = _tty
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while time.time() - start_time < timeout:
            rem = timeout - (time.time() - start_time)
            rlist, _, _ = select.select([sys.stdin], [], [], min(0.05, max(0.01, rem)))
            if rlist:
                return sys.stdin.read(1).lower()
    except (OSError, ValueError, termios.error) as e:  # no raw tty: behave as a timeout
        logging.getLogger("m3_memory.platform").debug("read_key: raw read failed: %s", e)
        time.sleep(timeout)
        return None
    finally:
        _restore_tty(fd, old_settings)
    return None


def wait_for_any_key() -> None:
    """Block until any key is pressed (a short sleep when stdin is not a tty)."""
    import time

    ms = _msvcrt()
    if ms is not None:
        while ms.kbhit():
            ms.getch()
        while not ms.kbhit():
            time.sleep(0.05)
        ms.getch()
        return

    fd = _stdin_tty_fd()
    if fd is None:
        time.sleep(2.0)
        return
    import select
    import termios as _termios
    import tty as _tty

    termios: Any = _termios  # Any: typeshed defines these only off win32
    tty: Any = _tty
    termios.tcflush(sys.stdin, termios.TCIFLUSH)
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        select.select([sys.stdin], [], [])
        sys.stdin.read(1)
    except (OSError, ValueError, termios.error) as e:  # no raw tty: a short pause instead
        logging.getLogger("m3_memory.platform").debug("wait_for_any_key: raw read failed: %s", e)
        time.sleep(2.0)
    finally:
        _restore_tty(fd, old_settings)


def console_has_person() -> bool:
    """True when a person is at this console.

    On Windows, isatty() is not enough: the NUL device reports itself as a
    character device, so `< NUL` (how scripts and schedulers detach stdin) reads
    as a terminal. GetConsoleMode succeeds only for a real console input handle.
    bin/m3_upgrade.py keeps a copy (interactive_console) because it cannot
    import this package; a test pins the two together. An OS fact, so it lives
    in this seam; wizard.ui re-exports it.
    """
    try:
        if sys.stdin is None or not sys.stdin.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    if not is_windows():
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined, unused-ignore]
    handle = kernel32.GetStdHandle(wintypes.DWORD(-10 & 0xFFFFFFFF))  # STD_INPUT_HANDLE
    mode = wintypes.DWORD()
    return bool(kernel32.GetConsoleMode(handle, ctypes.byref(mode)))


def _stdin_tty_fd() -> int | None:
    """stdin's descriptor when it is a terminal; None when it is not one, or
    has no descriptor at all (a pipe, a captured or redirected stream)."""
    try:
        fd = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):  # io.UnsupportedOperation is both
        return None
    return fd if os.isatty(fd) else None


def _restore_tty(fd: int, settings: Any) -> None:
    import termios as _termios

    termios: Any = _termios  # Any: typeshed defines these only off win32
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, settings)
    except (OSError, ValueError, termios.error) as e:  # the terminal may already be gone
        logging.getLogger("m3_memory.platform").debug("tty restore failed: %s", e)
