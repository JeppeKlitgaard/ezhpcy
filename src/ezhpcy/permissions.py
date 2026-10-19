"""Local file permissions that OpenSSH accepts on POSIX and Windows."""

import functools
import sys
lazy import csv
lazy import ctypes
lazy import subprocess
lazy from pathlib import Path

from ezhpcy.messages import MessageError
lazy from ezhpcy.constants import WINDOWS_CREATION_FLAGS

_SDDL_REVISION_1 = 1
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000


class FilePermissionError(MessageError, RuntimeError):
    pass


@functools.cache
def windows_current_user_sid() -> str:
    """Return the current Windows user's SID using a system-provided command."""
    try:
        result = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            check=True,
            capture_output=True,
            text=True,
            creationflags=WINDOWS_CREATION_FLAGS,
        )
        row = next(csv.reader(result.stdout.splitlines()))
        sid = row[1]
    except (IndexError, OSError, StopIteration, subprocess.CalledProcessError) as error:
        raise FilePermissionError(
            t"Could not determine the current Windows user for file permissions."
        ) from error
    if not sid.startswith("S-"):
        raise FilePermissionError(
            t"Windows returned an invalid user SID while securing a file."
        )
    return sid


def private_directory(path: Path) -> None:
    """
    Make a directory, with its parents, that only the current user can enter.

    On POSIX, `mkdir` applies the umask and leaves an existing directory as it
    is, so the mode is set again. On Windows, `mkdir` gives a new directory a
    private ACL, and `chmod` would only set its read-only flag.
    """
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if sys.platform != "win32":
        path.chmod(0o700)


# This is a hacky fix because Windows is a nightmare OS that should not exist.
def restrict_to_current_user(path: Path) -> None:
    """
    Make a file readable and writable by the current user only.

    OpenSSH refuses private keys and included config files that anyone else may
    write. On Windows, ACLs routinely grant that (e.g. sandbox groups on %TEMP%),
    and Python's private mkdir ACL uses OWNER RIGHTS, which OpenSSH also rejects.

    On Windows the DACL is replaced whole with one protected entry for the user.
    Editing it with `icacls /inheritance:r` leaves explicit entries in place, and
    under an elevated token those include copies of the directory's inherited
    entries, such as OWNER RIGHTS.
    """
    path.chmod(0o600)
    # Type checkers narrow `sys.platform` only inside an `if` block, not after an
    # early return, so the Windows-only code stays in one.
    if sys.platform == "win32":
        sid = windows_current_user_sid()
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        descriptor = ctypes.c_void_p()
        try:
            if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                f"D:P(A;;FA;;;{sid})",
                _SDDL_REVISION_1,
                ctypes.byref(descriptor),
                None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if not advapi32.SetFileSecurityW(
                str(path),
                _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
                descriptor,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        except OSError as error:
            raise FilePermissionError(
                t"Could not restrict Windows permissions on {path:path}."
            ) from error
        finally:
            kernel32.LocalFree(descriptor)
