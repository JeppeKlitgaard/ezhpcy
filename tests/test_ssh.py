from pathlib import PurePosixPath
from unittest.mock import MagicMock, patch

from ezhpcy.config import ConnectionInfo, RemoteFileConfig
from ezhpcy.ssh import SSHClient


def test_upload_text_writes_encoded_content() -> None:
    client = SSHClient(ConnectionInfo())
    sftp = MagicMock()
    remote_file = MagicMock()
    sftp.file.return_value.__enter__.return_value = remote_file
    open_sftp = MagicMock()
    open_sftp.__enter__.return_value = sftp

    with patch.object(client, "open_sftp", return_value=open_sftp):
        client.upload_text("AllowUsers s250250\n", PurePosixPath("/remote/sshd_config"))

    sftp.file.assert_called_once_with("/remote/sshd_config", "w")
    remote_file.write.assert_called_once_with("AllowUsers s250250\n".encode())


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
