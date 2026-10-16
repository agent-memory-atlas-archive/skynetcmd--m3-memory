"""Rust-side `m3-embed-server doctor` subprocess wrapper.

Invokes the Rust binary's own diagnostic subcommand (B1 in m3-core-rs).
Best-effort: silently skips when the binary isn't on PATH (an install
without `m3 embedder install` is a legitimate state). Bounded timeout
prevents this phase from hanging.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import TYPE_CHECKING

from m3_core.seams import is_windows

if TYPE_CHECKING:
    from m3_core.tier2_client import Diagnosis

logger = logging.getLogger("memory.doctor.embed_server_probe")

BINARY_NAME = "m3-embed-server.exe" if is_windows() else "m3-embed-server"


def _health_port() -> int:
    """The configured embedder port (embed.server.port), as shared_embedder_probe
    reads it: a fixed 8082 probed another account's server after this one moved."""
    from m3_memory.embedder_admin import _embed_server_port

    return _embed_server_port()


TIMEOUT_SECS = 30


def _resolve_binary() -> str | None:
    """Locate the Rust embed-server binary, PATH or not.

    `shutil.which` alone is the #167 defect. The binary normally lives inside
    the m3_core_rs wheel (site-packages/m3_core_rs/m3-embed-server[.exe]) and is
    NOT on PATH, so a bare which() returns None while the service is installed
    and serving. Measured 2026-09-12 on two hosts: which() -> None,
    embedder_admin._server_binary() -> the real path, :8082/health -> 200.

    That resolver is the one owner (this install's wheel before PATH): asking
    PATH first ran an older install's binary left in ~/.local/bin, which failed
    `status` and reported a working embedder as FAILED (measured 2026-10-15).
    """
    try:
        from m3_memory import embedder_admin

        found = embedder_admin._server_binary()
        # _server_binary returns a Path; subprocess and the callers here want a
        # str. Normalising at the boundary keeps the rest of this module typed.
        return str(found) if found else None
    except Exception:  # noqa: BLE001 — a probe must never raise
        return shutil.which(BINARY_NAME)


def _port_answers(timeout: float = 2.0) -> Diagnosis:
    """What answers on this machine's embedder port right now, classified.

    The distinction that matters for the verdict: a binary we cannot find but a
    port that answers means the service IS running, however it was started.
    Reporting "not installed" there is the false alarm §3 names -- and it is
    exactly what happened, in the same doctor run whose embedding-cascade line
    said "healthy -- shared tier-2 embedder online". Asked through tier2_client
    (token + TLS trust), so a token-protected server is not read as absent.
    """
    from m3_core import tier2_client

    return tier2_client.health(f"http://127.0.0.1:{_health_port()}", timeout)[0]


def _server_needed() -> bool | None:
    """Whether m3 processes embed through this server: shared mode, or no
    .embed_config.json with embed.inproc off (embed.py then defers to it too).
    None when that cannot be read; it selects message wording only."""
    try:
        from m3_memory import embedder_admin as ea

        if ea.shared_mode_enabled():
            return True
        return not (os.path.exists(ea._embed_config_path()) or ea._config.setting("embed.inproc"))
    except Exception:  # noqa: BLE001 — only selects message wording
        return None


def _held_by_someone_else() -> bool | str:
    from m3_memory.embedder_admin import embed_port_owner_check

    return embed_port_owner_check()


def _note_older_copy_on_path(exe: str | None) -> None:
    """Name an m3-embed-server on PATH that is not this install's. Older install
    guides copied one into ~/.local/bin; m3 no longer runs it, but typing the
    command (or an old crontab line) runs that copy. Informational: nothing breaks."""
    on_path = shutil.which(BINARY_NAME)
    if not on_path or not exe:
        return
    try:
        if os.path.samefile(on_path, exe):
            return
    except OSError:
        return
    print(f"embed-server: an older copy at {on_path} is on PATH (m3 uses {exe}); safe to remove: rm {on_path}")


def run(brief: bool = False) -> int:
    """Invoke `m3-embed-server doctor` and inherit its exit code.

    Returns 0 when the binary isn't installed (not a Python-side failure).
    Returns 1 on subprocess timeout or any unhandled exception. brief=True
    captures the subprocess output and prints only a one-line verdict.
    """
    exe = _resolve_binary()
    _note_older_copy_on_path(exe)

    # ⚠ CHECK THE EXEC BIT BEFORE ANYTHING ELSE. The macos-metal wheel has
    # shipped m3-embed-server mode 0644, and that single EACCES surfaces
    # downstream as a broken cascade, an embed-server error, two
    # shared-embedder issues and an empty :8082 — four symptoms, none of which
    # names the cause. m3 repairs it before exec, but a silent repair hides a
    # packaging bug, and a read-only install cannot be repaired at all. Say so.
    try:
        from m3_memory.embedder_admin import exec_bit_status

        _bit = exec_bit_status(exe)
        if _bit.get("state") == "not-executable":
            print(f"⚠️  embed-server binary is not executable (mode {_bit.get('mode')}) — {_bit.get('detail')}")
    except Exception:  # noqa: BLE001 — a diagnostic must never break the probe
        pass

    if not exe:
        # Three states, not two. "Cannot find the binary" and "the service is
        # absent" are different claims, and conflating them reported a live
        # server as missing (#167).
        port_diag = _port_answers()
        needed = _server_needed()
        if port_diag.serving:
            if brief:
                print(f"embed-server: serving on :{_health_port()} (binary not located)")
            else:
                logger.debug(
                    f"{BINARY_NAME} not located, but :{_health_port()} answers — "
                    f"the service is running; skipping the Rust-side doctor"
                )
        elif port_diag.answering:
            # Something answers but does not serve (401, TLS, unhealthy): a
            # missing binary is not the story; the shared-embedder line says more.
            print(
                f"⚠️  embed-server: binary not located; :{_health_port()} answers but is not serving: {port_diag.message()}"
            )
        elif needed is False:
            if brief:
                print("embed-server: not installed (optional)")
            else:
                logger.debug(f"{BINARY_NAME} not located; skipping Rust-side doctor")
        else:
            # Shared mode (the default) needs a server on this port; "optional"
            # would invite dismissing the one line that matters.
            role = "m3 embeds through a server there" if needed else "whether m3 needs one could not be read"
            print(
                f"⚠️  embed-server: binary not located and nothing serves on :{_health_port()} "
                f"({port_diag.detail}); {role}. fix: `m3 embedder install-gpu` then `m3 embedder install`"
            )
            return 1
        return 0
    try:
        import datetime

        from m3_memory.embedder_admin import restart_embed_service_hint, stale_embed_servers

        for s in stale_embed_servers():
            fmt = "%Y-%m-%d %H:%M"
            print(
                f"⚠️  embed-server: pid {s['pid']} started "
                f"{datetime.datetime.fromtimestamp(s['started']).strftime(fmt)}, before the "
                f"native core was installed "
                f"({datetime.datetime.fromtimestamp(s['installed']).strftime(fmt)}); it still "
                f"runs the previous binary. fix: {restart_embed_service_hint()}"
            )
    except Exception:  # noqa: BLE001 — a diagnostic must never break the probe
        pass
    if not brief:
        print()
        print("=== Rust-side service health (m3-embed-server doctor) ===")
    # Quiet the llama.cpp/GGML backend's own stderr chatter — model-load notices
    # ("vocab missing newline token, using special_pad_id instead") and Metal
    # teardown lines ("ggml_metal_free", "llama_context ...") are INFO-level and
    # harmless for an embedding model, but with inherited stderr they interleave
    # with this probe's stdout and scroll the readable summary off-screen.
    # GGML_LOG_LEVEL=4 (error-only) suppresses them while still surfacing real
    # errors. Respect an operator-set value so power users can opt back in.
    from m3_core.seams import childenv

    env = childenv.child_env()
    env.setdefault("GGML_LOG_LEVEL", "4")  # an operator-set value is inherited and kept
    try:
        # In brief mode, capture (and discard) the subprocess's own noisy output;
        # we only report pass/fail from its exit code as a single line.
        # `doctor` does not exist on every build of this binary. Measured
        # 2026-09-12 against m3_core_rs 3.9.7: `m3-embed-server doctor` prints
        # USAGE and exits 2, while `status` works and reports
        # running/stopped/not installed. Treating that rc 2 as a health FAILURE
        # reported a fine install as broken -- the same false alarm #167 is
        # about, one layer down. Probe the subcommand instead of assuming it.
        sub = "doctor"
        probe = subprocess.run(
            [exe, "doctor"],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECS,
            env=env,
        )
        if probe.returncode != 0 and "USAGE" in (probe.stdout or "") + (probe.stderr or ""):
            sub = "status"
            # Captured in both modes: an uncaptured child writes straight to the
            # terminal, ahead of this process's buffered section header.
            r = subprocess.run(
                [exe, "status"],
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECS,
                env=env,
            )
            if not brief:
                print(f"  status   : {(r.stdout or r.stderr or '').strip() or '(no output)'}")
        else:
            r = probe
            if not brief:
                # The probe already consumed the output; re-emit it.
                print(r.stdout or "", end="")

        if brief:
            if r.returncode == 0:
                state = (r.stdout or "").strip().splitlines()[-1:] or [""]
                detail = f" ({state[0]})" if sub == "status" and state[0] else ""
                # ⚠ "ok (not installed)" IS A MISLABEL. `status` exits 0 while
                # REPORTING that no service exists, so the rc alone says the
                # query worked, not that the service is healthy. Printing a
                # green tick beside "not installed" reads as fine when
                # shared-mode config requires something on :8082 — and it is
                # precisely the reassurance that masked a killed embedder
                # (macOS, 2026-09-20). Report the state, not the exit code.
                # Another account on the port comes first: installing or starting
                # this account's server cannot work until it has a free port.
                whose = _held_by_someone_else()
                if whose is True:
                    print(
                        "⚠️  embed-server: the port answers, but not from this account's server "
                        "(another user's m3 or another program holds it); this account's own "
                        "embedder is not running. fix: m3 config set embed.server.port <free port>"
                    )
                elif "not installed" in (r.stdout or "").lower():
                    print(
                        "⚠️  embed-server: not installed — shared mode expects "
                        "a service on this port. fix: m3 embedder install"
                    )
                elif sub == "status" and state[0].strip().lower() == "stopped":
                    print("⚠️  embed-server: registered but stopped. fix: m3 embedder start")
                elif isinstance(whose, str):
                    print(f"embed-server: ok{detail}; could not check who holds the port ({whose})")
                else:
                    print(f"✅ embed-server: ok{detail}")
            else:
                said = ((r.stderr or "").strip() or (r.stdout or "").strip()).splitlines()
                first = f": {said[-1][:200]}" if said else " (no output)"
                print(f"❌ embed-server: `{os.path.basename(exe)} {sub}` exited {r.returncode}{first}")
        return r.returncode
    except subprocess.TimeoutExpired:
        print("embed-server: ❌ timed out" if brief else f"  m3-embed-server doctor timed out after {TIMEOUT_SECS}s")
        return 1
    except Exception as e:
        print("embed-server: ❌ error" if brief else f"  m3-embed-server doctor failed: {type(e).__name__}: {e}")
        return 1
