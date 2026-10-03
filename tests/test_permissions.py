import subprocess
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


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr("ezhpcy.permissions.os.name", "nt")
    windows_current_user_sid.cache_clear()
    yield
    windows_current_user_sid.cache_clear()


def whoami_output(sid: str) -> SimpleNamespace:
    return SimpleNamespace(stdout=f'"MACHINE\alice","{sid}"\n')


def test_windows_acl_grants_only_the_current_user(
    tmp_path: Path, windows: None
) -> None:
    path = tmp_path / "worker_client_ed25519"
    path.touch()

    with patch(
        "ezhpcy.permissions.subprocess.run",
        return_value=whoami_output("S-1-5-21-1001"),
    ) as run:
        restrict_to_current_user(path)

    assert [call.args[0] for call in run.call_args_list] == [
        _WHOAMI,
        ["icacls", str(path), "/inheritance:r", "/grant:r", "*S-1-5-21-1001:(F)"],
    ]


def test_windows_user_sid_is_looked_up_once(tmp_path: Path, windows: None) -> None:
    first, second = tmp_path / "a.conf", tmp_path / "b.conf"
    first.touch()
    second.touch()

    with patch(
        "ezhpcy.permissions.subprocess.run",
        return_value=whoami_output("S-1-5-21-1001"),
    ) as run:
        restrict_to_current_user(first)
        restrict_to_current_user(second)

    commands = [call.args[0] for call in run.call_args_list]
    assert commands.count(_WHOAMI) == 1
    assert len(commands) == 3


@pytest.mark.parametrize("stdout", ["", '"MACHINE\alice","not-a-sid"\n'])
def test_unusable_whoami_output_is_a_permission_error(
    tmp_path: Path, windows: None, stdout: str
) -> None:
    path = tmp_path / "a.conf"
    path.touch()

    with (
        patch(
            "ezhpcy.permissions.subprocess.run",
            return_value=SimpleNamespace(stdout=stdout),
        ),
        pytest.raises(FilePermissionError),
    ):
        restrict_to_current_user(path)


def test_icacls_failure_is_a_permission_error(tmp_path: Path, windows: None) -> None:
    path = tmp_path / "a.conf"
    path.touch()

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        if command[0] == "icacls":
            raise subprocess.CalledProcessError(5, command)
        return whoami_output("S-1-5-21-1001")

    with (
        patch("ezhpcy.permissions.subprocess.run", side_effect=run),
        pytest.raises(FilePermissionError, match="Could not restrict"),
    ):
        restrict_to_current_user(path)


def test_posix_permissions_need_no_helper_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ezhpcy.permissions.os.name", "posix")
    path = tmp_path / "a.conf"
    path.touch()

    with (
        patch.object(Path, "chmod", autospec=True) as chmod,
        patch("ezhpcy.permissions.subprocess.run") as run,
    ):
        restrict_to_current_user(path)

    chmod.assert_called_once_with(path, 0o600)
    run.assert_not_called()
