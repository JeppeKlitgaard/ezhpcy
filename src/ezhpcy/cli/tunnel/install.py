import io
import shlex
from importlib import resources
from pathlib import PurePosixPath
from typing import Any

import typer

from ezhpcy.cli.tunnel.common import (
    HostOpt,
    PasswordOpt,
    UserOpt,
    connection_info_from_options,
    local_machine_or_fail,
)
from ezhpcy.cli.utils.ssh import InteractiveSSHClient
from ezhpcy.config import config
from ezhpcy import console
from ezhpcy.patch.sdist import sdist_for_current_installation

from rich.prompt import Confirm

INSTALL_DIR_NAME = "ezhpcy"
INSTALL_SCRIPT_RESOURCE = "scripts/install.sh"


def _run_remote(ssh: InteractiveSSHClient, command: str) -> str:
    _, stdout, stderr = ssh.exec_command(command)
    exit_status = stdout.channel.recv_exit_status()
    if exit_status != 0:
        message = stderr.read().decode().strip()
        raise RuntimeError(f"Remote command failed ({exit_status}): {message}")

    return stdout.read().decode()


def _remote_xdg_data_home(ssh: InteractiveSSHClient) -> PurePosixPath:
    data_home = _run_remote(
        ssh,
        'printf %s "${XDG_DATA_HOME:-$HOME/.local/share}"',
    ).strip()
    if not data_home:
        raise RuntimeError("Could not determine remote XDG data directory.")

    return PurePosixPath(data_home)


def _upload_resource(
    ssh: InteractiveSSHClient,
    resource: Any,
    remote_path: PurePosixPath,
) -> None:
    with resource.open("rb") as source:
        data = source.read()

    with ssh.open_sftp() as sftp:
        sftp.putfo(io.BytesIO(data), str(remote_path))


def install_cmd(
    user: UserOpt = config.connection.user,
    password: PasswordOpt = config.connection.password,
    host: HostOpt = config.connection.host,
) -> None:
    """Start an interactive DTU HPC session."""
    local_machine_or_fail()

    conn_info = connection_info_from_options(user=user, password=password, host=host)

    # Connect
    ssh = InteractiveSSHClient(conn_info)
    ssh.interactive_connect()

    console.print("[bold green]Success[/bold green]: connected to host.")
    remote_install_dir = _remote_xdg_data_home(ssh) / INSTALL_DIR_NAME

    # User consent
    user_accepts = Confirm.ask(
        f"This will install [bold purple]ezhpcy[/bold purple] on the remote host into [bold blue]{remote_install_dir}[/bold blue]. Proceed?",
        console=console,
        default=True,
    )

    if not user_accepts:
        console.print("[bold yellow]Aborted[/bold yellow]: installation cancelled by user.")
        raise typer.Exit(code=1)

    install_script = resources.files("ezhpcy").joinpath(INSTALL_SCRIPT_RESOURCE)

    console.print(
        f"Creating remote install directory: [bold blue]{remote_install_dir}[/bold blue]"
    )
    _run_remote(ssh, f"mkdir -p {shlex.quote(str(remote_install_dir))}")

    remote_install_script = remote_install_dir / "install.sh"
    console.print(
        f"Uploading install script: [bold blue]{remote_install_script}[/bold blue]"
    )
    _upload_resource(ssh, install_script, remote_install_script)
    _run_remote(ssh, f"chmod +x {shlex.quote(str(remote_install_script))}")

    with sdist_for_current_installation() as sdist:
        remote_sdist = remote_install_dir / sdist.name
        console.print(f"Uploading sdist: [bold blue]{remote_sdist}[/bold blue]")
        _upload_resource(ssh, sdist, remote_sdist)

    console.print("Running remote installer.")
    install_output = _run_remote(
        ssh,
        f"bash {shlex.quote(str(remote_install_script))} {shlex.quote(str(remote_sdist))}",
    )
    if install_output:
        console.print(install_output.rstrip())

    console.print("[bold green]Success[/bold green]: remote installation completed.")

