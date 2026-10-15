"""Touch ID for sudo on macOS: detect, and enable with the user's consent.

macOS 14+ reads /etc/pam.d/sudo_local (which updates leave alone); one line,
`auth sufficient pam_tid.so`, lets sudo accept a fingerprint instead of the
password. This is a system security setting, so m3 only ever ADDS that line,
only when asked, and checks the file afterwards. A fingerprint cannot reach an
SSH session, so the offer is made only to someone at the Mac.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 - fixed argv lists, no shell

from m3_memory._platform import is_macos

SUDO_LOCAL = "/etc/pam.d/sudo_local"
TEMPLATE = "/etc/pam.d/sudo_local.template"
PAM_TID = "/usr/lib/pam/pam_tid.so.2"
LINE = "auth       sufficient     pam_tid.so"


def _is_mac() -> bool:
    return is_macos()


def _active_tid_line(text: str) -> bool:
    return any(
        ln.split()[:3] == ["auth", "sufficient", "pam_tid.so"]
        for ln in text.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    )


def _sensor_present() -> bool | None:
    """True/False from `bioutil -r -s`; None when it cannot be asked."""
    try:
        out = subprocess.run(["bioutil", "-r", "-s"], capture_output=True, text=True, timeout=10).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return None
    for ln in out.splitlines():
        if "Biometrics functionality" in ln:
            return ln.strip().endswith("1")
    return None


def status() -> str:
    """enabled | available | no-sensor | unsupported."""
    if not _is_mac() or not os.path.exists(PAM_TID):
        return "unsupported"
    try:
        with open(SUDO_LOCAL, encoding="utf-8") as fh:
            if _active_tid_line(fh.read()):
                return "enabled"
    except FileNotFoundError:
        if not os.path.exists(TEMPLATE):
            return "unsupported"  # macOS < 14: sudo_local is not read
    except OSError:
        pass
    return "no-sensor" if _sensor_present() is False else "available"


def at_the_mac() -> bool:
    """A fingerprint (and the sudo prompt) need a person at this Mac, not SSH."""
    return not (os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"))


def enable() -> tuple[bool, str]:
    """Append the pam_tid line to sudo_local via sudo (asks for the password
    once). Returns (enabled, detail); success is read back from the file."""
    try:
        r = subprocess.run(
            ["sudo", "tee", "-a", SUDO_LOCAL], input=LINE + "\n", capture_output=True, text=True, timeout=120
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"sudo could not run ({e})"
    if r.returncode != 0:
        return False, (r.stderr or "sudo declined").strip()
    if status() != "enabled":
        return False, f"{SUDO_LOCAL} was written but holds no active pam_tid line"
    return True, f"added `{LINE.split()[0]} sufficient pam_tid.so` to {SUDO_LOCAL}"


UNDO = f"sudo sed -i '' '/pam_tid.so/d' {SUDO_LOCAL}"
TEST = "sudo -k; sudo true"
DECLINED = ".touchid_sudo_declined"  # in the config root: a "no" to the offer


def _declined_marker() -> str:
    from m3_memory import _bin

    _bin.ensure_on_path()
    from m3_core.paths import get_m3_config_root

    return os.path.join(get_m3_config_root(), DECLINED)


def offer(ask, say) -> None:
    """Ask once whether sudo may accept Touch ID, and enable it on a yes.

    The one offer, shared by `m3 setup`, `m3 touchid offer` and install.sh.
    Silent unless it can help: only at the Mac, only when Touch ID is available
    and not already on, and never again after a no. ``ask(question) -> bool``
    and ``say(text)`` are the caller's prompt and output."""
    if not at_the_mac() or os.path.exists(_declined_marker()) or status() != "available":
        return
    if not ask(
        f"  Let sudo accept Touch ID instead of your password? (adds one line to {SUDO_LOCAL}; "
        "asks for your password one last time)"
    ):
        try:
            os.makedirs(os.path.dirname(_declined_marker()), exist_ok=True)
            open(_declined_marker(), "w", encoding="utf-8").close()
        except OSError:
            pass
        say("  Touch ID for sudo not enabled; m3 will not ask again. Later: m3 touchid enable")
        return
    ok, detail = enable()
    say(
        f"  Touch ID for sudo: {detail}. test: {TEST}  undo: {UNDO}"
        if ok
        else f"  Touch ID for sudo not enabled: {detail}"
    )


_STATUS_TEXT = {
    "enabled": "on: sudo accepts Touch ID (not over SSH, and not inside tmux or screen)",
    "available": "off: this Mac has Touch ID, but sudo asks for your password. turn on: m3 touchid enable",
    "no-sensor": "not available: this Mac has no Touch ID sensor (or none is enrolled)",
    "unsupported": "not available: needs macOS 14 or newer",
}


def _ask_tty(question: str) -> bool:
    try:
        ans = input(question + " [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in ("", "y", "yes")


def main(sub: str | None) -> int:
    """`m3 touchid [status|enable|offer]`."""
    state = status()
    if not _is_mac():
        print("Touch ID for sudo is a macOS feature; nothing to do on this system.")
        return 0 if sub in (None, "status") else 1
    if sub in (None, "status"):
        print(f"Touch ID for sudo: {_STATUS_TEXT[state]}")
        return 0
    if sub == "offer":
        offer(_ask_tty, print)
        return 0
    # enable: asked for by name, so an earlier "no" to the offer does not apply.
    if state == "enabled":
        print(f"Touch ID for sudo is already on. test: {TEST}")
        return 0
    if state != "available":
        print(f"Touch ID for sudo: {_STATUS_TEXT[state]}")
        return 1
    if not at_the_mac():
        print(
            "This session is over SSH: sudo will ask for your password here, and Touch ID\n"
            "works only in a terminal at the Mac. Run `m3 touchid enable` there to try it at once."
        )
    ok, detail = enable()
    if not ok:
        print(f"Touch ID for sudo not enabled: {detail}")
        return 1
    try:
        os.remove(_declined_marker())
    except OSError:
        pass
    print(f"Touch ID for sudo: {detail}.\n  test: {TEST}\n  undo: {UNDO}")
    return 0
