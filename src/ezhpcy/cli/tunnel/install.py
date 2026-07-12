import os
import subprocess
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Annotated

import typer
from jinja2 import StrictUndefined, Template
from rich.prompt import Confirm

from ezhpcy import console
from ezhpcy.cli.tunnel.common import (
    HostOpt,
    PasswordOpt,
    UserOpt,
    connection_info_from_options,
    local_machine_or_fail,
)
from ezhpcy.cli.utils.ssh import InteractiveSSHClient
from ezhpcy.config import config
from ezhpcy.constants import (
    OPENSSH_MATCHSPEC,
    SSH_DIRECTORY_NAME,
    UV_MATCHSPEC,
    WORKER_HOST_KEY_NAME,
)
from ezhpcy.patch.sdist import sdist_for_current_installation

INSTALL_DIR_NAME = "ezhpcy"
INSTALL_SCRIPT_RESOURCE = "static/data/install.sh"
UNINSTALL_SCRIPT_RESOURCE = "static/data/uninstall.sh"
SSHD_CONFIG_RESOURCE = "static/config/ssh_remote/sshd_config"
WORKER_CLIENT_KEY_NAME = "worker_client_ed25519"
WORKER_HOST_ALIAS = "ezhpcy-worker"


def _ensure_not_installed(ssh: InteractiveSSHClient) -> None:
    """Abort installation when ezhpcy is already available remotely."""
    try:
        ssh.run(["ezhpcy", "version"])
    except RuntimeError:
        return

    console.print(
        "[bold yellow]Aborted[/bold yellow]: ezhpcy is already installed on the "
        "remote host. Use [bold blue]ezhpcy tunnel reinstall[/bold blue] instead, "
        "or run [bold blue]ezhpcy tunnel uninstall[/bold blue] first."
    )
    raise typer.Exit(code=1)


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


def install_cmd(
    user: UserOpt = config.connection.user,
    password: PasswordOpt = config.connection.password,
    host: HostOpt = config.connection.host,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Proceed with installation without asking for confirmation.",
        ),
    ] = False,
) -> None:
    """Install ezhpcy and provision worker SSH on the remote HPC host."""
    local_machine_or_fail()

    conn_info = connection_info_from_options(user=user, password=password, host=host)

    # Connect
    ssh = InteractiveSSHClient(conn_info)
    ssh.interactive_connect()

    _ensure_not_installed(ssh)

    # Get remote file_config
    remote_file_config = ssh.get_file_config()

    console.print("[bold green]Success[/bold green]: connected to host.")
    remote_install_dir = remote_file_config.data_dir / INSTALL_DIR_NAME
    remote_ssh_dir = (
        remote_file_config.config_dir / INSTALL_DIR_NAME / SSH_DIRECTORY_NAME
    )
    local_ssh_dir = config.local_file.config_dir / SSH_DIRECTORY_NAME

    # User consent
    user_accepts = yes or Confirm.ask(
        f"This will install [bold purple]ezhpcy[/bold purple] on the remote host into [bold blue]{remote_install_dir}[/bold blue]. Proceed?",
        console=console,
        default=True,
    )

    if not user_accepts:
        console.print(
            "[bold yellow]Aborted[/bold yellow]: installation cancelled by user."
        )
        raise typer.Exit(code=1)

    install_script_traversable = resources.files("ezhpcy").joinpath(
        INSTALL_SCRIPT_RESOURCE
    )
    uninstall_script_traversable = resources.files("ezhpcy").joinpath(
        UNINSTALL_SCRIPT_RESOURCE
    )
    sshd_config_traversable = resources.files("ezhpcy").joinpath(SSHD_CONFIG_RESOURCE)

    with ssh.sftp_client() as sftp:
        with (
            resources.as_file(install_script_traversable) as install_script,
            resources.as_file(uninstall_script_traversable) as uninstall_script,
        ):
            console.print(
                f"Creating remote install directory: [bold blue]{remote_install_dir}[/bold blue]"
            )
            sftp.mkdir(remote_install_dir, parents=True, exist_ok=True)

            remote_install_script = remote_install_dir / "install.sh"
            console.print(
                f"Uploading install script: [bold blue]{remote_install_script}[/bold blue]"
            )
            sftp.put(str(install_script), str(remote_install_script))

            remote_uninstall_script = remote_install_dir / "uninstall.sh"
            console.print(
                f"Uploading uninstall script: [bold blue]{remote_uninstall_script}[/bold blue]"
            )
            sftp.put(str(uninstall_script), str(remote_uninstall_script))

        sftp.chmod(str(remote_install_script), 0o755)
        sftp.chmod(str(remote_uninstall_script), 0o755)

        with sdist_for_current_installation() as sdist:
            remote_sdist = remote_install_dir / sdist.name
            console.print(f"Uploading sdist: [bold blue]{remote_sdist}[/bold blue]")
            sftp.put(str(sdist), str(remote_sdist))

        console.print("Running remote installer.")
        install_output = ssh.run(
            [
                "bash",
                str(remote_install_script),
                str(remote_sdist),
                UV_MATCHSPEC,
                OPENSSH_MATCHSPEC,
            ]
        )
        if install_output:
            console.print(install_output.rstrip())

        console.print("Provisioning worker SSH keys and configuration.")
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
                remote_username=conn_info.user,
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
                "sshd",
                "-t",
                "-f",
                str(remote_sshd_config),
            ],
            file_config=remote_file_config,
        )

        host_public_key = sftp.read_text(remote_host_public_key)
        _pin_worker_host_key(host_public_key, known_hosts)

    console.print("[bold green]Success[/bold green]: remote installation completed.")
