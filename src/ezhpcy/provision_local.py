"""The worker's SSH credentials on this machine, and where they go on the login node."""

from dataclasses import dataclass
lazy import base64
lazy import os
lazy from pathlib import Path, PurePosixPath
lazy from typing import Self

lazy import asyncssh

lazy from ezhpcy.config import config
lazy from ezhpcy.constants import (
    SSH_DIRECTORY_NAME,
    WORKER_CLIENT_KEY_NAME,
    WORKER_HOST_ALIAS,
    WORKER_HOST_KEY_NAME,
    WORKER_KNOWN_HOSTS_NAME,
)
lazy from ezhpcy.permissions import private_directory, restrict_to_current_user
lazy from ezhpcy.types import ConnectionInfo, RemoteState
lazy from ezhpcy.utils import ssh_connection_id


@dataclass(frozen=True)
class WorkerCredentials:
    """
    The worker's SSH credentials for one login endpoint: where they live on this
    machine and on the login node, and creating them locally. Each local machine
    has its own for each login user and host, so two login nodes never share
    keys.
    """

    machine_id: str
    login_user: str
    login_host: str

    @classmethod
    def for_login(cls, conn_info: ConnectionInfo, *, machine_id: str) -> Self:
        return cls(machine_id, conn_info.user, str(conn_info.host))

    @property
    def local_dir(self) -> Path:
        return config.local_file.ssh_dir(
            self.machine_id, user=self.login_user, host=self.login_host
        )

    @property
    def client_key(self) -> Path:
        return self.local_dir / WORKER_CLIENT_KEY_NAME

    @property
    def client_public_key(self) -> Path:
        return self.local_dir / f"{WORKER_CLIENT_KEY_NAME}.pub"

    @property
    def host_key(self) -> Path:
        return self.local_dir / WORKER_HOST_KEY_NAME

    @property
    def host_public_key(self) -> Path:
        return self.local_dir / f"{WORKER_HOST_KEY_NAME}.pub"

    @property
    def known_hosts(self) -> Path:
        """Pins the worker's host key for OpenSSH."""
        return self.local_dir / WORKER_KNOWN_HOSTS_NAME

    def missing_tunnel_files(self) -> list[Path]:
        """
        The local files a tunnel needs, as `ezhpcy provision` leaves them, that
        aren't there: the client key pair, and `known_hosts`.
        """
        required = (self.client_key, self.client_public_key, self.known_hosts)
        return [path for path in required if not path.is_file()]

    def remote_dir(self, remote_state: RemoteState) -> PurePosixPath:
        return (
            remote_state.package_cache_dir()
            / SSH_DIRECTORY_NAME
            / self.machine_id
            / ssh_connection_id(self.login_user, self.login_host)
        )

    def remote_host_key(self, remote_state: RemoteState) -> PurePosixPath:
        return self.remote_dir(remote_state) / WORKER_HOST_KEY_NAME

    def remote_host_public_key(self, remote_state: RemoteState) -> PurePosixPath:
        return self.remote_dir(remote_state) / f"{WORKER_HOST_KEY_NAME}.pub"

    def ensure_local_keys(self) -> None:
        """Create the client and host key pairs if they don't exist."""
        _ensure_key_pair(
            self.client_key, self.client_public_key, comment="ezhpcy worker client"
        )
        _ensure_key_pair(
            self.host_key, self.host_public_key, comment="ezhpcy worker host"
        )

    def authorized_key(self) -> tuple[str, str]:
        """The client key's type and base64 blob, for the worker's sshd to authorize."""
        try:
            key = asyncssh.read_public_key(self.client_public_key)
        except (OSError, asyncssh.KeyImportError) as error:
            raise RuntimeError(
                f"Could not read worker public key at {self.client_public_key}: {error}"
            ) from error
        if key.algorithm != b"ssh-ed25519":
            raise RuntimeError(
                f"The worker public key at {self.client_public_key} is not ssh-ed25519."
            )
        # Base64 output can't hold characters that sshd's AuthorizedKeysCommand
        # would interpret.
        return (
            key.algorithm.decode("ascii"),
            base64.b64encode(key.public_data).decode("ascii"),
        )

    def pin_host_key(self) -> None:
        """
        Pin the worker's host key in `known_hosts` under EzHPCy's stable alias,
        replacing the previous entry and keeping unrelated hosts.
        """
        fields = self.host_public_key.read_text(encoding="utf-8").split()
        if len(fields) < 2:
            raise RuntimeError(
                f"The worker host public key at {self.host_public_key} is malformed."
            )
        replacement = f"{WORKER_HOST_ALIAS} {fields[0]} {fields[1]}\n"
        known_hosts = self.known_hosts
        existing = (
            known_hosts.read_text(encoding="utf-8") if known_hosts.exists() else ""
        )
        retained = [
            line
            for line in existing.splitlines(keepends=True)
            if not line.split(maxsplit=1)
            or line.split(maxsplit=1)[0] != WORKER_HOST_ALIAS
        ]
        known_hosts.write_text("".join(retained) + replacement, encoding="utf-8")
        known_hosts.chmod(0o600)


def _ensure_key_pair(private_key: Path, public_key: Path, *, comment: str) -> None:
    """Create one dedicated local Ed25519 key pair if it does not exist."""
    private_directory(private_key.parent)
    if private_key.exists() != public_key.exists():
        raise RuntimeError(
            f"Incomplete SSH key pair at {private_key}; restore or remove it."
        )
    if not private_key.exists():
        key = asyncssh.generate_private_key("ssh-ed25519", comment=comment)
        private_key_text = key.export_private_key("openssh").decode("ascii")
        public_key_text = key.export_public_key("openssh").decode("ascii")
        # Create with owner-only permissions immediately and refuse a raced-in file;
        # writing first and chmodding afterward briefly exposes private key material.
        private_key_fd = os.open(
            private_key,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(private_key_fd, "w", encoding="ascii", newline="\n") as file:
            file.write(private_key_text)
        public_key.write_text(public_key_text, encoding="ascii")
    restrict_to_current_user(private_key)
    public_key.chmod(0o644)
