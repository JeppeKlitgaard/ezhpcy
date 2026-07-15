import io
import logging
import os
import shlex
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Annotated

import paramiko
import typer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from jinja2 import StrictUndefined, Template
from rich.prompt import Confirm

from ezhpcy import console
from ezhpcy.cli.tunnel.common import (
    ProfileContext,
    local_machine_or_fail,
    with_profile_options,
)
from ezhpcy.cli.utils.ssh import InteractiveSSHClient, absolute_sshd_command
from ezhpcy.config import get_config
from ezhpcy.constants import (
    OPENSSH_MATCHSPEC,
    PIXI_INSTALLER_URL,
    PIXI_VERSION,
    SSH_DIRECTORY_NAME,
    WORKER_CLIENT_KEY_NAME,
    WORKER_HOST_ALIAS,
    WORKER_HOST_KEY_NAME,
)
from ezhpcy.ssh import SSHClient
from ezhpcy.types import RemoteState
from ezhpcy.utils import local_machine_id
from ezhpcy.worker_payload import ensure_worker_payload, require_worker_payload

SSHD_CONFIG_RESOURCE = "static/config/ssh_remote/sshd_config"

logger = logging.getLogger(__name__)


class ProvisioningError(RuntimeError):
    pass


def _ensure_local_key_pair(
    ssh_dir: Path, *, key_name: str, comment: str
) -> tuple[Path, Path]:
    """Create one dedicated local Ed25519 key pair if it does not exist."""
    private_key = ssh_dir / key_name
    public_key = private_key.with_suffix(".pub")

    ssh_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(ssh_dir, 0o700)
    if private_key.exists() != public_key.exists():
        raise RuntimeError(
            f"Incomplete SSH key pair at {private_key}; restore or remove it."
        )
    if not private_key.exists():
        cryptography_key = ed25519.Ed25519PrivateKey.generate()
        private_key_text = cryptography_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("ascii")
        paramiko_key = paramiko.Ed25519Key.from_private_key(
            io.StringIO(private_key_text)
        )
        # Create with owner-only permissions immediately and refuse a raced-in file;
        # writing first and chmodding afterward briefly exposes private key material.
        private_key_fd = os.open(
            private_key,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(private_key_fd, "w", encoding="ascii", newline="\n") as file:
            file.write(private_key_text)
        public_key.write_text(
            f"{paramiko_key.get_name()} {paramiko_key.get_base64()} {comment}\n",
            encoding="ascii",
        )
    os.chmod(private_key, 0o600)
    os.chmod(public_key, 0o644)
    return private_key, public_key


def _ensure_local_ssh_keys(
    ssh_dir: Path,
) -> tuple[Path, Path, Path, Path]:
    """Ensure both worker client and worker host key pairs exist locally."""
    client_keys = _ensure_local_key_pair(
        ssh_dir,
        key_name=WORKER_CLIENT_KEY_NAME,
        comment="ezhpcy worker client",
    )
    host_keys = _ensure_local_key_pair(
        ssh_dir,
        key_name=WORKER_HOST_KEY_NAME,
        comment="ezhpcy worker host",
    )
    return *client_keys, *host_keys


def _render_sshd_config(
    template_text: str, *, remote_username: str, remote_config_dir: PurePosixPath
) -> str:
    return Template(template_text, undefined=StrictUndefined).render(
        remote_username=remote_username,
        remote_config_dir=str(remote_config_dir),
    )


def _pin_worker_host_key(host_public_key: str, known_hosts: Path) -> None:
    """Replace ezhpcy's stable alias entry without touching unrelated hosts."""
    fields = host_public_key.strip().split()
    if len(fields) < 2:
        raise RuntimeError("The remote worker host public key is malformed.")
    replacement = f"{WORKER_HOST_ALIAS} {fields[0]} {fields[1]}\n"
    existing = known_hosts.read_text(encoding="utf-8") if known_hosts.exists() else ""
    retained = [
        line
        for line in existing.splitlines(keepends=True)
        if not line.split(maxsplit=1) or line.split(maxsplit=1)[0] != WORKER_HOST_ALIAS
    ]
    known_hosts.write_text("".join(retained) + replacement, encoding="utf-8")
    os.chmod(known_hosts, 0o600)


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


def provision_worker_infrastructure(
    ssh: SSHClient,
    remote_state: RemoteState,
    *,
    remote_username: str,
    machine_id: str | None = None,
) -> PurePosixPath:
    """Idempotently provision everything needed by a compute worker."""
    machine_id = machine_id or local_machine_id()
    remote_root = remote_state.package_cache_dir()
    remote_ssh_dir = remote_root / SSH_DIRECTORY_NAME / machine_id
    local_ssh_dir = get_config().local_file.ssh_dir(machine_id)

    sshd_config_traversable = resources.files("ezhpcy").joinpath(SSHD_CONFIG_RESOURCE)

    provision_pixi(ssh, remote_state)
    provision_openssh(ssh, remote_state)

    with ssh.sftp_client() as sftp:
        sftp.mkdir(remote_root, parents=True, exist_ok=True)

        (
            _client_private_key,
            client_public_key,
            host_private_key,
            host_public_key,
        ) = _ensure_local_ssh_keys(local_ssh_dir)
        known_hosts = local_ssh_dir / "worker_known_hosts"

        sftp.mkdir(remote_ssh_dir, mode=0o700, parents=True, exist_ok=True)
        sftp.chmod(str(remote_ssh_dir), 0o700)

        remote_authorized_keys = remote_ssh_dir / "authorized_keys"
        sftp.put(str(client_public_key), str(remote_authorized_keys))
        sftp.chmod(str(remote_authorized_keys), 0o600)

        remote_host_key = remote_ssh_dir / WORKER_HOST_KEY_NAME
        remote_host_public_key = PurePosixPath(f"{remote_host_key}.pub")
        remote_sshd_config = remote_ssh_dir / "sshd_config"
        sftp.put(str(host_private_key), str(remote_host_key))
        sftp.put(str(host_public_key), str(remote_host_public_key))

        with resources.as_file(sshd_config_traversable) as sshd_config_template:
            rendered_config = _render_sshd_config(
                sshd_config_template.read_text(encoding="utf-8"),
                remote_username=remote_username,
                remote_config_dir=remote_ssh_dir,
            )
        sftp.write_text(remote_sshd_config, rendered_config)

        sftp.chmod(str(remote_host_key), 0o600)
        sftp.chmod(str(remote_sshd_config), 0o600)
        sftp.chmod(str(remote_host_public_key), 0o644)

        ssh.run_pixi(
            [
                "exec",
                f"--spec={OPENSSH_MATCHSPEC}",
                *absolute_sshd_command(["-t", "-f", str(remote_sshd_config)]),
            ],
            remote_state=remote_state,
        )

        _pin_worker_host_key(host_public_key.read_text(encoding="utf-8"), known_hosts)

    return ensure_worker_payload(ssh, remote_state)


def validate_worker_infrastructure(
    ssh: SSHClient,
    remote_state: RemoteState,
    *,
    machine_id: str | None = None,
) -> PurePosixPath:
    """Validate provisioned files without changing remote state."""
    machine_id = machine_id or local_machine_id()
    remote_ssh_dir = remote_state.package_cache_dir() / SSH_DIRECTORY_NAME / machine_id
    required = (
        remote_state.pixi_executable(),
        remote_ssh_dir / "authorized_keys",
        remote_ssh_dir / WORKER_HOST_KEY_NAME,
        PurePosixPath(f"{remote_ssh_dir / WORKER_HOST_KEY_NAME}.pub"),
        remote_ssh_dir / "sshd_config",
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
            "worker infrastructure is incomplete; run `ezhpcy tunnel provision` "
            f"(missing: {', '.join(missing)})"
        )
    return require_worker_payload(ssh, remote_state)


@with_profile_options
def provision_cmd(
    profile_context: ProfileContext,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Proceed with provisioning without asking for confirmation.",
        ),
    ] = False,
) -> None:
    """Idempotently provision worker infrastructure on the remote HPC host."""
    local_machine_or_fail()
    ssh = InteractiveSSHClient(profile_context.connection)
    ssh.interactive_connect()
    remote_state = ssh.get_remote_state()
    remote_root = remote_state.package_cache_dir()

    user_accepts = yes or Confirm.ask(
        "This will provision or repair [bold purple]ezhpcy[/bold purple] worker "
        f"infrastructure under [bold blue]{remote_root}[/bold blue]. Proceed?",
        console=console,
        default=True,
    )
    if not user_accepts:
        console.print("[bold yellow]Aborted[/bold yellow]: provisioning cancelled.")
        raise typer.Exit(code=1)

    remote_username = profile_context.connection.user
    assert remote_username is not None
    payload_path = provision_worker_infrastructure(
        ssh,
        remote_state,
        remote_username=remote_username,
        machine_id=local_machine_id(),
    )
    console.print(f"Worker payload ready: [bold blue]{payload_path}[/bold blue]")
    console.print("[bold green]Success[/bold green]: remote provisioning completed.")
