"""Local file permissions that OpenSSH accepts on POSIX and Windows."""

import csv
import functools
import os
import subprocess
from pathlib import Path

from ezhpcy.constants import WINDOWS_CREATION_FLAGS


class FilePermissionError(RuntimeError):
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
            "Could not determine the current Windows user for file permissions."
        ) from error
    if not sid.startswith("S-"):
        raise FilePermissionError(
            "Windows returned an invalid user SID while securing a file."
        )
    return sid


# This is a hacky fix because Windows is a nightmare OS that should not exist.
def restrict_to_current_user(path: Path) -> None:
    """
    Make a file readable and writable by the current user only.

    OpenSSH refuses private keys and included config files that anyone else may
    write. On Windows, inherited ACLs routinely grant that (e.g. sandbox groups
    on %TEMP%), and Python's private mkdir ACL uses OWNER RIGHTS, which OpenSSH
    also rejects, so the ACL is replaced with a single explicit entry.
    """
    os.chmod(path, 0o600)
    if os.name != "nt":
        return

    sid = windows_current_user_sid()
    try:
        subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"*{sid}:(F)",
            ],
            check=True,
            capture_output=True,
            text=True,
            creationflags=WINDOWS_CREATION_FLAGS,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise FilePermissionError(
            f"Could not restrict Windows permissions on {path}."
        ) from error
