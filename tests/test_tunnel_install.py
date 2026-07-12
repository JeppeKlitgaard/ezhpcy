from pathlib import Path, PurePosixPath

import pytest
import typer

from ezhpcy import console
from ezhpcy.cli.tunnel.install import (
    WORKER_HOST_ALIAS,
    _ensure_not_installed,
    _pin_worker_host_key,
    _render_sshd_config,
)


class StubSSH:
    def __init__(self, error: RuntimeError | None = None) -> None:
        self.error = error
        self.commands: list[list[str]] = []

    def run(self, args: list[str]) -> str:
        self.commands.append(args)
        if self.error is not None:
            raise self.error
        return "EZHPCY Version: 0.1.0\n"


def test_ensure_not_installed_checks_remote_version() -> None:
    ssh = StubSSH(error=RuntimeError("command not found"))

    _ensure_not_installed(ssh)  # type: ignore[arg-type]

    assert ssh.commands == [["ezhpcy", "version"]]


def test_ensure_not_installed_aborts_when_version_succeeds() -> None:
    ssh = StubSSH()

    with console.capture() as captured:
        with pytest.raises(typer.Exit) as exc_info:
            _ensure_not_installed(ssh)  # type: ignore[arg-type]

    assert exc_info.value.exit_code == 1
    assert ssh.commands == [["ezhpcy", "version"]]
    output = " ".join(captured.get().split())
    assert "ezhpcy tunnel reinstall" in output
    assert "ezhpcy tunnel uninstall" in output


def test_render_sshd_config_replaces_remote_values() -> None:
    rendered = _render_sshd_config(
        "AllowUsers {{ remote_username }}\nHostKey {{ remote_config_dir }}/host\n",
        remote_username="alice",
        remote_config_dir=PurePosixPath("/home/alice/.config/ezhpcy/ssh"),
    )

    assert rendered == ("AllowUsers alice\nHostKey /home/alice/.config/ezhpcy/ssh/host")


def test_pin_worker_host_key_preserves_unrelated_entries(tmp_path: Path) -> None:
    known_hosts = tmp_path / "worker_known_hosts"
    known_hosts.write_text(
        f"other ssh-ed25519 OTHER\n{WORKER_HOST_ALIAS} ssh-ed25519 OLD\n",
        encoding="utf-8",
    )

    _pin_worker_host_key("ssh-ed25519 NEW remote-comment\n", known_hosts)

    assert known_hosts.read_text(encoding="utf-8") == (
        f"other ssh-ed25519 OTHER\n{WORKER_HOST_ALIAS} ssh-ed25519 NEW\n"
    )
