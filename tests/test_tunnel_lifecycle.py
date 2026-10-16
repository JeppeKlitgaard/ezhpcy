from pathlib import PurePosixPath

import pytest

from ezhpcy.worker.sshd import sshd_config_arguments


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
