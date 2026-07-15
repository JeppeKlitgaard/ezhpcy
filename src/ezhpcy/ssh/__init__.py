import logging
import shlex
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import PurePosixPath

import paramiko

from ezhpcy.config import ConnectionInfo
from ezhpcy.scheduler.base import RemoteProcess
from ezhpcy.types import RemoteState

logger = logging.getLogger(__name__)


class SFTPClient(paramiko.SFTPClient):
    """SFTP client with convenience methods for ezhpcy's remote operations."""

    def read_bytes(self, path: str | PurePosixPath) -> bytes:
        """Read a remote file as bytes."""
        with self.file(str(path), "rb") as remote_file:
            return remote_file.read()

    def write_bytes(self, path: str | PurePosixPath, content: bytes) -> int:
        """Write bytes to a remote file."""
        with self.file(str(path), "wb") as remote_file:
            remote_file.write(content)
        return len(content)

    def read_text(
        self,
        path: str | PurePosixPath,
        encoding: str = "utf-8",
        errors: str = "strict",
    ) -> str:
        """Read and decode a remote text file."""
        with self.file(str(path), "r") as remote_file:
            return remote_file.read().decode(encoding, errors)

    def write_text(
        self,
        path: str | PurePosixPath,
        content: str,
        encoding: str = "utf-8",
        errors: str = "strict",
    ) -> int:
        """Encode and write text to a remote file."""
        with self.file(str(path), "w") as remote_file:
            remote_file.write(content.encode(encoding, errors))
        return len(content)

    def mkdir(
        self,
        path: str | PurePosixPath,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        """Create a remote directory with semantics matching ``Path.mkdir``."""
        remote_path = PurePosixPath(path)

        try:
            super().mkdir(str(remote_path), mode=mode)
        except FileNotFoundError:
            if not parents or remote_path.parent == remote_path:
                raise
            self.mkdir(remote_path.parent, parents=True, exist_ok=True)
            self.mkdir(remote_path, mode=mode, exist_ok=exist_ok)
        except OSError:
            if not exist_ok:
                raise
            attributes = self.stat(str(remote_path))
            if attributes.st_mode is None or not stat.S_ISDIR(attributes.st_mode):
                raise


class SSHClient(paramiko.SSHClient):
    """
    SSHClient subclass with some convenience methods.
    """

    conn_info: ConnectionInfo

    def __init__(self, conn_info: ConnectionInfo):
        super().__init__()
        self.conn_info = conn_info

    @contextmanager
    def sftp_client(self) -> Iterator[SFTPClient]:
        """Open an ezhpcy SFTP client for this SSH connection."""
        transport = self.get_transport()
        if transport is None or not transport.is_active():
            raise paramiko.SSHException("SSH session is not active")

        sftp = SFTPClient.from_transport(transport)
        try:
            yield sftp
        finally:
            sftp.close()

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

    def run_pixi(
        self,
        args: list[str],
        remote_state: RemoteState,
        **run_kwargs,
    ) -> str:
        """Run the pinned private Pixi with ezhpcy's XDG-resolved cache."""
        home = remote_state.pixi_home()
        cache = remote_state.pixi_cache_dir()
        pixi = remote_state.pixi_executable()

        return self.run(
            [
                "env",
                f"PIXI_HOME={home}",
                f"PIXI_CACHE_DIR={cache}",
                str(pixi),
                *args,
            ],
            **run_kwargs,
        )

    def run_login_shell(self, args: list[str], **run_kwargs) -> str:
        """Run a safely quoted command through the remote Bash login shell."""
        return self.run(["bash", "-lc", shlex.join(args)], **run_kwargs)

    def start_login_shell(self, args: list[str]) -> RemoteProcess:
        """Start a command in a remote Bash login shell with a pseudo-terminal."""
        transport = self.get_transport()
        if transport is None or not transport.is_active():
            raise paramiko.SSHException("SSH session is not active")
        channel = transport.open_session()
        try:
            channel.get_pty()
            channel.exec_command(shlex.join(["bash", "-lc", shlex.join(args)]))
        except BaseException:
            channel.close()
            raise
        return channel

    def get_remote_state(self) -> RemoteState:
        """
        Discover the state of the remote host.

        Respects XDG directory specifications.
        """

        cmd = 'printf \'{"cache_dir":"%s"}\' "${XDG_CACHE_HOME:-$HOME/.cache}"'
        raw = self.run(["bash", "-lc", cmd]).strip()

        remote_state = RemoteState.model_validate_json(raw, strict=True)
        logger.debug("Discovered remote state: %s", remote_state)
        return remote_state
