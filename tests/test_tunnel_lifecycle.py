from contextlib import contextmanager
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest

from ezhpcy._legacy_provision_host import (
    ProvisioningError,
    provision_openssh,
    provision_pixi,
    validate_worker_infrastructure,
)
from ezhpcy.constants import (
    EZHPCY_VERSION,
    OPENSSH_MATCHSPEC,
    PIXI_INSTALLER_URL,
    PIXI_VERSION,
)
from ezhpcy.tunnel.sshd import absolute_sshd_command, sshd_config_arguments
from ezhpcy.types import RemoteState


class StubSFTP:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def stat(self, path: str) -> SimpleNamespace:
        if path not in self.files:
            raise FileNotFoundError(path)
        return SimpleNamespace()


class StubSSH:
    def __init__(self) -> None:
        self.sftp = StubSFTP()
        self.commands: list[list[str]] = []
        self.pixi_commands: list[list[str]] = []
        self.remote_state = RemoteState(
            cache_dir=PurePosixPath("/home/alice/.cache"),
            state_dir=PurePosixPath("/home/alice/.local/state"),
        )

    @contextmanager
    def sftp_client(self):
        yield self.sftp

    def run(self, args: list[str]) -> str:
        self.commands.append(args)
        return ""

    def run_pixi(self, args: list[str], **_kwargs) -> str:
        self.pixi_commands.append(args)
        return ""


def test_provision_pixi_installs_the_pinned_version_in_a_versioned_cache() -> None:
    ssh = StubSSH()

    provision_pixi(ssh, ssh.remote_state)  # type: ignore[arg-type]

    assert len(ssh.commands) == 1
    command = ssh.commands[0]
    assert command[:2] == ["bash", "-c"]
    inline_script = command[2]
    expected_home = f"/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}/pixi/{PIXI_VERSION}"
    assert f"if [ ! -x {expected_home}/bin/pixi ]" in inline_script
    assert f"PIXI_HOME={expected_home}" in inline_script
    assert f"PIXI_VERSION={PIXI_VERSION}" in inline_script
    assert PIXI_INSTALLER_URL in inline_script


def test_provision_openssh_reuses_absolute_sshd_command() -> None:
    ssh = StubSSH()

    provision_openssh(ssh, ssh.remote_state)  # type: ignore[arg-type]

    assert ssh.pixi_commands == [
        [
            "exec",
            f"--spec={OPENSSH_MATCHSPEC}",
            *absolute_sshd_command(["-V"]),
        ]
    ]


def test_validate_worker_infrastructure_is_read_only() -> None:
    ssh = StubSSH()
    remote_ssh = PurePosixPath(
        f"/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}/ssh/machine-id/"
        "alice@login.example.com"
    )
    required = (
        PurePosixPath(
            f"/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}/pixi/{PIXI_VERSION}/bin/pixi"
        ),
        remote_ssh / "ssh_host_ed25519_key",
        remote_ssh / "ssh_host_ed25519_key.pub",
    )
    for path in required:
        ssh.sftp.files[str(path)] = b"present"
    result = validate_worker_infrastructure(  # type: ignore[arg-type]
        ssh,
        ssh.remote_state,
        remote_username="alice",
        remote_host="login.example.com",
        machine_id="machine-id",
    )

    assert result is None
    assert ssh.commands == []
    assert ssh.pixi_commands == []


def test_validate_worker_infrastructure_reports_missing_files() -> None:
    ssh = StubSSH()

    with pytest.raises(ProvisioningError, match="ezhpcy provision") as exc_info:
        validate_worker_infrastructure(  # type: ignore[arg-type]
            ssh,
            ssh.remote_state,
            remote_username="alice",
            remote_host="login.example.com",
            machine_id="machine-id",
        )

    assert f"/ezhpcy/{EZHPCY_VERSION}/pixi/{PIXI_VERSION}/bin/pixi" in str(
        exc_info.value
    )
    assert (
        f"/ezhpcy/{EZHPCY_VERSION}/ssh/machine-id/"
        "alice@login.example.com/ssh_host_ed25519_key"
    ) in str(exc_info.value)


def test_sshd_config_is_expressed_as_cli_arguments() -> None:
    arguments = sshd_config_arguments(
        host_key=PurePosixPath("/home/alice/.config/ezhpcy/ssh/host"),
        remote_username="alice",
        authorized_key=("ssh-ed25519", "PUBLICKEY"),
    )

    assert arguments[:2] == ["-f", "/dev/null"]
    settings = arguments[3::2]
    assert "HostKey=/home/alice/.config/ezhpcy/ssh/host" in settings
    assert "AuthorizedKeysFile=none" in settings
    assert "AuthorizedKeysCommand=/bin/echo ssh-ed25519 PUBLICKEY" in settings
    assert "AuthorizedKeysCommandUser=alice" in settings
    assert "AllowUsers=alice" in settings
    assert "Subsystem=sftp internal-sftp" in settings
    assert not any(setting.startswith("ClientAlive") for setting in settings)


def test_sshd_config_can_end_sessions_with_unreachable_clients() -> None:
    arguments = sshd_config_arguments(
        host_key=PurePosixPath("/home/alice/.config/ezhpcy/ssh/host"),
        remote_username="alice",
        authorized_key=("ssh-ed25519", "PUBLICKEY"),
        client_alive=(30, 3),
    )

    settings = arguments[3::2]
    assert "ClientAliveInterval=30" in settings
    assert "ClientAliveCountMax=3" in settings


@pytest.mark.parametrize("client_alive", [(0, 3), (30, 0)])
def test_sshd_config_rejects_non_positive_client_alive(
    client_alive: tuple[int, int],
) -> None:
    with pytest.raises(ValueError, match="client-alive"):
        sshd_config_arguments(
            host_key=PurePosixPath("/home/alice/.config/ezhpcy/ssh/host"),
            remote_username="alice",
            authorized_key=("ssh-ed25519", "PUBLICKEY"),
            client_alive=client_alive,
        )
