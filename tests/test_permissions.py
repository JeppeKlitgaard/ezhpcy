import ctypes
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ezhpcy.permissions import (
    FilePermissionError,
    restrict_to_current_user,
    windows_current_user_sid,
)

_WHOAMI = ["whoami", "/user", "/fo", "csv", "/nh"]
_DACL_SECURITY_INFORMATION = 0x00000004

requires_windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")


@pytest.fixture
def uncached_sid() -> Iterator[None]:
    windows_current_user_sid.cache_clear()
    yield
    windows_current_user_sid.cache_clear()


def whoami_output(sid: str) -> SimpleNamespace:
    return SimpleNamespace(stdout=f'"MACHINE\alice","{sid}"\n')


def windows_dacl(path: Path) -> str:
    """Return a file's DACL in SDDL, as `icacls /save` writes it."""
    saved = path.with_name(f"{path.name}.acl")
    subprocess.run(
        ["icacls", str(path), "/save", str(saved)], check=True, capture_output=True
    )
    return saved.read_text(encoding="utf-16-le").splitlines()[1]


def windows_sddl(sddl: str) -> str:
    """
    Return a DACL in SDDL as Windows renders it, which abbreviates some SIDs,
    e.g. the built-in Administrator account (RID 500) that CI runs as to LA.
    """
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32")
    descriptor = ctypes.c_void_p()
    rendered = ctypes.c_wchar_p()
    assert advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), None
    ), ctypes.WinError(ctypes.get_last_error())
    try:
        assert advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, 1, _DACL_SECURITY_INFORMATION, ctypes.byref(rendered), None
        ), ctypes.WinError(ctypes.get_last_error())
        try:
            return rendered.value or ""
        finally:
            kernel32.LocalFree(rendered)
    finally:
        kernel32.LocalFree(descriptor)


@requires_windows
def test_windows_dacl_replaces_explicit_entries_with_the_current_user(
    tmp_path: Path, uncached_sid: None
) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "a.conf"
    path.touch()
    # An elevated GitHub runner leaves the directory's SYSTEM, Administrators and
    # OWNER RIGHTS entries on the file as explicit entries; recreate that.
    subprocess.run(["icacls", str(path), "/inheritance:d"], check=True)
    assert "(A;;FA;;;OW)" in windows_dacl(path)

    restrict_to_current_user(path)

    assert windows_dacl(path) == windows_sddl(
        f"D:P(A;;FA;;;{windows_current_user_sid()})"
    )


@requires_windows
def test_unsettable_windows_dacl_is_a_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "ezhpcy.permissions.windows_current_user_sid", lambda: "not-a-sid"
    )
    path = tmp_path / "a.conf"
    path.touch()

    with pytest.raises(FilePermissionError, match="Could not restrict"):
        restrict_to_current_user(path)


def test_windows_user_sid_is_looked_up_once(uncached_sid: None) -> None:
    with patch(
        "ezhpcy.permissions.subprocess.run",
        return_value=whoami_output("S-1-5-21-1001"),
    ) as run:
        assert windows_current_user_sid() == "S-1-5-21-1001"
        assert windows_current_user_sid() == "S-1-5-21-1001"

    run.assert_called_once()
    assert run.call_args.args[0] == _WHOAMI


@pytest.mark.parametrize("stdout", ["", '"MACHINE\alice","not-a-sid"\n'])
def test_unusable_whoami_output_is_a_permission_error(
    uncached_sid: None, stdout: str
) -> None:
    with (
        patch(
            "ezhpcy.permissions.subprocess.run",
            return_value=SimpleNamespace(stdout=stdout),
        ),
        pytest.raises(FilePermissionError),
    ):
        windows_current_user_sid()


def test_posix_permissions_need_no_windows_acl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ezhpcy.permissions.sys.platform", "linux")
    path = tmp_path / "a.conf"
    path.touch()

    with (
        patch.object(Path, "chmod", autospec=True) as chmod,
        patch("ezhpcy.permissions.windows_current_user_sid") as user_sid,
    ):
        restrict_to_current_user(path)

    chmod.assert_called_once_with(path, 0o600)
    user_sid.assert_not_called()
