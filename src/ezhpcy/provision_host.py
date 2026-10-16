import logging
import re
lazy import asyncio
lazy import shlex
lazy from pathlib import PurePosixPath

lazy import asyncssh

lazy from ezhpcy.constants import OPENSSH_MATCHSPEC, PIXI_INSTALLER_URL, PIXI_VERSION
lazy from ezhpcy.provision_local import WorkerCredentials
lazy from ezhpcy.ssh import RemoteHost
lazy from ezhpcy.types import RemoteState
lazy from ezhpcy.worker.sshd import (
    absolute_sshd_command,
    sshd_config_arguments,
)

_VERSION_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+!-]*$")

logger = logging.getLogger(__name__)


class ProvisioningError(RuntimeError):
    pass


async def provision_pixi(login: RemoteHost, remote_state: RemoteState) -> None:
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
    await login.run(["bash", "-c", command])


async def provision_openssh(login: RemoteHost, remote_state: RemoteState) -> None:
    """Download and cache the pinned OpenSSH environment through Pixi."""
    logger.info("Caching the worker OpenSSH environment.")
    await login.run_pixi(
        [
            "exec",
            f"--spec={OPENSSH_MATCHSPEC}",
            *absolute_sshd_command(["-V"]),
        ],
        remote_state=remote_state,
    )


async def provision_sshd_files(
    login: RemoteHost, remote_state: RemoteState, credentials: WorkerCredentials
) -> None:
    """Provision worker SSH keys and validate the resulting sshd configuration."""
    authorized_key = await asyncio.to_thread(_prepare_local_keys, credentials)

    remote_dir = credentials.remote_dir(remote_state)
    remote_host_key = credentials.remote_host_key(remote_state)
    remote_host_public_key = credentials.remote_host_public_key(remote_state)
    async with login.sftp() as sftp:
        await sftp.makedirs(remote_dir, exist_ok=True)
        # Restrict the directory before the private key goes in.
        await sftp.chmod(remote_dir, 0o700)

        await sftp.put(credentials.host_key, remote_host_key)
        await sftp.put(credentials.host_public_key, remote_host_public_key)

        await sftp.chmod(remote_host_key, 0o600)
        await sftp.chmod(remote_host_public_key, 0o644)

    sshd_arguments = sshd_config_arguments(
        host_key=remote_host_key,
        remote_username=credentials.login_user,
        authorized_key=authorized_key,
    )
    await login.run_pixi(
        [
            "exec",
            f"--spec={OPENSSH_MATCHSPEC}",
            *absolute_sshd_command(["-t", *sshd_arguments]),
        ],
        remote_state=remote_state,
    )


def _prepare_local_keys(credentials: WorkerCredentials) -> tuple[str, str]:
    """
    Create the local worker keys if they don't exist, pin the worker's host key,
    and return the client key for the worker's sshd to authorize. All local file
    work, so it runs in one thread.
    """
    credentials.ensure_local_keys()
    credentials.pin_host_key()
    return credentials.authorized_key()


async def provision_worker_infrastructure(
    login: RemoteHost, remote_state: RemoteState, credentials: WorkerCredentials
) -> None:
    """Idempotently provision everything needed by a compute worker."""
    await provision_pixi(login, remote_state)
    await provision_openssh(login, remote_state)
    await provision_sshd_files(login, remote_state, credentials)


async def validate_worker_infrastructure(
    login: RemoteHost, remote_state: RemoteState, credentials: WorkerCredentials
) -> None:
    """Validate provisioned files without changing remote state."""
    required = (
        remote_state.pixi_executable(),
        credentials.remote_host_key(remote_state),
        credentials.remote_host_public_key(remote_state),
    )
    missing: list[str] = []
    async with login.sftp() as sftp:
        for path in required:
            try:
                await sftp.stat(path)
            except asyncssh.SFTPNoSuchFile:
                missing.append(str(path))

    if missing:
        raise ProvisioningError(
            "worker infrastructure is incomplete; run `ezhpcy provision` "
            f"(missing: {', '.join(missing)})"
        )


async def prune_stale_data(
    login: RemoteHost, remote_state: RemoteState
) -> tuple[PurePosixPath, ...]:
    """Remove remote EzHPCy and Pixi data other than the current versions'."""
    current_directories = (
        remote_state.package_cache_dir(),
        remote_state.package_state_dir(),
        remote_state.pixi_home(),
        remote_state.pixi_cache_dir(),
    )
    async with login.sftp() as sftp:
        stale = [
            path
            for current_directory in current_directories
            for path in await _stale_siblings(sftp, current_directory)
        ]

    if stale:
        await login.run(["rm", "-rf", "--", *(str(path) for path in stale)])
        for path in stale:
            logger.info("Pruned stale data %s.", path)
    else:
        logger.info("Remote EzHPCy data is current.")
    return tuple(stale)


async def _stale_siblings(
    sftp: asyncssh.SFTPClient, current_directory: PurePosixPath
) -> list[PurePosixPath]:
    """Return the other versions' directories next to `current_directory`."""
    root = current_directory.parent
    try:
        names = await sftp.listdir(root)
    except asyncssh.SFTPNoSuchFile:
        logger.debug("No remote data exists at %s.", root)
        return []

    stale: list[PurePosixPath] = []
    for name in names:
        if name in {".", "..", current_directory.name}:
            continue
        if _VERSION_NAME_PATTERN.fullmatch(name) is None:
            logger.warning("Retaining unrecognized entry %s/%s.", root, name)
            continue
        stale.append(root / name)
    return stale


async def prune_all_remote_data(login: RemoteHost, remote_state: RemoteState) -> None:
    """Remove the complete EzHPCy-managed remote footprint."""
    await login.run(
        [
            "rm",
            "-rf",
            "--",
            str(remote_state.package_cache_root()),
            str(remote_state.package_state_root()),
        ]
    )
