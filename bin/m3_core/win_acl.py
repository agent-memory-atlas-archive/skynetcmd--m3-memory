"""win_acl: who a Windows file's DACL lets in. THE owner of that question.

ctypes against advapi32, not pywin32: pywin32 was removed as unused
(requirements.txt, 2026-09-17), and this needs four calls. The DACL is read as
SDDL, which is locale-independent (icacls output is not).

"Private" means every ALLOW ace that applies to the object names the current
user, SYSTEM, Administrators or OWNER RIGHTS -- the ACL a file under the user
profile inherits (measured on SkyPC, 2026-10-08). A local administrator can
read any file regardless, so admitting them adds no exposure.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import re
import tempfile
from collections.abc import Iterator

from m3_core.seams import is_windows

_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x1
_DACL_SECURITY_INFORMATION = 0x4
_TOKEN_QUERY = 0x8
_TOKEN_USER = 1
_SDDL_REVISION_1 = 1

# SDDL aliases (and their full SIDs) that count as private.
_TRUSTED = {"SY", "BA", "OW", "S-1-5-18", "S-1-5-32-544", "S-1-3-4"}
_ACE = re.compile(r"\(([^;()]*);([^;()]*);[^;()]*;[^;()]*;[^;()]*;([^;()]*)\)")


class AclError(OSError):
    """The DACL could not be read: the caller cannot claim the file is private."""


def _last_error() -> int:
    """The calling thread's last Win32 error. Reached through getattr because
    typeshed defines ctypes.get_last_error only for win32, and the seam call
    guarding these bodies cannot narrow the platform for mypy."""
    return int(getattr(ctypes, "get_last_error")())


def _api():
    """advapi32 + kernel32 with every signature declared: ctypes defaults
    arguments to a C int, which truncates a 64-bit HANDLE or pointer."""
    if not is_windows():
        raise AclError("win_acl is Windows-only")
    from ctypes import wintypes  # lazy: the pure parser below is imported on every OS

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    P, PP = ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
    D, PD = wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
    PW = ctypes.POINTER(wintypes.LPWSTR)
    k32.GetCurrentProcess.argtypes, k32.GetCurrentProcess.restype = [], wintypes.HANDLE
    k32.CloseHandle.argtypes, k32.CloseHandle.restype = [wintypes.HANDLE], wintypes.BOOL
    k32.LocalFree.argtypes, k32.LocalFree.restype = [P], P
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, D, ctypes.POINTER(wintypes.HANDLE)]
    adv.OpenProcessToken.restype = wintypes.BOOL
    adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, P, D, PD]
    adv.GetTokenInformation.restype = wintypes.BOOL
    adv.ConvertSidToStringSidW.argtypes, adv.ConvertSidToStringSidW.restype = [P, PW], wintypes.BOOL
    adv.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, D, PP, PP, PP, PP, PP]
    adv.GetNamedSecurityInfoW.restype = D
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [P, D, D, PW, PD]
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    return adv, k32


def current_user_sid() -> str:
    if not is_windows():
        raise AclError("win_acl is Windows-only")
    from ctypes import wintypes

    adv, k32 = _api()
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    token = wintypes.HANDLE()
    if not adv.OpenProcessToken(k32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise AclError(f"OpenProcessToken failed (winerror {_last_error()})")
    try:
        size = wintypes.DWORD()
        adv.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token, _TOKEN_USER, buf, size, ctypes.byref(size)):
            raise AclError(f"GetTokenInformation failed (winerror {_last_error()})")
        psid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]  # TOKEN_USER.User.Sid
        out = wintypes.LPWSTR()
        if not adv.ConvertSidToStringSidW(ctypes.c_void_p(psid), ctypes.byref(out)):
            raise AclError(f"ConvertSidToStringSidW failed (winerror {_last_error()})")
        try:
            return str(out.value)
        finally:
            k32.LocalFree(out)
    finally:
        k32.CloseHandle(token)


def file_sddl(path: str) -> str:
    """The owner and DACL of ``path`` as an SDDL string."""
    if not is_windows():
        raise AclError("win_acl is Windows-only")
    from ctypes import wintypes

    adv, k32 = _api()
    info = _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION
    psd = ctypes.c_void_p()
    rc = adv.GetNamedSecurityInfoW(
        wintypes.LPCWSTR(str(path)), _SE_FILE_OBJECT, info, None, None, None, None, ctypes.byref(psd)
    )
    if rc != 0:
        raise AclError(f"GetNamedSecurityInfoW({path}) failed (winerror {rc})")
    try:
        out = wintypes.LPWSTR()
        if not adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            psd, _SDDL_REVISION_1, info, ctypes.byref(out), None
        ):
            raise AclError(f"ConvertSecurityDescriptorToStringSecurityDescriptorW failed (winerror {_last_error()})")
        try:
            return str(out.value)
        finally:
            k32.LocalFree(out)
    finally:
        k32.LocalFree(psd)


def foreign_principals(sddl: str, user_sid: str) -> list[str]:
    """SIDs an ALLOW ace grants this object to beyond the trusted set. A missing
    DACL grants everyone, reported as "Everyone (no DACL)". Pure, for tests."""
    dacl = sddl.split("D:", 1)[1] if "D:" in sddl else ""
    dacl = re.split(r"S:", dacl, maxsplit=1)[0]
    if not dacl or dacl.startswith("NO_ACCESS_CONTROL"):
        return ["Everyone (no DACL)"]
    foreign = []
    for ace_type, flags, sid in _ACE.findall(dacl):
        if not ace_type.startswith("A") or "IO" in flags:  # deny, or inherit-only
            continue
        named = f"{sid} ({_NAMES[sid]})" if sid in _NAMES else sid
        if sid not in _TRUSTED and sid != user_sid and named not in foreign:
            foreign.append(named)
    return foreign


# Readable names for the principals a too-broad ACL usually names.
_NAMES = {
    "WD": "Everyone",
    "S-1-1-0": "Everyone",
    "AU": "Authenticated Users",
    "BU": "Users",
    "S-1-5-32-545": "Users",
    "IU": "Interactive users",
    "SU": "Service logons",
    "S-1-5-3": "Batch logons",
    "AN": "Anonymous",
}


@contextlib.contextmanager
def probe_new_file(directory: str | os.PathLike[str]) -> Iterator[str]:
    """An EMPTY file in ``directory``, removed on exit even if the caller raises.
    A new file takes its directory's inheritable ACL, so this is how a writer
    learns what its file WOULD inherit before any content is written.
    (3.12 ``delete_on_close=False``: closed before the ACL is read.)"""
    with tempfile.NamedTemporaryFile(
        dir=directory, prefix=".m3_acl_probe.", delete=True, delete_on_close=False
    ) as probe:
        probe.close()
        yield probe.name


def file_foreign_principals(path: str) -> list[str]:
    """Who besides the trusted set can open ``path``; [] means private."""
    return foreign_principals(file_sddl(path), current_user_sid())


def icacls_user_only(path: str | os.PathLike[str], *, directory: bool = False) -> str:
    """THE command m3 tells a user to run to make ``path`` user-only. The user is
    spelled out: ``%USERNAME%`` expands in cmd.exe only and stays literal in
    PowerShell, granting a principal that does not exist. A directory's grants
    inherit to new files ((OI)(CI))."""
    import getpass

    rights = "(OI)(CI)F" if directory else "F"
    return f'icacls "{path}" /inheritance:r /grant:r "{getpass.getuser()}:{rights}" /grant:r "SYSTEM:{rights}"'
