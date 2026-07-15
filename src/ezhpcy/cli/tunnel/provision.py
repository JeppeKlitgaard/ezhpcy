import logging
import os
import shlex
import subprocess
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Annotated

import typer
from jinja2 import StrictUndefined, Template
from rich.prompt import Confirm

from ezhpcy import console
from ezhpcy.cli.tunnel.common import (
    ProfileContext,
    local_machine_or_fail,
    with_profile_options,
)
from ezhpcy.cli.utils.ssh import InteractiveSSHClient, absolute_sshd_command
from ezhpcy.config import RemoteFileConfig, get_config
from ezhpcy.constants import (
    OPENSSH_MATCHSPEC,
    PACKAGE_NAME,
    SSH_DIRECTORY_NAME,
    WORKER_CLIENT_KEY_NAME,
    WORKER_HOST_ALIAS,
    WORKER_HOST_KEY_NAME,
)
from ezhpcy.ssh import SSHClient
from ezhpcy.worker_payload import ensure_worker_payload, require_worker_payload

REMOTE_ROOT_NAME = PACKAGE_NAME
PROVISION_SCRIPT_RESOURCE = "static/data/provision.sh.j2"
SSHD_CONFIG_RESOURCE = "static/config/ssh_remote/sshd_config"

logger = logging.getLogger(__name__)


class ProvisioningError(RuntimeError):
    pass


def _ensure_local_client_key(ssh_dir: Path) -> tuple[Path, Path]:
    """Create ezhpcy's dedicated worker client key if it does not exist."""
    private_key = ssh_dir / WORKER_CLIENT_KEY_NAME
    public_key = private_key.with_suffix(".pub")

    ssh_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(ssh_dir, 0o700)
    if private_key.exists() != public_key.exists():
        raise RuntimeError(
            f"Incomplete worker client key pair at {private_key}; restore or remove it."
        )
    if not private_key.exists():
        subprocess.run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                "ezhpcy worker client",
                "-f",
                str(private_key),
            ],
            check=True,
        )
    os.chmod(private_key, 0o600)
    os.chmod(public_key, 0o644)
    return private_key, public_key


def _render_sshd_config(
    template_text: str, *, remote_username: str, remote_config_dir: PurePosixPath
) -> str:
    return Template(template_text, undefined=StrictUndefined).render(
        remote_username=remote_username,
        remote_config_dir=str(remote_config_dir),
    )


def _render_shell_script(template_text: str, **variables: str) -> str:
    """Render a self-contained shell script from its packaged template."""
    quoted_variables = {name: shlex.quote(value) for name, value in variables.items()}
    return Template(template_text, undefined=StrictUndefined).render(**quoted_variables)


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


def provision_worker_infrastructure(
    ssh: SSHClient,
    remote_file_config: RemoteFileConfig,
    *,
    remote_username: str,
) -> PurePosixPath:
    """Idempotently provision everything needed by a compute worker."""
    remote_root = remote_file_config.data_dir / REMOTE_ROOT_NAME
    remote_ssh_dir = (
        remote_file_config.config_dir / REMOTE_ROOT_NAME / SSH_DIRECTORY_NAME
    )
    local_ssh_dir = get_config().local_file.config_dir / SSH_DIRECTORY_NAME

    provision_template = resources.files("ezhpcy").joinpath(PROVISION_SCRIPT_RESOURCE)
    sshd_config_traversable = resources.files("ezhpcy").joinpath(SSHD_CONFIG_RESOURCE)
    provision_script = _render_shell_script(
        provision_template.read_text(encoding="utf-8"),
        openssh_matchspec=OPENSSH_MATCHSPEC,
    )

    with ssh.sftp_client() as sftp:
        sftp.mkdir(remote_root, parents=True, exist_ok=True)
        remote_provision_script = remote_root / "provision.sh"
        sftp.write_text(remote_provision_script, provision_script)
        sftp.chmod(str(remote_provision_script), 0o755)

        logger.info("Provisioning remote Pixi and OpenSSH infrastructure.")
        ssh.run(["bash", str(remote_provision_script)])

        _private_key, public_key = _ensure_local_client_key(local_ssh_dir)
        known_hosts = local_ssh_dir / "worker_known_hosts"

        sftp.mkdir(remote_ssh_dir, mode=0o700, parents=True, exist_ok=True)
        sftp.chmod(str(remote_ssh_dir), 0o700)

        remote_authorized_keys = remote_ssh_dir / "authorized_keys"
        sftp.put(str(public_key), str(remote_authorized_keys))
        sftp.chmod(str(remote_authorized_keys), 0o600)

        remote_host_key = remote_ssh_dir / WORKER_HOST_KEY_NAME
        remote_host_public_key = PurePosixPath(f"{remote_host_key}.pub")
        remote_sshd_config = remote_ssh_dir / "sshd_config"
        try:
            sftp.stat(str(remote_host_key))
        except FileNotFoundError:
            ssh.run_pixi(
                [
                    "exec",
                    f"--spec={OPENSSH_MATCHSPEC}",
                    "ssh-keygen",
                    "-q",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-C",
                    "ezhpcy worker host",
                    "-f",
                    str(remote_host_key),
                ],
                file_config=remote_file_config,
            )

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
            file_config=remote_file_config,
        )

        host_public_key = sftp.read_text(remote_host_public_key)
        _pin_worker_host_key(host_public_key, known_hosts)

    return ensure_worker_payload(ssh, remote_file_config)


def validate_worker_infrastructure(
    ssh: SSHClient, remote_file_config: RemoteFileConfig
) -> PurePosixPath:
    """Validate provisioned files without changing remote state."""
    remote_root = remote_file_config.data_dir / REMOTE_ROOT_NAME
    remote_ssh_dir = (
        remote_file_config.config_dir / REMOTE_ROOT_NAME / SSH_DIRECTORY_NAME
    )
    required = (
        remote_root / "pixi_home/bin/pixi",
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
    return require_worker_payload(ssh, remote_file_config)


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
    remote_file_config = ssh.get_file_config()
    remote_root = remote_file_config.data_dir / REMOTE_ROOT_NAME

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
        remote_file_config,
        remote_username=remote_username,
    )
    console.print(f"Worker payload ready: [bold blue]{payload_path}[/bold blue]")
    console.print("[bold green]Success[/bold green]: remote provisioning completed.")
