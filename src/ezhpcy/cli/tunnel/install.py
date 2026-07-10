from importlib import resources
from typing import Annotated

import typer
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
from ezhpcy.patch.sdist import sdist_for_current_installation

INSTALL_DIR_NAME = "ezhpcy"
INSTALL_SCRIPT_RESOURCE = "scripts/install.sh"


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
    """Start an interactive HPC session."""
    local_machine_or_fail()

    conn_info = connection_info_from_options(user=user, password=password, host=host)

    # Connect
    ssh = InteractiveSSHClient(conn_info)
    ssh.interactive_connect()

    # Get remote file_config
    remote_file_config = ssh.get_file_config()

    console.print("[bold green]Success[/bold green]: connected to host.")
    remote_install_dir = remote_file_config.data_dir / INSTALL_DIR_NAME

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
    with resources.as_file(install_script_traversable) as install_script:
        console.print(
            f"Creating remote install directory: [bold blue]{remote_install_dir}[/bold blue]"
        )
        ssh.run(["mkdir", "-p", str(remote_install_dir)])

        remote_install_script = remote_install_dir / "install.sh"
        console.print(
            f"Uploading install script: [bold blue]{remote_install_script}[/bold blue]"
        )
        ssh.upload_file(install_script, remote_install_script)

    ssh.run(["chmod", "+x", str(remote_install_script)])

    with sdist_for_current_installation() as sdist:
        remote_sdist = remote_install_dir / sdist.name
        console.print(f"Uploading sdist: [bold blue]{remote_sdist}[/bold blue]")
        ssh.upload_file(sdist, remote_sdist)

    console.print("Running remote installer.")
    install_output = ssh.run(["bash", str(remote_install_script), str(remote_sdist)])
    if install_output:
        console.print(install_output.rstrip())

    console.print("[bold green]Success[/bold green]: remote installation completed.")
