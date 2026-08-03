import shlex
import stat
from pathlib import PurePosixPath
from unittest.mock import MagicMock, call, patch

import paramiko
import pytest

from ezhpcy.cli.utils.ssh import InteractiveSSHClient, _send_server_alive_requests
from ezhpcy.config import ConnectionInfo
from ezhpcy.constants import EZHPCY_VERSION, PIXI_VERSION
from ezhpcy.ssh import SFTPClient, SSHClient
from ezhpcy.types import RemoteState


def test_interactive_ssh_prompts_after_key_authentication_fails() -> None:
    client = InteractiveSSHClient(
        ConnectionInfo(user="alice", host="login.example.com")
    )
    transport = MagicMock()
    transport.is_active.return_value = True

    with (
        patch.object(
            client,
            "connect",
            side_effect=[paramiko.AuthenticationException("keys rejected"), None],
        ) as connect,
        patch(
            "ezhpcy.cli.utils.ssh.Prompt.ask",
            return_value="secret",
        ) as prompt,
        patch.object(client, "get_transport", return_value=transport),
    ):
        client.interactive_connect()

    assert connect.call_args_list == [
        call(
            hostname="login.example.com",
            username="alice",
            password=None,
        ),
        call(
            hostname="login.example.com",
            username="alice",
            password="secret",
        ),
    ]
    prompt.assert_called_once()
    transport.set_keepalive.assert_called_once_with(30)


def test_interactive_ssh_enables_default_transport_keepalive() -> None:
    client = InteractiveSSHClient(
        ConnectionInfo(user="alice", host="login.example.com")
    )
    transport = MagicMock()
    transport.is_active.return_value = True

    with (
        patch.object(client, "connect") as connect,
        patch.object(client, "get_transport", return_value=transport),
    ):
        client.interactive_connect()

    connect.assert_called_once_with(
        hostname="login.example.com",
        username="alice",
        password=None,
    )
    transport.set_keepalive.assert_called_once_with(30)


def test_interactive_ssh_uses_configured_transport_keepalive() -> None:
    client = InteractiveSSHClient(
        ConnectionInfo(
            user="alice",
            host="login.example.com",
            ssh_keepalive_interval_seconds=75,
        )
    )
    transport = MagicMock()
    transport.is_active.return_value = True

    with (
        patch.object(client, "connect"),
        patch.object(client, "get_transport", return_value=transport),
    ):
        client.interactive_connect()

    transport.set_keepalive.assert_called_once_with(75)


def test_interactive_ssh_server_alive_requests_require_a_reply() -> None:
    transport = MagicMock()
    transport.is_active.return_value = True
    stop_requested = MagicMock()
    stop_requested.wait.side_effect = [False, True]

    _send_server_alive_requests(transport, stop_requested, 30)

    assert stop_requested.wait.call_args_list == [call(30), call(30)]
    transport.global_request.assert_called_once_with("keepalive@openssh.com", wait=True)


def test_interactive_ssh_does_not_prompt_for_unsupported_password_auth() -> None:
    client = InteractiveSSHClient(
        ConnectionInfo(user="alice", host="login.example.com")
    )
    error = paramiko.BadAuthenticationType("unsupported", ["publickey"])

    with (
        patch.object(client, "connect", side_effect=error),
        patch("ezhpcy.cli.utils.ssh.Prompt.ask") as prompt,
        pytest.raises(paramiko.BadAuthenticationType),
    ):
        client.interactive_connect()

    prompt.assert_not_called()


def test_interactive_ssh_does_not_prompt_when_password_prompt_is_disabled() -> None:
    client = InteractiveSSHClient(
        ConnectionInfo(
            user="alice",
            host="login.example.com",
        ),
        password_prompt=False,
    )
    error = paramiko.AuthenticationException("keys rejected")

    with (
        patch.object(client, "connect", side_effect=error) as connect,
        patch("ezhpcy.cli.utils.ssh.Prompt.ask") as prompt,
        pytest.raises(paramiko.AuthenticationException) as raised,
    ):
        client.interactive_connect()

    assert raised.value is error
    connect.assert_called_once_with(
        hostname="login.example.com",
        username="alice",
        password=None,
    )
    prompt.assert_not_called()


def test_interactive_ssh_propagates_explicit_password_failure() -> None:
    client = InteractiveSSHClient(
        ConnectionInfo(
            user="alice",
            host="login.example.com",
            password="incorrect",
        )
    )

    with (
        patch.object(
            client,
            "connect",
            side_effect=paramiko.AuthenticationException("incorrect password"),
        ),
        patch("ezhpcy.cli.utils.ssh.Prompt.ask") as prompt,
        pytest.raises(paramiko.AuthenticationException),
    ):
        client.interactive_connect()

    prompt.assert_not_called()


def test_sftp_read_and_write_bytes() -> None:
    sftp = MagicMock(spec=SFTPClient)
    remote_file = MagicMock()
    remote_file.read.return_value = b"payload"
    sftp.file.return_value.__enter__.return_value = remote_file

    assert SFTPClient.read_bytes(sftp, "/remote/payload") == b"payload"
    sftp.file.assert_called_once_with("/remote/payload", "rb")

    sftp.file.reset_mock()
    assert SFTPClient.write_bytes(sftp, "/remote/payload", b"new") == 3
    sftp.file.assert_called_once_with("/remote/payload", "wb")
    remote_file.write.assert_called_once_with(b"new")


def test_sftp_client_context_manager_closes_custom_client() -> None:
    client = SSHClient(ConnectionInfo(host="login.example.com"))
    transport = MagicMock()
    transport.is_active.return_value = True
    sftp = MagicMock(spec=SFTPClient)

    with (
        patch.object(client, "get_transport", return_value=transport),
        patch.object(SFTPClient, "from_transport", return_value=sftp) as from_transport,
    ):
        with client.sftp_client() as opened_sftp:
            assert opened_sftp is sftp

    from_transport.assert_called_once_with(transport)
    sftp.close.assert_called_once_with()


def test_sftp_read_text_decodes_remote_file() -> None:
    sftp = MagicMock(spec=SFTPClient)
    remote_file = MagicMock()
    remote_file.read.return_value = "nøgle\n".encode("utf-16")
    sftp.file.return_value.__enter__.return_value = remote_file

    content = SFTPClient.read_text(
        sftp,
        PurePosixPath("/remote/host_key.pub"),
        encoding="utf-16",
    )

    assert content == "nøgle\n"
    sftp.file.assert_called_once_with("/remote/host_key.pub", "r")


def test_sftp_write_text_encodes_remote_file() -> None:
    sftp = MagicMock(spec=SFTPClient)
    remote_file = MagicMock()
    sftp.file.return_value.__enter__.return_value = remote_file

    written = SFTPClient.write_text(
        sftp,
        PurePosixPath("/remote/sshd_config"),
        "nøgle\n",
        encoding="utf-16",
    )

    assert written == len("nøgle\n")
    sftp.file.assert_called_once_with("/remote/sshd_config", "w")
    remote_file.write.assert_called_once_with("nøgle\n".encode("utf-16"))


def test_sftp_mkdir_creates_missing_parents() -> None:
    sftp = object.__new__(SFTPClient)

    with patch.object(
        paramiko.SFTPClient,
        "mkdir",
        side_effect=[FileNotFoundError, FileNotFoundError, None, None, None],
    ) as mkdir:
        sftp.mkdir(PurePosixPath("/remote/ezhpcy/ssh"), parents=True)

    assert mkdir.call_args_list == [
        call("/remote/ezhpcy/ssh", mode=0o777),
        call("/remote/ezhpcy", mode=0o777),
        call("/remote", mode=0o777),
        call("/remote/ezhpcy", mode=0o777),
        call("/remote/ezhpcy/ssh", mode=0o777),
    ]


def test_sftp_mkdir_allows_existing_directory() -> None:
    sftp = object.__new__(SFTPClient)
    directory = MagicMock(st_mode=stat.S_IFDIR | 0o755)

    with (
        patch.object(paramiko.SFTPClient, "mkdir", side_effect=OSError),
        patch.object(paramiko.SFTPClient, "stat", return_value=directory),
    ):
        sftp.mkdir(PurePosixPath("/remote/ezhpcy"), exist_ok=True)


def test_run_pixi_uses_ezhpcy_xdg_directories() -> None:
    client = SSHClient(ConnectionInfo(host="login.example.com"))
    remote_state = RemoteState(
        cache_dir=PurePosixPath("/cache"),
    )

    with patch.object(client, "run", return_value="pixi output") as run:
        output = client.run_pixi(
            ["exec", "--spec=openssh", "sshd", "-V"],
            remote_state=remote_state,
        )

    assert output == "pixi output"
    run.assert_called_once_with(
        [
            "env",
            f"PIXI_HOME=/cache/ezhpcy/{EZHPCY_VERSION}/pixi/{PIXI_VERSION}",
            f"PIXI_CACHE_DIR=/cache/ezhpcy/{EZHPCY_VERSION}/pixi_cache/{PIXI_VERSION}",
            f"/cache/ezhpcy/{EZHPCY_VERSION}/pixi/{PIXI_VERSION}/bin/pixi",
            "exec",
            "--spec=openssh",
            "sshd",
            "-V",
        ]
    )


def test_run_login_shell_safely_quotes_the_nested_command() -> None:
    client = SSHClient(ConnectionInfo(host="login.example.com"))
    command = [
        "bsub",
        "-J",
        "worker name",
        "/path with spaces/sshd",
        "single'quote",
        "$HOME; echo unsafe",
    ]

    with patch.object(client, "run", return_value="submitted") as run:
        output = client.run_login_shell(command, timeout=30)

    assert output == "submitted"
    run.assert_called_once_with(["bash", "-lc", shlex.join(command)], timeout=30)


def test_start_login_shell_opens_pty_and_keeps_channel_running() -> None:
    client = SSHClient(ConnectionInfo(host="login.example.com"))
    transport = MagicMock()
    transport.is_active.return_value = True
    channel = MagicMock()
    transport.open_session.return_value = channel
    command = [
        "bsub",
        "-Is",
        "-J",
        "worker name",
        "/path with spaces/worker",
        "single'quote",
        "$HOME; echo unsafe",
    ]

    with patch.object(client, "get_transport", return_value=transport):
        process = client.start_login_shell(command)

    assert process is channel
    channel.get_pty.assert_called_once_with()
    serialized = shlex.join(["bash", "-lc", shlex.join(command)])
    channel.exec_command.assert_called_once_with(serialized)
    login_argv = shlex.split(serialized)
    assert login_argv[:2] == ["bash", "-lc"]
    assert shlex.split(login_argv[2]) == command


def test_get_remote_state_maps_the_xdg_cache_directory() -> None:
    client = SSHClient(ConnectionInfo(host="login.example.com"))
    response = '{"cache_dir":"/cache"}'

    with patch.object(client, "run", return_value=response) as run:
        remote_state = client.get_remote_state()

    assert remote_state.cache_dir == PurePosixPath("/cache")
    remote_script = run.call_args.args[0][2]
    assert "XDG_CACHE_HOME" in remote_script
