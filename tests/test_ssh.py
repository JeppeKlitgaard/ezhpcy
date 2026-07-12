import stat
from pathlib import PurePosixPath
from unittest.mock import MagicMock, call, patch

import paramiko

from ezhpcy.config import ConnectionInfo, RemoteFileConfig
from ezhpcy.ssh import SFTPClient, SSHClient


def test_sftp_client_context_manager_closes_custom_client() -> None:
    client = SSHClient(ConnectionInfo())
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


def test_upload_text_writes_encoded_content() -> None:
    client = SSHClient(ConnectionInfo())
    sftp = MagicMock(spec=SFTPClient)
    sftp_client = MagicMock()
    sftp_client.__enter__.return_value = sftp

    with patch.object(client, "sftp_client", return_value=sftp_client):
        client.upload_text("AllowUsers s250250\n", PurePosixPath("/remote/sshd_config"))

    sftp.write_text.assert_called_once_with(
        PurePosixPath("/remote/sshd_config"),
        "AllowUsers s250250\n",
        encoding="utf-8",
    )


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
    client = SSHClient(ConnectionInfo())
    file_config = RemoteFileConfig(
        cache_dir=PurePosixPath("/cache"),
        config_dir=PurePosixPath("/config"),
        data_dir=PurePosixPath("/data"),
        config_file=PurePosixPath("/config/ezhpcy/ezhpcy.toml"),
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
