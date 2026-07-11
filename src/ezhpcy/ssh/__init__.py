import shlex
from pathlib import Path, PurePosixPath

import paramiko

from ezhpcy.config import ConnectionInfo, RemoteFileConfig
from ezhpcy.constants import PACKAGE_NAME


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

        Note that `environment` requires the SSH server to support the `AcceptEnv` directive for the specified environment variables.
        If the server does not support this, the environment variables will not be set.
        It is more reliable to set environment variables in the command itself, e.g. `env VAR=value command`.
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

    def upload_text(
        self,
        content: str,
        remote_path: PurePosixPath,
        encoding: str = "utf-8",
    ) -> None:
        """Upload text content directly to a file on the remote host."""
        with self.open_sftp() as sftp:
            with sftp.file(str(remote_path), "w") as remote_file:
                remote_file.write(content.encode(encoding))

    def run_pixi(
        self,
        args: list[str],
        file_config: RemoteFileConfig | None = None,
        **run_kwargs,
    ) -> str:
        """Run private Pixi remotely with ezhpcy's XDG-resolved home and cache."""
        file_config = file_config or self.get_file_config()
        pixi_home = file_config.data_dir / PACKAGE_NAME / "pixi_home"
        pixi_cache_dir = file_config.cache_dir / PACKAGE_NAME / "pixi_cache"
        pixi = pixi_home / "bin/pixi"

        return self.run(
            [
                "env",
                f"PIXI_HOME={pixi_home}",
                f"PIXI_CACHE_DIR={pixi_cache_dir}",
                str(pixi),
                *args,
            ],
            **run_kwargs,
        )

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
            '"${EZHPCY_CONFIG_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/ezhpcy/ezhpcy.toml}"'
        )
        raw = self.run(["bash", "-lc", cmd]).strip()

        file_config = RemoteFileConfig.model_validate_json(raw, strict=True)
        return file_config
