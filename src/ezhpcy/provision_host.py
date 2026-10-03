import logging
lazy import shlex
lazy from pathlib import PurePosixPath

lazy from ezhpcy.config import config
lazy from ezhpcy.constants import (
    OPENSSH_MATCHSPEC,
    PIXI_INSTALLER_URL,
    PIXI_VERSION,
    SSH_DIRECTORY_NAME,
    WORKER_HOST_KEY_NAME,
)
lazy from ezhpcy.provision_local import ensure_local_ssh_keys, pin_worker_host_key
lazy from ezhpcy.ssh import SSHClient
lazy from ezhpcy.tunnel.sshd import (
    absolute_sshd_command,
    read_ed25519_public_key,
    sshd_config_arguments,
)
lazy from ezhpcy.types import RemoteState
lazy from ezhpcy.utils import local_machine_id, ssh_connection_id

logger = logging.getLogger(__name__)


class ProvisioningError(RuntimeError):
    pass


def provision_pixi(ssh: SSHClient, remote_state: RemoteState) -> None:
    """Install the pinned private Pixi version when it is not already present."""
    home = remote_state.pixi_home()
    pixi = remote_state.pixi_executable()
    download = shlex.join(
        [
            "curl",
            "--fail",
            "--location",
            "--show-error",
            "--silent",
            PIXI_INSTALLER_URL,
        ]
    )
    install = shlex.join(
        [
            "env",
            f"PIXI_HOME={home}",
            f"PIXI_VERSION={PIXI_VERSION}",
            "PIXI_NO_PATH_UPDATE=1",
            "bash",
        ]
    )
    command = (
        "set -euo pipefail\n"
        f"if [ ! -x {shlex.quote(str(pixi))} ]; then\n"
        f"    {download} | {install}\n"
        "fi"
    )

    logger.info("Ensuring Pixi %s is installed at %s.", PIXI_VERSION, home)
    ssh.run(["bash", "-c", command])


def provision_openssh(ssh: SSHClient, remote_state: RemoteState) -> None:
    """Download and cache the pinned OpenSSH environment through Pixi."""
    logger.info("Caching the worker OpenSSH environment.")
    ssh.run_pixi(
        [
            "exec",
            f"--spec={OPENSSH_MATCHSPEC}",
            *absolute_sshd_command(["-V"]),
        ],
        remote_state=remote_state,
    )


def provision_sshd_files(
    ssh: SSHClient,
    remote_state: RemoteState,
    *,
    remote_username: str,
    remote_host: str,
    machine_id: str | None = None,
) -> None:
    """Provision worker SSH keys and validate the resulting sshd configuration."""
    machine_id = machine_id or local_machine_id()
    connection_id = ssh_connection_id(remote_username, remote_host)
    remote_root = remote_state.package_cache_dir()
    remote_ssh_dir = remote_root / SSH_DIRECTORY_NAME / machine_id / connection_id
    local_ssh_dir = config.local_file.ssh_dir(
        machine_id, user=remote_username, host=remote_host
    )

    with ssh.sftp_client() as sftp:
        sftp.mkdir(remote_root, parents=True, exist_ok=True)

        (
            _client_private_key,
            client_public_key,
            host_private_key,
            host_public_key,
        ) = ensure_local_ssh_keys(local_ssh_dir)
        known_hosts = local_ssh_dir / "worker_known_hosts"

        sftp.mkdir(remote_ssh_dir, mode=0o700, parents=True, exist_ok=True)
        sftp.chmod(str(remote_ssh_dir), 0o700)

        remote_host_key = remote_ssh_dir / WORKER_HOST_KEY_NAME
        remote_host_public_key = PurePosixPath(f"{remote_host_key}.pub")
        sftp.put(str(host_private_key), str(remote_host_key))
        sftp.put(str(host_public_key), str(remote_host_public_key))

        sftp.chmod(str(remote_host_key), 0o600)
        sftp.chmod(str(remote_host_public_key), 0o644)

        sshd_arguments = sshd_config_arguments(
            host_key=remote_host_key,
            remote_username=remote_username,
            authorized_key=read_ed25519_public_key(client_public_key),
        )
        ssh.run_pixi(
            [
                "exec",
                f"--spec={OPENSSH_MATCHSPEC}",
                *absolute_sshd_command(["-t", *sshd_arguments]),
            ],
            remote_state=remote_state,
        )

        pin_worker_host_key(host_public_key.read_text(encoding="utf-8"), known_hosts)


def provision_worker_infrastructure(
    ssh: SSHClient,
    remote_state: RemoteState,
    *,
    remote_username: str,
    remote_host: str,
    machine_id: str | None = None,
) -> None:
    """Idempotently provision everything needed by a compute worker."""
    provision_pixi(ssh, remote_state)
    provision_openssh(ssh, remote_state)
    provision_sshd_files(
        ssh,
        remote_state,
        remote_username=remote_username,
        remote_host=remote_host,
        machine_id=machine_id,
    )


def validate_worker_infrastructure(
    ssh: SSHClient,
    remote_state: RemoteState,
    *,
    remote_username: str,
    remote_host: str,
    machine_id: str | None = None,
) -> None:
    """Validate provisioned files without changing remote state."""
    machine_id = machine_id or local_machine_id()
    connection_id = ssh_connection_id(remote_username, remote_host)
    remote_ssh_dir = (
        remote_state.package_cache_dir()
        / SSH_DIRECTORY_NAME
        / machine_id
        / connection_id
    )
    required = (
        remote_state.pixi_executable(),
        remote_ssh_dir / WORKER_HOST_KEY_NAME,
        PurePosixPath(f"{remote_ssh_dir / WORKER_HOST_KEY_NAME}.pub"),
    )
    missing: list[str] = []
    with ssh.sftp_client() as sftp:
        for path in required:
            try:
                sftp.stat(str(path))
            except FileNotFoundError:
                missing.append(str(path))

    if missing:
        raise ProvisioningError(
            "worker infrastructure is incomplete; run `ezhpcy provision` "
            f"(missing: {', '.join(missing)})"
        )
