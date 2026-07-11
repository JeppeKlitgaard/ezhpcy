import shlex
from pathlib import Path, PurePosixPath

import paramiko

from ezhpcy.config import ConnectionInfo, RemoteFileConfig


class SSHClient(paramiko.SSHClient):
    """
    SSHClient subclass with some convenience methods.
    """

    conn_info: ConnectionInfo

    def __init__(self, conn_info: ConnectionInfo):
        super().__init__()
        self.conn_info = conn_info

    def run(
        self,
        args: list[str],
        bufsize: int = -1,
        timeout: int | None = None,
        get_pty: bool = False,
        environment: dict[str, str] | None = None,
    ) -> str:
        """
        Convenience method to run a command on the remote host and return its output as a string.

        Handles escaping of the command and its arguments, and raises an exception if the command fails.
        """
        escaped_cmd = shlex.join(args)
        _stdin, stdout, stderr = self.exec_command(
            escaped_cmd,
            bufsize=bufsize,
            timeout=timeout,
            get_pty=get_pty,
            environment=environment,
        )

        exit_status = stdout.channel.recv_exit_status()
        if exit_status != 0:
            message = stderr.read().decode().strip()
            raise RuntimeError(f"Remote command failed ({exit_status}): {message}")

        return stdout.read().decode()

    def upload_file(self, local_path: Path, remote_path: PurePosixPath) -> None:
        """
        Upload a file to the remote host.
        """
        with self.open_sftp() as sftp:
            sftp.put(local_path, str(remote_path))

    def get_file_config(self) -> RemoteFileConfig:
        """
        Get the FileConfig from the remote host.

        Respects XDG directory specifications.
        """

        cmd = (
            'printf \'{"cache_dir":"%s","config_dir":"%s","data_dir":"%s","config_file":"%s"}\' '
            '"${XDG_CONFIG_HOME:-$HOME/.config}" '
            '"${XDG_CACHE_HOME:-$HOME/.cache}" '
            '"${XDG_DATA_HOME:-$HOME/.local/share}" '
            '"${EZHPCY_CONFIG_FILE:-${XDG_CONFIG_HOME:-$HOME/.local/share}/ezhpcy/ezhpcy.toml}"'
        )
        raw = self.run(["bash", "-lc", cmd]).strip()

        file_config = RemoteFileConfig.model_validate_json(raw, strict=True)
        return file_config
