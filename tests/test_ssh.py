from pathlib import PurePosixPath
from unittest.mock import MagicMock, patch

from ezhpcy.config import ConnectionInfo
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
