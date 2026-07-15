import stat
from pathlib import PurePosixPath
from unittest.mock import MagicMock, call, patch

import paramiko

from ezhpcy.config import ConnectionInfo, RemoteFileConfig
from ezhpcy.ssh import SFTPClient, SSHClient


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
    file_config = RemoteFileConfig(
        cache_dir=PurePosixPath("/cache"),
        config_dir=PurePosixPath("/config"),
        data_dir=PurePosixPath("/data"),
        runtime_dir=PurePosixPath("/runtime/ezhpcy"),
    )

    with patch.object(client, "run", return_value="pixi output") as run:
        output = client.run_pixi(
            ["exec", "--spec=openssh", "sshd", "-V"],
            file_config=file_config,
        )

    assert output == "pixi output"
    run.assert_called_once_with(
        [
            "env",
            "PIXI_HOME=/data/ezhpcy/pixi_home",
            "PIXI_CACHE_DIR=/cache/ezhpcy/pixi_cache",
            "/data/ezhpcy/pixi_home/bin/pixi",
            "exec",
            "--spec=openssh",
            "sshd",
            "-V",
        ]
    )


def test_run_login_shell_safely_quotes_the_nested_command() -> None:
    client = SSHClient(ConnectionInfo(host="login.example.com"))

    with patch.object(client, "run", return_value="submitted") as run:
        output = client.run_login_shell(
            ["bsub", "-J", "worker name", "sh", "-c", "echo '$HOME'"],
            timeout=30,
        )

    assert output == "submitted"
    run.assert_called_once_with(
        [
            "bash",
            "-lc",
            "bsub -J 'worker name' sh -c 'echo '\"'\"'$HOME'\"'\"''",
        ],
        timeout=30,
    )


def test_start_login_shell_opens_pty_and_keeps_channel_running() -> None:
    client = SSHClient(ConnectionInfo(host="login.example.com"))
    transport = MagicMock()
    transport.is_active.return_value = True
    channel = MagicMock()
    transport.open_session.return_value = channel

    with patch.object(client, "get_transport", return_value=transport):
        process = client.start_login_shell(
            ["bsub", "-Is", "-J", "worker name", "sleep", "60"]
        )

    assert process is channel
    channel.get_pty.assert_called_once_with()
    channel.exec_command.assert_called_once_with(
        "bash -lc 'bsub -Is -J '\"'\"'worker name'\"'\"' sleep 60'"
    )


def test_get_file_config_maps_xdg_directories_to_the_correct_fields() -> None:
    client = SSHClient(ConnectionInfo(host="login.example.com"))
    response = (
        '{"cache_dir":"/cache","config_dir":"/config",'
        '"data_dir":"/data","runtime_dir":"/runtime/ezhpcy"}'
    )

    with patch.object(client, "run", return_value=response) as run:
        file_config = client.get_file_config()

    assert file_config.cache_dir == PurePosixPath("/cache")
    assert file_config.config_dir == PurePosixPath("/config")
    assert file_config.runtime_dir == PurePosixPath("/runtime/ezhpcy")
    remote_script = run.call_args.args[0][2]
    assert "XDG_RUNTIME_DIR" in remote_script
    assert "TMPDIR" in remote_script
    assert "ezhpcy-$(id -u)" in remote_script
